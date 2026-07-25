"""yt-dlp wrapper — Feature D (IG / TikTok / YouTube) with a quality selector.

Flow: `extract_meta()` first (no download) to build the preview + buttons, then
`download_video_quality()` / `download_audio()` when the user picks a tier.
Blocking yt-dlp calls run in killable child processes so the async event loop
stays responsive and a stuck extractor/ffmpeg/Deno process cannot consume a
worker forever.
"""
import asyncio
import importlib.util
import json
import logging
import math
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

logger = logging.getLogger(__name__)

SUPPORTED_HOSTS = (
    "instagram.com", "instagr.am", "cdninstagram.com",
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "youtube.com", "youtu.be", "m.youtube.com",
    "facebook.com", "fb.watch",
    "twitter.com", "x.com",
)


def _worker_count() -> int:
    raw = (os.getenv("YTDLP_CONCURRENCY") or "3").strip()
    try:
        return max(1, min(int(raw), 8))
    except ValueError:
        logger.warning("Invalid YTDLP_CONCURRENCY=%r; using 3", raw)
        return 3


# yt-dlp is blocking and CPU/network heavy. Bound both running children and the
# wait queue so bursts cannot exhaust memory, processes, or file descriptors.
_MAX_WORKERS = _worker_count()
_ADMISSION_LIMIT = _MAX_WORKERS * 2
_ADMITTED = 0
_PROCESS_GATE = asyncio.Semaphore(_MAX_WORKERS)
_ACTIVE_PROCESSES: dict[int, asyncio.subprocess.Process] = {}
_PROVIDER_TASKS: set[asyncio.Task] = set()
_CLOSING = False

_BLOCKED_UNTIL: dict[str, tuple[float, str]] = {}


class ProviderBusy(RuntimeError):
    pass


class ProviderTimeout(RuntimeError):
    pass


def _claim_provider_slot() -> None:
    global _ADMITTED
    if _CLOSING or _ADMITTED >= _ADMISSION_LIMIT:
        raise ProviderBusy("media provider queue is busy")
    _ADMITTED += 1


def _release_provider_slot() -> None:
    global _ADMITTED
    _ADMITTED -= 1


def _provider_job_timeout(operation: str | None = None) -> int:
    metadata_job = operation in {"search", "extract_meta"}
    name = "YTDLP_METADATA_TIMEOUT_SECONDS" if metadata_job else "YTDLP_JOB_TIMEOUT_SECONDS"
    default = "90" if metadata_job else "600"
    raw = (os.getenv(name) or default).strip()
    try:
        timeout = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be a positive integer, got {raw!r}"
        ) from exc
    if timeout <= 0:
        raise RuntimeError(
            f"{name} must be a positive integer, got {raw!r}"
        )
    return timeout


def _private_worker_env(job_dir: str) -> dict[str, str]:
    """Build a least-privilege environment for a yt-dlp worker process."""
    passthrough = {
        "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED", "HOME", "XDG_CACHE_HOME", "TMPDIR", "LANG", "LC_ALL",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "DENO_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "YTDLP_PROXY", "YTDLP_PLAYER_CLIENT", "YTDLP_SLEEP_REQUESTS",
        "YTDLP_JOB_TIMEOUT_SECONDS", "YTDLP_METADATA_TIMEOUT_SECONDS",
        "DOWNLOAD_MAX_SECONDS", "SEARCH_MAX_SECONDS",
    }
    env = {key: value for key, value in os.environ.items() if key in passthrough}
    cookie_file = os.getenv("YTDLP_COOKIES_FILE")
    if cookie_file and os.path.isfile(cookie_file):
        private_cookie = os.path.join(job_dir, "cookies.txt")
        shutil.copyfile(cookie_file, private_cookie)
        os.chmod(private_cookie, 0o600)
        env["YTDLP_COOKIES_FILE"] = private_cookie
    return env


def _write_private_json(path: str, payload: object) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        # Wait a short fixed grace even if the Python leader exits immediately:
        # ffmpeg/Deno descendants can ignore TERM while keeping the group alive.
        await asyncio.sleep(1)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:  # pragma: no cover - production image and CI are POSIX
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        logger.error("provider worker pid=%s did not exit after forced termination", process.pid)


