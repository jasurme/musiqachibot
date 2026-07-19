import asyncio
import logging
import os
import tempfile

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from bot.config import load_config
from bot.db.storage import Storage
from bot.handlers import (
    media_recognize,
    results,
    round as round_handler,
    start,
    text_search,
    url_download,
)
from bot.middlewares.i18n import I18nMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("musiqa")


def _materialize_cookies() -> None:
    """Railway/cloud hosts expose string env vars, not files. If cookies are
    provided as YTDLP_COOKIES_CONTENT, write them to a file and point
    YTDLP_COOKIES_FILE at it so yt-dlp can use them."""
    content = os.getenv("YTDLP_COOKIES_CONTENT")
    if content and not os.getenv("YTDLP_COOKIES_FILE"):
        path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        try:
            with open(path, "w") as f:
                f.write(content)
            os.environ["YTDLP_COOKIES_FILE"] = path
            logger.info("Loaded YouTube cookies from YTDLP_COOKIES_CONTENT")
        except OSError as exc:
            logger.warning("could not write cookies file: %s", exc)


async def _set_commands(bot: Bot) -> None:
    from aiogram.types import BotCommand
    commands = [
        BotCommand(command="start", description="Boshlash / Start"),
        BotCommand(command="round", description="⭕ Videoni yumaloq qilish"),
        BotCommand(command="lang", description="🌐 Til / Language"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as exc:  # non-fatal
        logger.warning("set_my_commands failed: %s", exc)


async def main() -> None:
    config = load_config()
    _materialize_cookies()

    session = None
    if config.local_api_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(config.local_api_url))
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

    i18n = I18nMiddleware(storage)
    dp.message.middleware(i18n)
    dp.callback_query.middleware(i18n)

    # order matters: round (state-filtered) first, URL before the catch-all text
    dp.include_router(round_handler.router)
    dp.include_router(start.router)
    dp.include_router(url_download.router)
    dp.include_router(media_recognize.router)
    dp.include_router(text_search.router)
    dp.include_router(results.router)

    me = await bot.get_me()
    dp["bot_username"] = me.username
    logger.info("Bot @%s (id=%s) starting long polling...", me.username, me.id)

    try:
        await _set_commands(bot)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped.")
