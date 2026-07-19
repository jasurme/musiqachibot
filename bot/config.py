import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    local_api_url: str | None
    default_locale: str
    download_dir: str
    max_file_mb: int
    # feature keys (unused until their step)
    spotify_client_id: str | None
    spotify_client_secret: str | None
    genius_token: str | None
    audd_token: str | None
    # sqlite path — point at a mounted volume in production (e.g. /data/musiqa.db)
    db_path: str = "musiqa.db"


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def load_config() -> Config:
    token = _clean(os.getenv("BOT_TOKEN"))
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is not set. Put `BOT_TOKEN=...` in your .env file "
            "(get one from @BotFather)."
        )
    return Config(
        bot_token=token,
        local_api_url=_clean(os.getenv("LOCAL_BOT_API_URL")),
        default_locale=os.getenv("DEFAULT_LOCALE", "uz").strip() or "uz",
        download_dir=os.getenv("DOWNLOAD_DIR", "downloads").strip() or "downloads",
        max_file_mb=int(os.getenv("MAX_FILE_MB", "50") or "50"),
        spotify_client_id=_clean(os.getenv("SPOTIFY_CLIENT_ID")),
        spotify_client_secret=_clean(os.getenv("SPOTIFY_CLIENT_SECRET")),
        genius_token=_clean(os.getenv("GENIUS_TOKEN")),
        audd_token=_clean(os.getenv("AUDD_TOKEN")),
        db_path=os.getenv("DB_PATH", "musiqa.db").strip() or "musiqa.db",
    )