async def _run_uninterruptible_cleanup(awaitable) -> tuple[object, bool]:
    """Finish process cleanup even after repeated cancellation requests."""
    cleanup = asyncio.create_task(awaitable)
    interrupted = False
    while True:
        try:
            return await asyncio.shield(cleanup), interrupted
        except asyncio.CancelledError:
            # A cancellation can arrive in the same event-loop turn in which
            # cleanup completes.  It still has to be propagated; otherwise a
            # shutdown request is silently converted into normal completion.
            interrupted = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            if cleanup.done():
                return cleanup.result(), True


async def _communicate_with_deadline(
    process: asyncio.subprocess.Process, timeout_seconds: float,
) -> tuple[bytes, bytes]:
    communication = asyncio.create_task(process.communicate())

    async def drain_after_stop() -> None:
        try:
            await asyncio.wait_for(asyncio.shield(communication), timeout=5)
        except asyncio.TimeoutError:
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)

    try:
        return await asyncio.wait_for(
            asyncio.shield(communication), timeout=timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        async def stop_timed_out_worker() -> None:
            await _kill_process_group(process)
            await drain_after_stop()

        _, interrupted = await _run_uninterruptible_cleanup(stop_timed_out_worker())
        if interrupted:
            raise asyncio.CancelledError
        raise ProviderTimeout("media provider job timed out") from exc
    except asyncio.CancelledError:
        async def stop_cancelled_worker() -> None:
            await _kill_process_group(process)
            await drain_after_stop()

        await _run_uninterruptible_cleanup(stop_cancelled_worker())
        raise


async def _await_worker_spawn(
    spawn_task: asyncio.Task, timeout_seconds: float,
) -> asyncio.subprocess.Process:
    """Bound process creation and reap it if its caller is cancelled mid-spawn."""
    try:
        return await asyncio.wait_for(
            asyncio.shield(spawn_task), timeout=timeout_seconds
        )
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        async def finish_spawn_and_stop() -> None:
            process = None
            try:
                process = await asyncio.wait_for(asyncio.shield(spawn_task), timeout=5)
            except (Exception, asyncio.CancelledError):
                spawn_task.cancel()
                await asyncio.gather(spawn_task, return_exceptions=True)
                # Process creation can win the race with the timeout/cancel.
                # If cancel() returned too late, retrieve and terminate the
                # successfully-created child instead of leaking it.
                if spawn_task.done() and not spawn_task.cancelled():
                    try:
                        process = spawn_task.result()
                    except Exception:
                        pass
            if process is not None:
                await _kill_process_group(process)

        _, interrupted = await _run_uninterruptible_cleanup(finish_spawn_and_stop())
        if interrupted and isinstance(exc, asyncio.TimeoutError):
            raise asyncio.CancelledError
        if isinstance(exc, asyncio.TimeoutError):
            raise ProviderTimeout("media provider job timed out while starting") from exc
        raise


def _raise_worker_error(payload: dict) -> None:
    message = safe_error_message(payload.get("message") or "provider worker failed")
    error_type = payload.get("error_type")
    if error_type == "DownloadTooLarge":
        raise DownloadTooLarge(message)
    if error_type == "DownloadError":
        raise yt_dlp.utils.DownloadError(message)
    if error_type == "ValueError":
        raise ValueError(message)
    if error_type in {"TimeoutExpired", "ProviderTimeout"}:
        raise ProviderTimeout(message)
    if error_type == "FileNotFoundError":
        raise FileNotFoundError(message)
    raise RuntimeError(message)


async def _run_provider_process(operation: str, *args) -> object:
    """Run yt-dlp in a disposable process with queue and wall-clock bounds."""
    # Validate before mutating admission/task state so a bad runtime override
    # cannot permanently consume a queue slot.
    timeout_seconds = _provider_job_timeout(operation)
    _claim_provider_slot()
    owner_task = asyncio.current_task()
    if owner_task is not None:
        _PROVIDER_TASKS.add(owner_task)
    gate_acquired = False
    process: asyncio.subprocess.Process | None = None
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        remaining = deadline - asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(_PROCESS_GATE.acquire(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise ProviderTimeout("media provider job timed out in queue") from exc
        gate_acquired = True
        if _CLOSING:
            raise ProviderBusy("media provider queue is busy")

        with tempfile.TemporaryDirectory(prefix="musiqa_provider_") as job_dir:
            request_path = os.path.join(job_dir, "request.json")
            response_path = os.path.join(job_dir, "response.json")
            _write_private_json(
                request_path,
                {"operation": operation, "args": list(args)},
            )
            worker_env = _private_worker_env(job_dir)
            spawn_kwargs = {"start_new_session": True} if os.name == "posix" else {}
            spawn_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "bot.services.downloader_worker",
                    request_path,
                    response_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=worker_env,
                    **spawn_kwargs,
                )
            )
            remaining = max(0.001, deadline - asyncio.get_running_loop().time())
            process = await _await_worker_spawn(spawn_task, remaining)
            _ACTIVE_PROCESSES[process.pid] = process
            if _CLOSING:
                await _kill_process_group(process)
                raise ProviderBusy("media provider queue is busy")
            remaining = max(0.001, deadline - asyncio.get_running_loop().time())
            _, stderr = await _communicate_with_deadline(process, remaining)
            if stderr:
                warning = safe_error_message(
                    stderr.decode("utf-8", errors="replace")[-4000:].strip()
                )
                if warning:
                    logger.warning("provider worker: %s", warning)
            if process.returncode != 0 or not os.path.isfile(response_path):
                raise RuntimeError(
                    f"provider worker exited without a result (code={process.returncode})"
                )
            with open(response_path, encoding="utf-8") as stream:
                payload = json.load(stream)
            if not isinstance(payload, dict):
                raise RuntimeError("provider worker returned an invalid result")
            if not payload.get("ok"):
                _raise_worker_error(payload)
            return payload.get("result")
    finally:
        if process is not None:
            _ACTIVE_PROCESSES.pop(process.pid, None)
        if gate_acquired:
            _PROCESS_GATE.release()
        if owner_task is not None:
            _PROVIDER_TASKS.discard(owner_task)
        _release_provider_slot()


async def shutdown_provider_workers() -> None:
    """Stop provider children before cookie/session teardown during shutdown."""
    global _CLOSING
    _CLOSING = True
    current = asyncio.current_task()
    tasks = [task for task in _PROVIDER_TASKS if task is not current]
    for task in tasks:
        task.cancel()
    if tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=10
            )
        except asyncio.TimeoutError:
            logger.error("provider tasks did not stop within shutdown deadline")
    # Defense in depth for a task that failed before registering its cleanup.
    active = list(_ACTIVE_PROCESSES.values())
    if active:
        await asyncio.gather(
            *(_kill_process_group(process) for process in active),
            return_exceptions=True,
        )


