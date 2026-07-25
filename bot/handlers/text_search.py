"""Feature A — find a song by name / artist.

Send text → numbered results list (via the shared `results` component) → tap a
number → that exact track is downloaded and sent (and cached for next time).
"""
import html
import logging
import re

from aiogram import Router
from aiogram.types import Message

from bot import jobs
from bot.handlers.results import present
from bot.services.search import search_tracks
from bot.utils import is_group, is_mentioned, strip_mention

router = Router(name="text_search")
logger = logging.getLogger(__name__)
MAX_QUERY_LENGTH = 200
_URL = re.compile(r"https?://", re.IGNORECASE)


def _is_plain_text(message: Message) -> bool:
    return bool(message.text) and not message.text.startswith("/")


@router.message(_is_plain_text)
async def handle_text(message: Message, _, db, bot_username, **kwargs):
    # In groups, only search when the bot is tagged (else stay silent).
    if is_group(message):
        if not is_mentioned(message, bot_username):
            return
        query = strip_mention(message.text, bot_username)
    else:
        query = message.text.strip()
    if not query:
        return
    if _URL.search(query):
        await message.answer(_("unsupported_link"))
        return
    if len(query) > MAX_QUERY_LENGTH:
        await message.answer(_("query_too_long", limit=MAX_QUERY_LENGTH))
        return

    user_id = message.from_user.id
    claim_error = jobs.try_claim(user_id)
    if claim_error:
        await message.answer(_(claim_error))
        return
    status = None
    try:
        status = await message.answer(_("searching"))
        items = await search_tracks(query, limit=30)
    except Exception as exc:
        from bot.services.downloader import download_error_key, safe_error_message
        logger.error(
            "Text search failed query_length=%s error=%s: %s",
            len(query), type(exc).__name__, safe_error_message(exc),
        )
        key = download_error_key(exc)
        if status is not None:
            visible = {
                "service_busy", "download_blocked", "download_rate_limited",
            }
            await status.edit_text(_(key if key in visible else "generic_error"))
        return
    finally:
        jobs.release(user_id)
    if not items:
        await status.edit_text(_("no_results"))
        return

    header = f"<b>{html.escape(query)}</b>"
    await present(message, _, db, header=header, items=items, per_page=10, edit=status)
