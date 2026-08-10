import asyncio
import logging
import os
import tempfile

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from bot.config import Config, load_config
from bot import jobs
from bot.db.storage import Storage
from bot.handlers import (
    broadcast,
    media_recognize,
    music_campaigns,
    results,
    round as round_handler,
    start,
    text_search,
    top_music,
    url_download,
)
from bot.middlewares.i18n import I18nMiddleware
from bot.i18n import SUPPORTED, t
from bot.services.downloader import (
    cleanup_stale_runtime_files,
    runtime_warnings,
    shutdown_provider_workers,
)
from bot.services.music_campaigns import run_music_campaign_scheduler
from bot.services.top_music import run_top_music_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("musiqa")


def _build_telegram_session(config: Config) -> AiohttpSession:
    if config.local_api_url:
        return AiohttpSession(
            api=TelegramAPIServer.from_base(
                config.local_api_url, is_local=True
            ),
            timeout=300,
        )
    return AiohttpSession(timeout=300)


def _materialize_cookies() -> str | None:
    """Materialize Railway's multiline YouTube cookie variable securely."""
    content = os.getenv("YTDLP_COOKIES_CONTENT")
    configured_file = os.getenv("YTDLP_COOKIES_FILE")
    if content and (not configured_file or not os.path.isfile(configured_file)):
        if configured_file:
            logger.warning(
                "YTDLP_COOKIES_FILE does not exist; using "
                "YTDLP_COOKIES_CONTENT instead"
            )
        normalized = content.lstrip("\ufeff\r\n ")
        first_line = normalized.splitlines()[0] if normalized else ""
        if first_line not in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"}:
            logger.warning(
                "YTDLP_COOKIES_CONTENT is not a Netscape cookie export; "
                "YouTube authentication will probably fail"
            )
        path = None
        try:
            fd, path = tempfile.mkstemp(prefix="musiqa_yt_cookies_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(normalized)
            os.environ["YTDLP_COOKIES_FILE"] = path
            logger.info("Loaded YouTube cookies from YTDLP_COOKIES_CONTENT")
            return path
        except OSError as exc:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            logger.warning("could not write cookies file: %s", exc)
    return None


async def _set_commands(
    bot: Bot, default_locale: str, admin_user_id: int,
) -> None:
    from aiogram.types import BotCommand, BotCommandScopeChat

    def commands(locale: str, *, admin: bool = False) -> list[BotCommand]:
        values = [
            BotCommand(command="start", description=t("cmd_start", locale)),
            BotCommand(command="round", description=t("cmd_round", locale)),
            BotCommand(
                command="top_music", description=t("cmd_top_music", locale)
            ),
            BotCommand(
                command="new_music", description=t("cmd_new_music", locale)
            ),
            BotCommand(command="rising", description=t("cmd_rising", locale)),
            BotCommand(
                command="discoveries", description=t("cmd_discoveries", locale)
            ),
            BotCommand(command="moods", description=t("cmd_moods", locale)),
            BotCommand(command="lang", description=t("cmd_lang", locale)),
            BotCommand(command="privacy", description=t("cmd_privacy", locale)),
            BotCommand(
                command="delete_my_data",
                description=t("cmd_delete_my_data", locale),
            ),
        ]
        if admin:
            values.append(
                BotCommand(
                    command="total_users",
                    description=t("cmd_total_users", locale),
                )
            )
        return values

    try:
        await bot.set_my_commands(commands(default_locale))
        for locale in SUPPORTED:
            await bot.set_my_commands(commands(locale), language_code=locale)

        # A chat-scoped command list replaces the broader default list, so the
        # administrator receives every normal command plus the private-only
        # audience counter. Menu scope is convenience; the handler still
        # performs its own identity and private-chat authorization.
        admin_scope = BotCommandScopeChat(chat_id=admin_user_id)
        await bot.set_my_commands(
            commands(default_locale, admin=True), scope=admin_scope
        )
        for locale in SUPPORTED:
            await bot.set_my_commands(
                commands(locale, admin=True),
                scope=admin_scope,
                language_code=locale,
            )
    except Exception as exc:  # non-fatal
        logger.warning("set_my_commands failed: %s", exc)


async def main() -> None:
    config = load_config()
    jobs.configure(config.heavy_job_concurrency)
    materialized_cookie = _materialize_cookies()
    session: AiohttpSession | None = None
    bot: Bot | None = None
    storage: Storage | None = None
    admin_broadcast_task: asyncio.Task | None = None
    top_music_task: asyncio.Task | None = None
    music_campaign_task: asyncio.Task | None = None
    try:
        removed = cleanup_stale_runtime_files(config.download_dir)
        if removed:
            logger.info("Removed %s stale media runtime paths", removed)
        for warning in runtime_warnings():
            logger.warning("startup dependency check: %s", warning)
        logger.info(
            "yt-dlp auth configuration: cookies=%s proxy=%s instagram_proxy=%s",
            bool(
                os.getenv("YTDLP_COOKIES_FILE")
                and os.path.isfile(os.environ["YTDLP_COOKIES_FILE"])
            ),
            bool(os.getenv("YTDLP_PROXY")),
            bool(os.getenv("INSTAGRAM_PROXY")),
        )

        session = _build_telegram_session(config)
        if config.local_api_url:
            logger.info("Using local Bot API server at %s", config.local_api_url)

        bot = Bot(
            token=config.bot_token,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        storage = Storage(config.db_path)
        await storage.init()

        dp = Dispatcher()
        dp["db"] = storage
        dp["config"] = config

        i18n = I18nMiddleware(storage, config.default_locale)
        # Outer middleware sees even otherwise-unhandled private files, so the
        # broadcast audience contains every user who interacts with the bot.
        dp.message.outer_middleware(i18n)
        dp.callback_query.outer_middleware(i18n)

        # Order matters: admin broadcasts must preempt every normal workflow;
        # round is state-filtered, and URL must precede catch-all text search.
        dp.include_router(broadcast.router)
        dp.include_router(top_music.router)
        dp.include_router(music_campaigns.router)
        dp.include_router(round_handler.router)
        dp.include_router(start.router)
        dp.include_router(url_download.router)
        dp.include_router(media_recognize.router)
        dp.include_router(text_search.router)
        dp.include_router(results.router)

        me = await bot.get_me()
        dp["bot_username"] = me.username
        logger.info("Bot @%s (id=%s) starting long polling...", me.username, me.id)

        await _set_commands(
            bot, config.default_locale, config.admin_user_id
        )
        await bot.delete_webhook(drop_pending_updates=config.drop_pending_updates)
        # Snapshot resumable manual campaigns before polling can accept a new
        # Confirm callback. Delivery stays in the background, while the fixed
        # token set prevents one new campaign from acquiring two runners.
        pending_admin_broadcasts = await storage.list_sending_broadcast_drafts(
            config.admin_user_id
        )
        admin_broadcast_task = asyncio.create_task(
            broadcast.run_pending_admin_broadcasts(
                bot, storage, config, drafts=pending_admin_broadcasts
            ),
            name="admin-broadcast-resume",
        )
        top_music_task = asyncio.create_task(
            run_top_music_scheduler(bot, storage, config),
            name="top-music-scheduler",
        )
        music_campaign_task = asyncio.create_task(
            run_music_campaign_scheduler(bot, storage, config),
            name="music-campaign-scheduler",
        )
        await dp.start_polling(bot)
    finally:
        if admin_broadcast_task is not None:
            admin_broadcast_task.cancel()
            try:
                await admin_broadcast_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "admin broadcast resume shutdown failed: %s",
                    type(exc).__name__,
                )
        if music_campaign_task is not None:
            music_campaign_task.cancel()
            try:
                await music_campaign_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "music campaign scheduler shutdown failed: %s",
                    type(exc).__name__,
                )
        if top_music_task is not None:
            top_music_task.cancel()
            try:
                await top_music_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "top music scheduler shutdown failed: %s",
                    type(exc).__name__,
                )
        try:
            await shutdown_provider_workers()
        except Exception as exc:
            logger.warning("provider worker shutdown failed: %s", type(exc).__name__)
        if storage is not None:
            try:
                await storage.close()
            except Exception as exc:
                logger.warning("database close failed: %s", type(exc).__name__)
        try:
            if bot is not None:
                await bot.session.close()
            elif session is not None:
                await session.close()
        except Exception as exc:
            logger.warning("Telegram session close failed: %s", type(exc).__name__)
        if materialized_cookie:
            os.environ.pop("YTDLP_COOKIES_FILE", None)
            try:
                os.remove(materialized_cookie)
            except OSError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped.")