@dataclass
class DownloadResult:
    path: str
    title: str
    uploader: str
    duration: float | None
    ext: str


@dataclass
class MediaMeta:
    url: str
    title: str
    uploader: str
    duration: float | None
    thumbnail: str | None
    heights: list[int]


_URL_IN_LOG = re.compile(r"(?:https?|socks[45]h?)://[^\s]+", re.IGNORECASE)
_STALE_MEDIA_FILE = re.compile(r"^(?:audio|video)_[0-9a-f]{12}(?:\..+)?$")
_STALE_JOB_PREFIXES = (
    ".musiqa_job_audio_", ".musiqa_job_video_",
    ".musiqa_recognize_", ".musiqa_round_",
)


class DownloadTooLarge(RuntimeError):
    """Raised while streaming when source media exceeds the configured cap."""


def cleanup_stale_runtime_files(
    download_dir: str,
    *,
    max_age_seconds: int | None = None,
    temp_dir: str | None = None,
) -> int:
    """Remove only validated bot-owned leftovers from an interrupted process."""
    max_age = max_age_seconds
    if max_age is None:
        max_age = max(600, _provider_job_timeout() * 2)
    cutoff = time.time() - max(0, max_age)
    removed = 0

    def old(path: Path) -> bool:
        try:
            return path.lstat().st_mtime < cutoff
        except OSError:
            return False

    os.makedirs(download_dir, exist_ok=True)
    for path in Path(download_dir).iterdir():
        if path.is_symlink() or not old(path):
            continue
        try:
            if path.is_dir() and path.name.startswith(_STALE_JOB_PREFIXES):
                shutil.rmtree(path)
                removed += 1
            elif path.is_file() and _STALE_MEDIA_FILE.fullmatch(path.name):
                path.unlink()
                removed += 1
        except OSError:
            logger.warning("Could not remove stale bot runtime path %s", path.name)

    runtime_temp = Path(temp_dir or tempfile.gettempdir())
    try:
        temp_entries = list(runtime_temp.iterdir())
    except OSError:
        temp_entries = []
    for path in temp_entries:
        if path.is_symlink() or not old(path):
            continue
        try:
            if path.is_dir() and path.name.startswith("musiqa_provider_"):
                shutil.rmtree(path)
                removed += 1
            elif path.is_file() and path.name.startswith("musiqa_yt_cookies_"):
                path.unlink()
                removed += 1
        except OSError:
            logger.warning("Could not remove stale bot temp path %s", path.name)
    return removed


