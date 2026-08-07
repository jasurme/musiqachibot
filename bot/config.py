import math
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

SUPPORTED_LOCALES = ("uz", "ru", "en")


@dataclass(frozen=True)
class Config:
    bot_token: str
    local_api_url: str | None
    default_locale: str
    download_dir: str
    max_file_mb: int
    # Optional paid recognition fallback after Shazamio.
    audd_token: str | None
    # sqlite path — point at a mounted volume in production (e.g. /data/musiqa.db)
    db_path: str = "musiqa.db"
    # Keep queued Telegram updates across normal restarts by default.
    drop_pending_updates: bool = False
    # Standard Telegram getFile is limited to 20 MB. This is intentionally
    # separate from the standard 50 MB outbound upload limit above.
    max_input_mb: int = 20
    # Reject unexpectedly long source media before downloading/transcoding it.
    download_max_seconds: int = 1800
    # Kill the complete extractor process (including ffmpeg children) if a
    # provider job stops making bounded progress.
    ytdlp_job_timeout_seconds: int = 600
    ytdlp_metadata_timeout_seconds: int = 90
    ytdlp_sleep_requests: float = 0.0
    ytdlp_concurrency: int = 3
    # Bounds all expensive user pipelines (provider, ffmpeg, recognition).
    heavy_job_concurrency: int = 3
    search_max_seconds: int = 1200
    search_cache_seconds: int = 3600
    privacy_policy_url: str | None = None
    # Messages sent to the bot by this private-chat user are copied to every
    # active private user. Keep the default requested by the bot operator while
    # allowing an environment override for future ownership changes.
    admin_user_id: int = 7645204689
    # Stay below Telegram's documented bulk-send ceiling and send sequentially.
    broadcast_rate_per_second: int = 20
    # Uzbekistan Top 10 is refreshed off the request path. Users always read
    # the last complete SQLite snapshot, so /top_music stays instant.
    top_music_refresh_hours: int = 48
    top_music_retry_minutes: int = 30


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _positive_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _nonnegative_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a non-negative integer, got {raw!r}") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be a non-negative integer, got {raw!r}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false, got {raw!r}")


def _nonnegative_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a non-negative number, got {raw!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise RuntimeError(f"{name} must be a non-negative number, got {raw!r}")
    return value


def load_config() -> Config:
    token = _clean(os.getenv("BOT_TOKEN"))
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is not set. Put `BOT_TOKEN=...` in your .env file "
            "(get one from @BotFather)."
        )
    default_locale = os.getenv("DEFAULT_LOCALE", "uz").strip().lower() or "uz"
    if default_locale not in SUPPORTED_LOCALES:
        supported = ", ".join(SUPPORTED_LOCALES)
        raise RuntimeError(
            f"DEFAULT_LOCALE must be one of {supported}, got {default_locale!r}"
        )
    local_api_url = _clean(os.getenv("LOCAL_BOT_API_URL"))
    max_file_mb = _positive_int("MAX_FILE_MB", 50)
    max_input_mb = _positive_int("MAX_INPUT_MB", 20)
    download_max_seconds = _positive_int("DOWNLOAD_MAX_SECONDS", 1800)
    ytdlp_job_timeout_seconds = _positive_int("YTDLP_JOB_TIMEOUT_SECONDS", 600)
    ytdlp_metadata_timeout_seconds = _positive_int(
        "YTDLP_METADATA_TIMEOUT_SECONDS", 90
    )
    ytdlp_sleep_requests = _nonnegative_float("YTDLP_SLEEP_REQUESTS", 0)
    ytdlp_concurrency = _positive_int("YTDLP_CONCURRENCY", 3)
    heavy_job_concurrency = _positive_int("HEAVY_JOB_CONCURRENCY", 3)
    search_max_seconds = _positive_int("SEARCH_MAX_SECONDS", 1200)
    search_cache_seconds = _nonnegative_int("SEARCH_CACHE_SECONDS", 3600)
    admin_user_id = _positive_int("ADMIN_USER_ID", 7645204689)
    broadcast_rate_per_second = _positive_int(
        "BROADCAST_RATE_PER_SECOND", 20
    )
    top_music_refresh_hours = _positive_int("TOP_MUSIC_REFRESH_HOURS", 48)
    top_music_retry_minutes = _positive_int("TOP_MUSIC_RETRY_MINUTES", 30)
    if ytdlp_concurrency > 8:
        raise RuntimeError("YTDLP_CONCURRENCY cannot exceed 8")
    if heavy_job_concurrency > 8:
        raise RuntimeError("HEAVY_JOB_CONCURRENCY cannot exceed 8")
    if broadcast_rate_per_second > 25:
        raise RuntimeError("BROADCAST_RATE_PER_SECOND cannot exceed 25")
    if top_music_refresh_hours > 24 * 30:
        raise RuntimeError("TOP_MUSIC_REFRESH_HOURS cannot exceed 720")
    if top_music_retry_minutes >= top_music_refresh_hours * 60:
        raise RuntimeError(
            "TOP_MUSIC_RETRY_MINUTES must be shorter than the refresh interval"
        )
    if ytdlp_sleep_requests >= min(
        ytdlp_job_timeout_seconds, ytdlp_metadata_timeout_seconds
    ):
        raise RuntimeError(
            "YTDLP_SLEEP_REQUESTS must be lower than both provider timeouts"
        )
    if local_api_url is None and max_file_mb > 50:
        raise RuntimeError(
            "MAX_FILE_MB cannot exceed 50 without LOCAL_BOT_API_URL"
        )
    if local_api_url is None and max_input_mb > 20:
        raise RuntimeError(
            "MAX_INPUT_MB cannot exceed 20 without LOCAL_BOT_API_URL"
        )
    if max_file_mb > 2000 or max_input_mb > 2000:
        raise RuntimeError("Telegram media limits cannot exceed 2000 MB")
    return Config(
        bot_token=token,
        local_api_url=local_api_url,
        default_locale=default_locale,
        download_dir=os.getenv("DOWNLOAD_DIR", "downloads").strip() or "downloads",
        max_file_mb=max_file_mb,
        audd_token=_clean(os.getenv("AUDD_TOKEN")),
        db_path=os.getenv("DB_PATH", "musiqa.db").strip() or "musiqa.db",
        drop_pending_updates=_bool("DROP_PENDING_UPDATES"),
        max_input_mb=max_input_mb,
        download_max_seconds=download_max_seconds,
        ytdlp_job_timeout_seconds=ytdlp_job_timeout_seconds,
        ytdlp_metadata_timeout_seconds=ytdlp_metadata_timeout_seconds,
        ytdlp_sleep_requests=ytdlp_sleep_requests,
        ytdlp_concurrency=ytdlp_concurrency,
        heavy_job_concurrency=heavy_job_concurrency,
        search_max_seconds=search_max_seconds,
        search_cache_seconds=search_cache_seconds,
        privacy_policy_url=_clean(os.getenv("PRIVACY_POLICY_URL")),
        admin_user_id=admin_user_id,
        broadcast_rate_per_second=broadcast_rate_per_second,
        top_music_refresh_hours=top_music_refresh_hours,
        top_music_retry_minutes=top_music_retry_minutes,
    )
