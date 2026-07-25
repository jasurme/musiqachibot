"""Unit tests for pure helpers — no bot, no network."""
import asyncio
import os
import stat
import subprocess
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from bot.db.storage import Storage
from bot.config import load_config
from bot.i18n import SUPPORTED, TRANSLATIONS, t
from bot.services import downloader
from bot.services.recognizer import FallbackRecognizer, Track
from bot.services.search import SearchItem, _clean_title
from bot.services.video import make_video_note


# ── i18n ─────────────────────────────────────────────────
def test_config_validates_and_uses_default_locale(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("DEFAULT_LOCALE", "RU")
    monkeypatch.setenv("LOCAL_BOT_API_URL", "http://localhost:8081")
    monkeypatch.setenv("MAX_FILE_MB", "75")
    monkeypatch.setenv("MAX_INPUT_MB", "19")
    monkeypatch.setenv("DOWNLOAD_MAX_SECONDS", "900")
    monkeypatch.setenv("YTDLP_JOB_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("YTDLP_METADATA_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("YTDLP_SLEEP_REQUESTS", "0.5")
    monkeypatch.setenv("YTDLP_CONCURRENCY", "2")
    monkeypatch.setenv("HEAVY_JOB_CONCURRENCY", "4")
    monkeypatch.setenv("SEARCH_MAX_SECONDS", "1000")
    monkeypatch.setenv("SEARCH_CACHE_SECONDS", "0")
    monkeypatch.setenv("PRIVACY_POLICY_URL", "https://example.com/privacy")
    monkeypatch.setenv("DROP_PENDING_UPDATES", "true")
    config = load_config()
    assert config.default_locale == "ru"
    assert config.max_file_mb == 75
    assert config.max_input_mb == 19
    assert config.download_max_seconds == 900
    assert config.ytdlp_job_timeout_seconds == 120
    assert config.ytdlp_metadata_timeout_seconds == 60
    assert config.ytdlp_sleep_requests == 0.5
    assert config.ytdlp_concurrency == 2
    assert config.heavy_job_concurrency == 4
    assert config.search_max_seconds == 1000
    assert config.search_cache_seconds == 0
    assert config.privacy_policy_url == "https://example.com/privacy"
    assert config.drop_pending_updates is True


def test_config_rejects_invalid_max_size(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("MAX_FILE_MB", "zero")
    with pytest.raises(RuntimeError, match="MAX_FILE_MB"):
        load_config()


@pytest.mark.parametrize("name", [
    "DOWNLOAD_MAX_SECONDS", "YTDLP_JOB_TIMEOUT_SECONDS",
    "YTDLP_METADATA_TIMEOUT_SECONDS",
])
def test_config_rejects_invalid_provider_limits(monkeypatch, name):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv(name, "invalid")
    with pytest.raises(RuntimeError, match=name):
        load_config()


@pytest.mark.parametrize("value", ["invalid", "nan", "-1"])
def test_config_rejects_invalid_request_sleep(monkeypatch, value):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("YTDLP_SLEEP_REQUESTS", value)
    with pytest.raises(RuntimeError, match="YTDLP_SLEEP_REQUESTS"):
        load_config()


def test_config_rejects_excessive_provider_concurrency(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("YTDLP_CONCURRENCY", "9")
    with pytest.raises(RuntimeError, match="YTDLP_CONCURRENCY"):
        load_config()


def test_config_rejects_excessive_heavy_job_concurrency(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("HEAVY_JOB_CONCURRENCY", "9")
    with pytest.raises(RuntimeError, match="HEAVY_JOB_CONCURRENCY"):
        load_config()


def test_heavy_job_guard_distinguishes_duplicate_from_global_capacity():
    from bot import jobs

    jobs.configure(2)
    assert jobs.try_claim(1) is None
    assert jobs.try_claim(1) == "already_processing"
    assert jobs.try_claim(2) is None
    assert jobs.try_claim(3) == "service_busy"
    jobs.release(1)
    assert jobs.try_claim(3) is None


@pytest.mark.parametrize("name", ["SEARCH_MAX_SECONDS", "SEARCH_CACHE_SECONDS"])
def test_config_rejects_invalid_search_limits(monkeypatch, name):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv(name, "invalid")
    with pytest.raises(RuntimeError, match=name):
        load_config()


def test_config_rejects_cloud_limits_above_telegram_caps(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.delenv("LOCAL_BOT_API_URL", raising=False)
    monkeypatch.setenv("MAX_FILE_MB", "51")
    with pytest.raises(RuntimeError, match="LOCAL_BOT_API_URL"):
        load_config()


async def test_local_bot_api_session_uses_absolute_path_mode(config, tmp_path):
    from bot.main import _build_telegram_session

    local_config = replace(config, local_api_url="http://telegram-bot-api:8081")
    session = _build_telegram_session(local_config)
    try:
        absolute = str(tmp_path / "telegram-file.mp3")
        assert session.api.is_local is True
        assert session.api.wrap_local_file.to_local(absolute) == absolute
    finally:
        await session.close()


def test_all_locales_have_same_keys():
    base = set(TRANSLATIONS["en"])
    for loc in SUPPORTED:
        assert set(TRANSLATIONS[loc]) == base, f"{loc} key mismatch"


def test_t_falls_back_to_default_locale():
    assert t("welcome", "zz").startswith("👋")  # unknown locale → uz default


def test_t_formats_placeholders():
    out = t("rec_header", "en", title="Believer", artist="Imagine Dragons")
    assert "Believer" in out and "Imagine Dragons" in out


def test_t_missing_kwarg_does_not_crash():
    # rec_header needs title+artist; omitting them must not raise
    assert isinstance(t("rec_header", "en"), str)


# ── URL support detection ────────────────────────────────
@pytest.mark.parametrize("url", [
    "https://www.tiktok.com/@x/video/1",
    "https://youtu.be/abc",
    "https://www.youtube.com/watch?v=abc",
    "https://instagram.com/reel/x/",
    "https://vm.tiktok.com/xyz",
])
def test_supported_urls(url):
    assert downloader.is_supported_url(url)


@pytest.mark.parametrize("url", [
    "https://example.com/x",
    "https://notyoutube.com/watch",
    "ftp://youtube.com",
    "not a url",
])
def test_unsupported_urls(url):
    assert not downloader.is_supported_url(url)


def test_download_error_classifies_railway_youtube_bot_check():
    exc = RuntimeError("Sign in to confirm you’re not a bot. Use --cookies")
    assert downloader.download_error_key(exc) == "download_blocked"


def test_provider_circuit_breaker_stops_immediate_retries():
    url = "https://www.youtube.com/watch?v=abc"
    error = downloader.yt_dlp.utils.DownloadError("HTTP Error 403: Forbidden")
    downloader.record_provider_failure(url, error)
    assert downloader.provider_error_key(url) == "download_blocked"


def test_non_provider_error_does_not_trip_source_circuit():
    url = "https://www.youtube.com/watch?v=abc"
    downloader.record_provider_failure(url, RuntimeError("HTTP Error 403: Telegram"))
    assert downloader.provider_error_key(url) is None


def test_ytdlp_options_suppress_terminal_progress():
    opts = downloader._base_opts()
    assert opts["quiet"] is True
    assert opts["noprogress"] is True
    assert opts["no_warnings"] is False
    assert opts["retries"] >= 3


def test_audio_fallback_never_selects_unrestricted_full_video():
    assert downloader._AUDIO_FORMAT_SELECTOR == (
        "bestaudio/"
        "best[acodec!=none][height<=360]/"
        "worst[acodec!=none]"
    )


def test_cookie_mode_ignores_forced_player_clients(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(cookies))
    monkeypatch.setenv("YTDLP_PLAYER_CLIENT", "tv,web_safari,android")

    opts = downloader._net_opts()

    assert opts["cookiefile"] == str(cookies)
    assert "extractor_args" not in opts
    assert any(
        "YTDLP_PLAYER_CLIENT is ignored" in warning
        for warning in downloader.runtime_warnings()
    )


def test_cookieless_mode_can_use_explicit_player_clients(monkeypatch):
    monkeypatch.delenv("YTDLP_COOKIES_FILE", raising=False)
    monkeypatch.setenv("YTDLP_PLAYER_CLIENT", "tv, web_safari")

    assert downloader._net_opts()["extractor_args"] == {
        "youtube": {"player_client": ["tv", "web_safari"]}
    }


def test_provider_log_redaction_removes_secret_url_paths():
    message = downloader.safe_error_message(
        "failed https://api.telegram.org/file/bot123456:secret/audio/file.mp3?sig=x"
    )
    assert message == "failed https://api.telegram.org/<redacted>"
    assert "secret" not in message and "sig=" not in message
    proxy = downloader.safe_error_message(
        "proxy socks5://proxy-user:proxy-pass@example.com:1080 failed"
    )
    assert proxy == "proxy socks5://example.com:1080/<redacted> failed"
    assert "proxy-pass" not in proxy


def test_streaming_source_guard_sums_separate_formats():
    opts = downloader._source_limit_opts(100)
    guard = opts["progress_hooks"][0]
    guard({"status": "downloading", "filename": "video", "downloaded_bytes": 60})
    with pytest.raises(downloader.DownloadTooLarge):
        guard({"status": "downloading", "filename": "audio", "downloaded_bytes": 41})


def test_streaming_source_guard_does_not_trust_fragment_size_estimates():
    opts = downloader._source_limit_opts(100)
    guard = opts["progress_hooks"][0]

    # FragmentDownloader extrapolates this value from the fragments received
    # so far. A large initial fragment can make the estimate much larger than
    # the eventual media file, so only bytes actually transferred are counted.
    guard({
        "status": "downloading", "filename": "audio",
        "downloaded_bytes": 20, "total_bytes_estimate": 500,
    })
    guard({
        "status": "finished", "filename": "audio",
        "downloaded_bytes": 100, "total_bytes_estimate": 500,
    })
    with pytest.raises(downloader.DownloadTooLarge):
        guard({
            "status": "downloading", "filename": "audio",
            "downloaded_bytes": 101, "total_bytes_estimate": 500,
        })


def test_streaming_source_guard_rejects_known_oversized_content_length():
    guard = downloader._source_limit_opts(100)["progress_hooks"][0]
    with pytest.raises(downloader.DownloadTooLarge):
        guard({
            "status": "downloading", "filename": "audio",
            "downloaded_bytes": 1, "total_bytes": 101,
        })


async def test_provider_admission_rejects_an_unbounded_queue(monkeypatch):
    monkeypatch.setattr(downloader, "_ADMISSION_LIMIT", 1)
    monkeypatch.setattr(downloader, "_ADMITTED", 1)
    with pytest.raises(downloader.ProviderBusy):
        await downloader._run_provider_process("extract_meta", "https://youtu.be/x")


async def test_provider_worker_error_roundtrip_releases_admission(monkeypatch):
    monkeypatch.setenv("YTDLP_METADATA_TIMEOUT_SECONDS", "10")
    with pytest.raises(ValueError, match="unsupported media URL"):
        await downloader._run_provider_process(
            "extract_meta", "https://example.com/not-supported"
        )
    assert downloader._ADMITTED == 0


async def test_invalid_provider_timeout_does_not_leak_admission(monkeypatch):
    monkeypatch.setenv("YTDLP_METADATA_TIMEOUT_SECONDS", "invalid")
    before_tasks = set(downloader._PROVIDER_TASKS)
    with pytest.raises(RuntimeError, match="positive integer"):
        await downloader._run_provider_process(
            "extract_meta", "https://example.com/not-supported"
        )
    assert downloader._ADMITTED == 0
    assert downloader._PROVIDER_TASKS == before_tasks


async def test_completed_cleanup_still_reports_same_tick_cancellation():
    async def immediate_cleanup():
        return "clean"

    task = asyncio.create_task(
        downloader._run_uninterruptible_cleanup(immediate_cleanup())
    )
    asyncio.get_running_loop().call_soon(task.cancel)
    result, interrupted = await task
    assert result == "clean"
    assert interrupted is True
    assert task.cancelling() == 0


async def test_spawn_timeout_kills_child_created_at_timeout_boundary(monkeypatch):
    class FakeProcess:
        pid = 43210

    process = FakeProcess()

    async def completed_spawn():
        return process

    spawn_task = asyncio.create_task(completed_spawn())
    real_wait_for = asyncio.wait_for
    wait_calls = 0

    async def boundary_wait_for(awaitable, timeout):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls <= 2:
            await asyncio.sleep(0)
            raise asyncio.TimeoutError
        return await real_wait_for(awaitable, timeout=timeout)

    killed = []

    async def record_kill(target):
        killed.append(target)

    monkeypatch.setattr(downloader.asyncio, "wait_for", boundary_wait_for)
    monkeypatch.setattr(downloader, "_kill_process_group", record_kill)
    with pytest.raises(downloader.ProviderTimeout):
        await downloader._await_worker_spawn(spawn_task, 0.001)
    assert killed == [process]


async def test_provider_shutdown_waits_for_inflight_spawn(monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()

    async def delayed_spawn(*args, **kwargs):
        spawn_started.set()
        await allow_spawn.wait()
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(downloader.asyncio, "create_subprocess_exec", delayed_spawn)
    monkeypatch.setattr(downloader, "_CLOSING", False)
    job = asyncio.create_task(
        downloader._run_provider_process(
            "extract_meta", "https://example.com/not-supported"
        )
    )
    await asyncio.wait_for(spawn_started.wait(), timeout=2)
    shutdown = asyncio.create_task(downloader.shutdown_provider_workers())
    await asyncio.sleep(0.05)
    assert not shutdown.done()
    allow_spawn.set()
    await asyncio.wait_for(shutdown, timeout=5)
    assert job.done()
    assert downloader._ADMITTED == 0
    assert not downloader._ACTIVE_PROCESSES


async def test_provider_deadline_kills_worker_process_group(tmp_path):
    marker = tmp_path / "orphaned-grandchild"
    ready = tmp_path / "grandchild-ready"
    child_code = (
        "import pathlib,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(1.8); "
        "pathlib.Path(sys.argv[2]).write_text('orphan')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]]); "
        "time.sleep(30)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", parent_code, child_code, str(ready), str(marker),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    assert ready.exists()
    started = time.monotonic()
    with pytest.raises(downloader.ProviderTimeout):
        await downloader._communicate_with_deadline(process, 0.2)
    assert time.monotonic() - started < 3
    assert process.returncode is not None
    await asyncio.sleep(0.8)
    assert not marker.exists()


async def test_repeated_cancellation_cannot_interrupt_worker_cleanup(tmp_path):
    ready = tmp_path / "stubborn-ready"
    marker = tmp_path / "stubborn-survived"
    worker_code = (
        "import pathlib,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(1.8); "
        "pathlib.Path(sys.argv[2]).write_text('survived')"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", worker_code, str(ready), str(marker),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    task = asyncio.create_task(
        downloader._communicate_with_deadline(process, 30)
    )
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
    await asyncio.sleep(0.8)
    assert not marker.exists()


async def test_cancelled_provider_download_removes_staging(monkeypatch, tmp_path):
    async def cancelled(operation, target, staging_dir, max_bytes):
        Path(staging_dir, "partial.webm").write_bytes(b"partial")
        raise asyncio.CancelledError

    monkeypatch.setattr(downloader, "_run_provider_process", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await downloader.download_audio("https://youtu.be/x", str(tmp_path))
    assert list(tmp_path.iterdir()) == []


async def test_successful_provider_download_publishes_after_worker(monkeypatch, tmp_path):
    async def success(operation, target, staging_dir, max_bytes):
        staged = Path(staging_dir, "worker.mp3")
        staged.write_bytes(b"media")
        return {
            "path": str(staged), "title": "Song", "uploader": "Artist",
            "duration": 3, "ext": "mp3",
        }

    monkeypatch.setattr(downloader, "_run_provider_process", success)
    result = await downloader.download_audio("https://youtu.be/x", str(tmp_path))
    assert Path(result.path).parent == tmp_path
    assert Path(result.path).read_bytes() == b"media"
    assert not list(tmp_path.glob(".musiqa_job_audio_*"))


def test_parent_rejects_oversized_worker_output(tmp_path):
    staging = tmp_path / "stage"
    staging.mkdir()
    output = staging / "worker.mp3"
    output.write_bytes(b"x" * 101)
    payload = {
        "path": str(output), "title": "Song", "uploader": "Artist",
        "duration": 3, "ext": "mp3",
    }
    with pytest.raises(downloader.DownloadTooLarge, match="size limit"):
        downloader._publish_staged_download(
            payload, str(staging), str(tmp_path), "audio", max_bytes=100
        )


def test_video_transcode_has_hard_output_cap(monkeypatch, tmp_path):
    source = tmp_path / "source.webm"
    source.write_bytes(b"source")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"x" * 101)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(downloader.subprocess, "run", fake_run)
    with pytest.raises(downloader.DownloadTooLarge, match="size limit"):
        downloader._ensure_telegram_mp4(
            {"duration": 8, "vcodec": "vp9"}, str(source), str(tmp_path), 100
        )
    assert "-fs" in commands[0]
    assert commands[0][commands[0].index("-fs") + 1] == "100"


@pytest.mark.parametrize(
    "availability", ["private", "premium_only", "subscriber_only", "needs_auth"]
)
def test_authenticated_media_is_rejected_even_with_shared_cookies(availability):
    reason = downloader._reject_unbounded_media(
        {"availability": availability, "duration": 1}, incomplete=False
    )
    assert "private or authenticated" in reason
    assert downloader.download_error_key(RuntimeError(reason)) == "download_private"


def test_provider_worker_env_is_private_and_copies_cookies(monkeypatch, tmp_path):
    source = tmp_path / "source-cookies.txt"
    source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    monkeypatch.setenv("BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("YTDLP_COOKIES_CONTENT", "cookie-secret")
    monkeypatch.setenv("YTDLP_COOKIES_FILE", str(source))
    env = downloader._private_worker_env(str(job_dir))
    private = Path(env["YTDLP_COOKIES_FILE"])
    assert "BOT_TOKEN" not in env and "YTDLP_COOKIES_CONTENT" not in env
    assert private.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert stat.S_IMODE(private.stat().st_mode) == 0o600


def test_stale_runtime_cleanup_is_narrow_and_age_bounded(tmp_path):
    downloads = tmp_path / "downloads"
    runtime_temp = tmp_path / "tmp"
    downloads.mkdir()
    runtime_temp.mkdir()
    stale_job = downloads / ".musiqa_job_audio_old"
    stale_job.mkdir()
    stale_output = downloads / "audio_012345abcdef.mp3"
    stale_output.write_bytes(b"old")
    unrelated = downloads / "keep-me.mp3"
    unrelated.write_bytes(b"safe")
    recent_job = downloads / ".musiqa_round_recent"
    recent_job.mkdir()
    stale_cookie = runtime_temp / "musiqa_yt_cookies_old.txt"
    stale_cookie.write_text("secret", encoding="utf-8")
    old_time = time.time() - 120
    for path in (stale_job, stale_output, unrelated, stale_cookie):
        os.utime(path, (old_time, old_time))

    removed = downloader.cleanup_stale_runtime_files(
        str(downloads), max_age_seconds=60, temp_dir=str(runtime_temp)
    )
    assert removed == 3
    assert not stale_job.exists() and not stale_output.exists()
    assert not stale_cookie.exists()
    assert unrelated.exists() and recent_job.exists()


def test_provider_timeout_does_not_open_source_circuit():
    url = "https://www.youtube.com/watch?v=abc"
    downloader.record_provider_failure(url, downloader.ProviderTimeout("timed out"))
    assert downloader.provider_error_key(url) is None


def test_supported_url_parser_strips_message_punctuation():
    from bot.handlers.url_download import _first_supported_url
    assert _first_supported_url("Watch (https://youtu.be/abc).") == "https://youtu.be/abc"


# ── title cleaning ───────────────────────────────────────
@pytest.mark.parametrize("raw,clean", [
    ("Ummon - Xiyonat | Уммон - Хиёнат (AUDIO)", "Ummon - Xiyonat"),
    ("Ummon - Qanday unutding | Уммон", "Ummon - Qanday unutding"),
    ("Artist - Song (Official Music Video)", "Artist - Song"),
    ("Artist - Song [HD]", "Artist - Song"),
    ("Plain Title", "Plain Title"),
    ("Song (Remix)", "Song (Remix)"),  # remix kept — not a junk tag
])
def test_clean_title(raw, clean):
    assert _clean_title(raw) == clean


def test_search_item_url():
    it = SearchItem(video_id="abc123", title="t", duration=100, uploader="u")
    assert it.url == "https://www.youtube.com/watch?v=abc123"


async def test_search_cache_reuses_normalized_query(monkeypatch):
    from bot.services import search
    calls = {"n": 0}

    async def fake_search(query, limit):
        calls["n"] += 1
        return [SearchItem("v", "Song", 10, "Artist")]

    monkeypatch.setattr(search, "_run_search_worker", fake_search)
    first, second = await asyncio.gather(
        search.search_tracks("  Artist   Song ", 5),
        search.search_tracks("artist song", 5),
    )
    assert first == second
    assert calls["n"] == 1


def test_search_filters_out_long_albums():
    from bot.services.search import _entries_to_items
    entries = [
        {"id": "song", "title": "Ummon - Qanday unutding", "duration": 244},
        {"id": "album", "title": "Ummon - Full Album 2015", "duration": 5580},  # 93 min
        {"id": "nodur", "title": "Unknown length", "duration": None},
        {"id": "", "title": "no id", "duration": 100},  # dropped: no id
    ]
    ids = [it.video_id for it in _entries_to_items(entries, max_seconds=1200)]
    assert "song" in ids
    assert "album" not in ids     # 93-min album filtered → no 128 MB "too big"
    assert "nodur" in ids         # unknown duration kept
    assert "" not in ids


def test_cookies_content_materialized(monkeypatch):
    from bot.main import _materialize_cookies
    from bot.services.downloader import _net_opts
    monkeypatch.delenv("YTDLP_COOKIES_FILE", raising=False)
    monkeypatch.setenv("YTDLP_COOKIES_CONTENT", "# Netscape HTTP Cookie File\n")
    try:
        _materialize_cookies()
        path = os.environ.get("YTDLP_COOKIES_FILE")
        assert path and os.path.exists(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert _net_opts().get("cookiefile") == path  # picked up by yt-dlp opts
    finally:
        leaked = os.environ.pop("YTDLP_COOKIES_FILE", None)
        if leaked and os.path.exists(leaked):
            os.remove(leaked)


def test_telegram_video_compatibility_requires_mp4_h264_aac(tmp_path):
    mp4 = str(tmp_path / "clip.mp4")
    compatible = {
        "requested_formats": [
            {"vcodec": "avc1.64001f", "acodec": "none"},
            {"vcodec": "none", "acodec": "mp4a.40.2"},
        ]
    }
    incompatible = {
        "requested_formats": [
            {"vcodec": "vp9", "acodec": "none"},
            {"vcodec": "none", "acodec": "opus"},
        ]
    }
    assert downloader._is_telegram_mp4(compatible, mp4)
    assert not downloader._is_telegram_mp4(incompatible, mp4)
    assert not downloader._is_telegram_mp4(compatible, str(tmp_path / "clip.webm"))


def test_provider_audio_conversion_produces_mp3(tmp_path):
    source = str(tmp_path / "source.wav")
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=0.2", source,
        ],
        check=True, capture_output=True, timeout=10,
    )
    output = downloader._ensure_mp3(source, str(tmp_path))
    assert output.endswith(".mp3")
    assert os.path.isfile(output) and os.path.getsize(output) > 1000


# ── round video-note conversion (real ffmpeg, no network) ─
async def test_make_video_note_is_square(tmp_path):
    src = str(tmp_path / "src.mp4")
    # synthesize a 2s NON-square clip locally with ffmpeg
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=15:duration=2",
         "-pix_fmt", "yuv420p", src],
        check=True, capture_output=True,
    )
    note = await make_video_note(src, str(tmp_path), size=480)
    assert os.path.exists(note) and os.path.getsize(note) > 1000
    dims = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", note],
        capture_output=True, text=True,
    ).stdout.strip()
    assert dims.replace(" ", "") == "480,480"  # cropped to a centred square


# ── storage ──────────────────────────────────────────────
async def test_storage_locale_roundtrip():
    s = Storage(":memory:")
    await s.init()
    assert await s.get_locale(1) is None
    await s.set_locale(1, "ru")
    assert await s.get_locale(1) == "ru"
    await s.set_locale(1, "en")  # upsert
    assert await s.get_locale(1) == "en"
    await s.close()


async def test_storage_audio_cache_roundtrip():
    s = Storage(":memory:")
    await s.init()
    assert await s.get_cached_audio("k") is None
    await s.set_cached_audio("k", "FILEID", "Some Title")
    assert await s.get_cached_audio("k") == "FILEID"
    await s.set_cached_audio("k", "FILEID2")  # replace
    assert await s.get_cached_audio("k") == "FILEID2"
    await s.delete_cached_audio("k")
    assert await s.get_cached_audio("k") is None
    await s.close()


async def test_storage_recognition_cache_roundtrip():
    s = Storage(":memory:")
    await s.init()
    assert await s.get_recognition("file-1") is None
    await s.set_recognition(
        "file-1", "Believer", "Imagine Dragons",
        "https://www.shazam.com/track/1", "https://example.com/cover.jpg",
    )
    cached = await s.get_recognition("file-1")
    assert cached == {
        "title": "Believer", "artist": "Imagine Dragons",
        "url": "https://www.shazam.com/track/1",
        "cover": "https://example.com/cover.jpg",
    }
    await s.close()


async def test_storage_deletes_only_data_owned_by_requesting_user():
    s = Storage(":memory:")
    await s.init()
    await s.set_locale(1, "en")
    await s.set_locale(2, "ru")
    await s.save_session("one", {"owner_user_id": 1, "url": "https://example.com/1"})
    await s.save_session("two", {"owner_user_id": 2, "url": "https://example.com/2"})
    await s.set_recognition("r1", "One", "Artist", owner_user_id=1)
    await s.set_recognition("r2", "Two", "Artist", owner_user_id=2)

    removed = await s.delete_user_data(1)

    assert removed == 3
    assert await s.get_locale(1) is None
    assert await s.get_locale(2) == "ru"
    assert await s.get_session("one") is None
    assert await s.get_session("two") is not None
    assert await s.get_recognition("r1") is None
    assert await s.get_recognition("r2") is not None
    await s.close()


async def test_fallback_recognizer_uses_second_provider():
    class NoMatch:
        async def recognize(self, path):
            return None

    class Match:
        async def recognize(self, path):
            return Track(title="Song", artist="Artist")

    track = await FallbackRecognizer([NoMatch(), Match()]).recognize("sample.mp3")
    assert track and track.query == "Artist Song"


def test_deployment_includes_current_youtube_solver_runtime():
    root = Path(__file__).resolve().parent.parent
    requirements = (root / "requirements.txt").read_text()
    dockerfile = (root / "Dockerfile").read_text()
    assert "yt-dlp[default]==2026.7.4" in requirements
    assert "denoland/deno:bin-2.9.3" in dockerfile
    assert "import yt_dlp_ejs" in dockerfile
    assert "ffmpeg tini" in dockerfile
    assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in dockerfile
    entrypoint = (root / "docker-entrypoint.sh").read_text()
    assert entrypoint.count("/usr/bin/tini --") == 2
    assert entrypoint.count("--no-new-privs") == 2
    assert entrypoint.index("install -d /data /data/downloads") < entrypoint.index(
        "chown 101:101 /data /data/downloads"
    )


def test_railway_deploy_durations_are_numeric():
    root = Path(__file__).resolve().parent.parent
    with (root / "railway.toml").open("rb") as config_file:
        deploy = tomllib.load(config_file)["deploy"]

    assert deploy["overlapSeconds"] == 0
    assert deploy["drainingSeconds"] == 15
    assert type(deploy["overlapSeconds"]) is int
    assert type(deploy["drainingSeconds"]) is int


# ── quality keyboard (Feature D) ─────────────────────────
_ = lambda k, **kw: k  # noqa: E731 — stub translator for keyboard tests


def test_quality_keyboard_tiers_audio_and_find_music():
    from bot.handlers.url_download import _quality_keyboard
    kb = _quality_keyboard("tok", [144, 360, 480, 720, 1080], _)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "dl:tok:360" in datas
    assert "dl:tok:720" in datas
    assert "dl:tok:1080" in datas
    assert "dl:tok:audio" in datas
    assert "dl:tok:music" in datas  # the "Find music" button
    assert "dl:tok:round" in datas  # the "Yumaloq video" button


def test_quality_keyboard_caps_to_available_height():
    from bot.handlers.url_download import _quality_keyboard
    kb = _quality_keyboard("tok", [240, 360], _)  # max 360
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "dl:tok:360" in datas
    assert "dl:tok:720" not in datas  # not offered above source resolution
    assert "dl:tok:audio" in datas and "dl:tok:music" in datas


def test_quality_keyboard_no_heights_shows_all():
    from bot.handlers.url_download import _quality_keyboard
    kb = _quality_keyboard("tok", [], _)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "dl:tok:360" in datas and "dl:tok:audio" in datas and "dl:tok:music" in datas


# ── results list rendering (shared A/C) ──────────────────
def _items(n):
    return [SearchItem(video_id=f"v{i}", title=f"Song {i}", duration=180 + i, uploader="ch")
            for i in range(n)]


def test_results_list_text_numbers_and_durations():
    from bot.handlers.results import _list_text
    sess = {"header": "<b>q</b>", "items": _items(3), "per_page": 10}
    text = _list_text(sess, 0)
    assert "<b>1.</b>" in text and "<b>3.</b>" in text
    assert "3:00" in text  # 180s


def test_results_keyboard_pick_and_paging():
    from bot.handlers.results import _kb
    sess = {"header": "h", "items": _items(30), "per_page": 10}
    kb = _kb("tok", sess, 0, lambda k, **kw: k)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "pick:tok:0" in datas and "pick:tok:9" in datas
    assert "page:tok:1" in datas  # next page exists
    assert "page:tok:-1" not in datas  # no prev on page 0


def test_results_keyboard_extras_lyrics_video():
    from bot.handlers.results import _kb
    sess = {"header": "h", "items": _items(5), "per_page": 5, "extras": True}
    kb = _kb("tok", sess, 0, lambda k, **kw: k)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "lyr:tok" in datas and "vid:tok" in datas
    assert "pick:tok:0" in datas
    assert not any(d.startswith("page:") for d in datas)  # only 5 items, no paging


def test_results_keyboard_adds_recognition_listen_link():
    from bot.handlers.results import _kb
    sess = {
        "header": "h", "items": _items(1), "per_page": 5, "extras": True,
        "listen_url": "https://www.shazam.com/track/1",
    }
    kb = _kb("tok", sess, 0, lambda k, **kw: k)
    assert any(
        button.url == "https://www.shazam.com/track/1"
        for row in kb.inline_keyboard for button in row
    )


def test_results_page2_has_back_button():
    from bot.handlers.results import _kb
    sess = {"header": "h", "items": _items(30), "per_page": 10}
    kb = _kb("tok", sess, 1, lambda k, **kw: k)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "page:tok:0" in datas  # back
    assert "pick:tok:10" in datas and "pick:tok:19" in datas