def safe_error_message(message: object) -> str:
    """Drop URL credentials/query strings before provider errors reach logs."""
    def clean(match: re.Match) -> str:
        raw = match.group(0)
        try:
            parsed = urlparse(raw)
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{host}{port}/<redacted>"
        except (ValueError, TypeError):
            return "<redacted-url>"

    return _URL_IN_LOG.sub(clean, str(message))


class _YDLLogger:
    def debug(self, message):
        logger.debug("yt-dlp: %s", safe_error_message(message))

    def warning(self, message):
        logger.warning("yt-dlp: %s", safe_error_message(message))

    def error(self, message):
        logger.error("yt-dlp: %s", safe_error_message(message))


def runtime_warnings() -> list[str]:
    """Return actionable dependency/config warnings for startup logs."""
    warnings: list[str] = []
    if shutil.which("ffmpeg") is None:
        warnings.append("ffmpeg is missing; conversion and recognition will fail")
    if shutil.which("deno") is None:
        warnings.append(
            "Deno is missing; current YouTube downloads need an external JS runtime"
        )
    if importlib.util.find_spec("yt_dlp_ejs") is None:
        warnings.append(
            "yt-dlp-ejs is missing; install yt-dlp with the [default] extra"
        )
    cookie_file = os.getenv("YTDLP_COOKIES_FILE")
    if cookie_file and not os.path.isfile(cookie_file):
        warnings.append(f"YTDLP_COOKIES_FILE does not exist: {cookie_file}")
    on_railway = any(
        os.getenv(name)
        for name in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID")
    )
    if on_railway and not cookie_file and not os.getenv("YTDLP_PROXY"):
        warnings.append(
            "Railway/datacenter YouTube requests may be bot-checked; configure "
            "YTDLP_COOKIES_CONTENT and, if needed, YTDLP_PROXY"
        )
    return warnings


def download_error_key(exc: BaseException) -> str:
    """Map provider failures to a localized, honest user-facing error key."""
    if isinstance(exc, (ProviderBusy, ProviderTimeout)):
        return "service_busy"
    message = str(exc).lower()
    if any(part in message for part in (
        "private video", "private account", "login to view", "members-only",
        "private or authenticated media",
    )):
        return "download_private"
    if any(part in message for part in (
        "configured size limit", "larger than max-filesize",
        "exceeds the configured maximum duration", "live streams are not supported",
    )):
        return "download_too_large"
    if "media provider queue is busy" in message:
        return "service_busy"
    if any(part in message for part in (
        "sign in to confirm", "not a bot", "cookies are required",
        "login required", "http error 403", "bot-checked",
    )):
        return "download_blocked"
    if any(part in message for part in ("http error 429", "too many requests", "rate limit")):
        return "download_rate_limited"
    return "download_failed"


def provider_name(url: str) -> str:
    host = (urlparse(url).hostname or "unknown").lower().removeprefix("www.")
    if host == "youtu.be" or host.endswith(".youtube.com") or host == "youtube.com":
        return "youtube"
    return host


def provider_error_key(url: str) -> str | None:
    """Return an active short-lived provider circuit-breaker error, if any."""
    provider = provider_name(url)
    blocked = _BLOCKED_UNTIL.get(provider)
    if not blocked:
        return None
    deadline, error_key = blocked
    if deadline <= time.monotonic():
        _BLOCKED_UNTIL.pop(provider, None)
        return None
    return error_key


def record_provider_failure(url: str, exc: BaseException) -> None:
    """Avoid repeatedly hammering a source that just blocked this server."""
    # Handler pipelines also contain Telegram, SQLite, filesystem and ffmpeg
    # work. Only yt-dlp failures are evidence that the source itself is blocked.
    if not isinstance(exc, yt_dlp.utils.DownloadError):
        return
    error_key = download_error_key(exc)
    cooldown = {
        "download_blocked": 300,
        "download_rate_limited": 120,
    }.get(error_key)
    if cooldown:
        _BLOCKED_UNTIL[provider_name(url)] = (time.monotonic() + cooldown, error_key)


def clear_provider_failures() -> None:
    """Clear transient circuit state (primarily useful in tests)."""
    _BLOCKED_UNTIL.clear()


def _net_opts() -> dict:
    """Optional yt-dlp network options from env — essential on cloud/datacenter
    hosts (Railway etc.) where YouTube blocks bare requests.
      YTDLP_COOKIES_FILE : path to a Netscape cookies.txt (export from a browser)
      YTDLP_PROXY        : authorized stable proxy URL with unblocked egress
    """
    opts: dict = {}
    cookies = os.getenv("YTDLP_COOKIES_FILE")
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    proxy = os.getenv("YTDLP_PROXY")
    if proxy:
        opts["proxy"] = proxy
    # On datacenter IPs YouTube may withhold formats ("Requested format is not
    # available"). Trying alternate player clients often restores them.
    # e.g. YTDLP_PLAYER_CLIENT="tv,web_safari,android"
    clients = os.getenv("YTDLP_PLAYER_CLIENT")
    if clients:
        opts["extractor_args"] = {
            "youtube": {"player_client": [c.strip() for c in clients.split(",") if c.strip()]}
        }
    request_sleep = os.getenv("YTDLP_SLEEP_REQUESTS")
    if request_sleep:
        try:
            sleep_seconds = float(request_sleep)
        except ValueError as exc:
            raise RuntimeError(
                f"YTDLP_SLEEP_REQUESTS must be a non-negative number, got {request_sleep!r}"
            ) from exc
        if not math.isfinite(sleep_seconds) or sleep_seconds < 0:
            raise RuntimeError(
                f"YTDLP_SLEEP_REQUESTS must be a non-negative number, got {request_sleep!r}"
            )
        if sleep_seconds >= min(
            _provider_job_timeout(), _provider_job_timeout("search")
        ):
            raise RuntimeError(
                "YTDLP_SLEEP_REQUESTS must be lower than both provider timeouts"
            )
        opts["sleep_interval_requests"] = sleep_seconds
    return opts


def _base_opts() -> dict:
    return {
        "quiet": True,
        # Keep progress quiet, but route actionable extractor/EJS/PO-token
        # warnings through the credential-redacting logger above.
        "no_warnings": False,
        "noprogress": True,
        "logger": _YDLLogger(),
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 30,
        **_net_opts(),
    }


def _source_limit_opts(max_bytes: int | None) -> dict:
    """Bound known and streaming source bytes before post-download size checks.

    yt-dlp's max_filesize handles known sizes. The hook is defense in depth for
    manifests/unknown sizes and sums separate video+audio streams.
    """
    if max_bytes is None:
        return {}
    limit = int(max_bytes)
    if limit <= 0:
        raise ValueError("max_bytes must be positive")
    seen: dict[str, int] = {}

    def guard(progress: dict) -> None:
        if progress.get("status") not in {"downloading", "finished"}:
            return
        expected = progress.get("total_bytes") or progress.get("total_bytes_estimate")
        if expected and int(expected) > limit:
            raise DownloadTooLarge("source exceeds configured size limit")
        filename = str(progress.get("filename") or progress.get("tmpfilename") or "stream")
        current = progress.get("downloaded_bytes") or expected or 0
        seen[filename] = max(seen.get(filename, 0), int(current))
        if sum(seen.values()) > limit:
            raise DownloadTooLarge("source exceeds configured size limit")

    return {"max_filesize": limit, "progress_hooks": [guard]}


def _reject_unbounded_media(info: dict, *, incomplete: bool) -> str | None:
    if info.get("availability") in {
        "private", "premium_only", "subscriber_only", "needs_auth",
    }:
        # Cookies are an extractor transport aid, never permission to expose
        # a shared account's private, paid, member, or login-only media.
        return "private or authenticated media is not supported"
    if info.get("is_live") or info.get("live_status") == "is_live":
        return "live streams are not supported"
    raw_limit = (os.getenv("DOWNLOAD_MAX_SECONDS") or "1800").strip()
    try:
        max_seconds = int(raw_limit)
    except ValueError as exc:
        raise RuntimeError(
            f"DOWNLOAD_MAX_SECONDS must be a positive integer, got {raw_limit!r}"
        ) from exc
    if max_seconds <= 0:
        raise RuntimeError(
            f"DOWNLOAD_MAX_SECONDS must be a positive integer, got {raw_limit!r}"
        )
    duration = info.get("duration")
    if duration and float(duration) > max_seconds:
        return "media exceeds the configured maximum duration"
    return None


def is_supported_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in SUPPORTED_HOSTS)


def _first_entry(info: dict) -> dict:
    if not info:
        raise RuntimeError("yt-dlp returned no media information")
    if info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if entries:
            return entries[0]
    return info


def _find_output(work_dir: str, preferred_ext: str) -> str:
    files = [
        str(path)
        for path in Path(work_dir).iterdir()
        if path.is_file() and not path.name.endswith((".part", ".ytdl", ".json"))
    ]
    preferred = [p for p in files if Path(p).suffix.lower() == preferred_ext.lower()]
    candidates = preferred or files
    if not candidates:
        raise FileNotFoundError("yt-dlp completed without a usable output file")
    return max(candidates, key=os.path.getsize)


def _publish_output(source: str, out_dir: str, prefix: str) -> str:
    suffix = Path(source).suffix.lower()
    destination = os.path.join(out_dir, f"{prefix}_{uuid.uuid4().hex[:12]}{suffix}")
    os.replace(source, destination)
    return destination


def _publish_staged_download(
    payload: object, staging_dir: str, out_dir: str, prefix: str,
    max_bytes: int | None = None,
) -> DownloadResult:
    if not isinstance(payload, dict):
        raise RuntimeError("provider worker returned invalid download metadata")
    try:
        result = DownloadResult(**payload)
    except TypeError as exc:
        raise RuntimeError("provider worker returned invalid download metadata") from exc
    staging_root = os.path.realpath(staging_dir)
    source = os.path.realpath(result.path)
    try:
        contained = os.path.commonpath((staging_root, source)) == staging_root
    except ValueError:
        contained = False
    if not contained or not os.path.isfile(source):
        raise RuntimeError("provider worker returned an unsafe output path")
    _assert_output_size(source, max_bytes)
    result.path = _publish_output(source, out_dir, prefix)
    result.ext = Path(result.path).suffix.lstrip(".").lower()
    return result


def _is_telegram_mp4(info: dict, path: str) -> bool:
    if Path(path).suffix.lower() != ".mp4":
        return False
    formats = info.get("requested_formats") or [info]
    video_codecs = {
        str(item.get("vcodec") or "none").lower() for item in formats
        if item.get("vcodec") not in {None, "none"}
    }
    audio_codecs = {
        str(item.get("acodec") or "none").lower() for item in formats
        if item.get("acodec") not in {None, "none"}
    }
    video_ok = bool(video_codecs) and all(
        codec.startswith(("avc1", "h264")) for codec in video_codecs
    )
    audio_ok = not audio_codecs or all(
        codec.startswith(("mp4a", "aac")) for codec in audio_codecs
    )
    return video_ok and audio_ok


def _assert_output_size(path: str, max_bytes: int | None) -> None:
    if max_bytes is not None and os.path.getsize(path) > int(max_bytes):
        raise DownloadTooLarge("converted output exceeds configured size limit")


def _probe_duration(path: str) -> float | None:
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            check=True, capture_output=True, text=True, timeout=30,
        )
        duration = float(completed.stdout.strip())
        return duration if math.isfinite(duration) and duration > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _verify_capped_conversion(
    source: str,
    output: str,
    max_bytes: int | None,
    expected_duration: float | None,
) -> None:
    _assert_output_size(output, max_bytes)
    if max_bytes is None or os.path.getsize(output) < int(max_bytes) * 0.98:
        return
    # ffmpeg's -fs can exit successfully with a truncated file. If it reached
    # the cap, require the output duration to prove the whole media survived.
    try:
        expected = float(expected_duration) if expected_duration is not None else None
    except (TypeError, ValueError):
        expected = None
    if expected is not None and (not math.isfinite(expected) or expected <= 0):
        expected = None
    expected = expected or _probe_duration(source)
    actual = _probe_duration(output)
    tolerance = max(2.0, (expected or 0) * 0.02)
    if expected is None or actual is None or actual + tolerance < expected:
        raise DownloadTooLarge("converted output exceeds configured size limit")


def _ensure_telegram_mp4(
    info: dict, source: str, work_dir: str, max_bytes: int | None = None,
) -> str:
    """Transcode only incompatible fallbacks so sendVideo always gets H.264/AAC."""
    if _is_telegram_mp4(info, source):
        _assert_output_size(source, max_bytes)
        return source
    converted = os.path.join(work_dir, "telegram_compatible.mp4")
    size_limit = ["-fs", str(int(max_bytes))] if max_bytes is not None else []
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-i", source,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", *size_limit, converted,
        ],
        check=True, capture_output=True, timeout=300,
    )
    _verify_capped_conversion(source, converted, max_bytes, info.get("duration"))
    return converted


def _ensure_mp3(
    source: str, work_dir: str, max_bytes: int | None = None,
    expected_duration: float | None = None,
) -> str:
    """Convert provider audio in a bounded child process under our control."""
    if Path(source).suffix.lower() == ".mp3":
        _assert_output_size(source, max_bytes)
        return source
    converted = os.path.join(work_dir, "telegram_audio.mp3")
    size_limit = ["-fs", str(int(max_bytes))] if max_bytes is not None else []
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-i", source, "-vn",
            "-acodec", "libmp3lame", "-b:a", "192k", *size_limit, converted,
        ],
        check=True, capture_output=True, timeout=300,
    )
    _verify_capped_conversion(
        source, converted, max_bytes, expected_duration
    )
    return converted


# ── metadata only (no download) ──────────────────────────
async def extract_meta(url: str) -> MediaMeta:
    payload = await _run_provider_process("extract_meta", url)
    if not isinstance(payload, dict):
        raise RuntimeError("provider worker returned invalid media metadata")
    try:
        return MediaMeta(**payload)
    except TypeError as exc:
        raise RuntimeError("provider worker returned invalid media metadata") from exc


def _extract_meta_sync(url: str) -> MediaMeta:
    if not is_supported_url(url):
        raise ValueError("unsupported media URL")
    opts = {
        **_base_opts(), "noplaylist": True, "skip_download": True,
        "match_filter": _reject_unbounded_media,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = _first_entry(ydl.extract_info(url, download=False))
    heights = sorted(
        {int(f["height"]) for f in (info.get("formats") or []) if f.get("height")}
    )
    return MediaMeta(
        url=info.get("webpage_url") or url,
        title=info.get("title") or "",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        heights=heights,
    )


# ── download a specific video quality ────────────────────
async def download_video_quality(
    url: str, out_dir: str, max_height: int, max_bytes: int | None = None,
) -> DownloadResult:
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".musiqa_job_video_", dir=out_dir
    ) as staging_dir:
        payload = await _run_provider_process(
            "download_quality", url, staging_dir, max_height, max_bytes
        )
        return _publish_staged_download(
            payload, staging_dir, out_dir, "video", max_bytes
        )


def _download_quality_sync(
    url: str, out_dir: str, max_height: int, max_bytes: int | None = None,
) -> DownloadResult:
    if not is_supported_url(url):
        raise ValueError("unsupported media URL")
    if max_height <= 0:
        raise ValueError("max_height must be positive")
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".musiqa_video_", dir=out_dir) as work_dir:
        opts = {
            **_base_opts(),
            **_source_limit_opts(max_bytes),
            "outtmpl": os.path.join(work_dir, "%(id)s.%(ext)s"),
            "format": (
                f"bestvideo[height<={max_height}][ext=mp4][vcodec^=avc1]"
                "+bestaudio[ext=m4a]/"
                f"best[height<={max_height}][ext=mp4][vcodec^=avc1]/"
                f"bestvideo[height<={max_height}]+bestaudio/"
                f"best[height<={max_height}]"
            ),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "match_filter": _reject_unbounded_media,
            "restrictfilenames": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = _first_entry(ydl.extract_info(url, download=True))
        try:
            source = _find_output(work_dir, ".mp4")
        except FileNotFoundError as exc:
            if max_bytes is not None:
                raise DownloadTooLarge(
                    "source exceeds configured size limit"
                ) from exc
            raise
        source = _ensure_telegram_mp4(info, source, work_dir, max_bytes)
        path = _publish_output(source, out_dir, "video")
    return DownloadResult(
        path=path,
        title=info.get("title") or "",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=info.get("duration"),
        ext=os.path.splitext(path)[1].lstrip("."),
    )


# ── download audio (from a URL, or via YouTube search) ───
async def download_audio(
    url: str, out_dir: str, max_bytes: int | None = None,
) -> DownloadResult:
    return await _download_audio_in_worker(url, out_dir, max_bytes)


async def _download_audio_in_worker(
    target: str, out_dir: str, max_bytes: int | None,
) -> DownloadResult:
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".musiqa_job_audio_", dir=out_dir
    ) as staging_dir:
        payload = await _run_provider_process(
            "download_audio", target, staging_dir, max_bytes
        )
        return _publish_staged_download(
            payload, staging_dir, out_dir, "audio", max_bytes
        )


def _download_audio_sync(
    target: str, out_dir: str, max_bytes: int | None = None,
) -> DownloadResult:
    if target.startswith(("http://", "https://")) and not is_supported_url(target):
        raise ValueError("unsupported media URL")
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".musiqa_audio_", dir=out_dir) as work_dir:
        opts = {
            **_base_opts(),
            **_source_limit_opts(max_bytes),
            "outtmpl": os.path.join(work_dir, "%(id)s.%(ext)s"),
            "format": "bestaudio/best",
            "noplaylist": True,
            "match_filter": _reject_unbounded_media,
            "restrictfilenames": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = _first_entry(ydl.extract_info(target, download=True))
        try:
            source = _find_output(work_dir, ".mp3")
        except FileNotFoundError as exc:
            if max_bytes is not None:
                raise DownloadTooLarge(
                    "source exceeds configured size limit"
                ) from exc
            raise
        source = _ensure_mp3(
            source, work_dir, max_bytes, info.get("duration")
        )
        path = _publish_output(source, out_dir, "audio")
    return DownloadResult(
        path=path,
        title=info.get("title") or "",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=info.get("duration"),
        ext="mp3",
    )
