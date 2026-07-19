"""Feature A — find a song by name / artist.

Send text → numbered results list (via the shared `results` component) → tap a
number → that exact track is downloaded and sent (and cached for next time).
"""
import html

from aiogram import Router
from aiogram.types import Message

from bot.handlers.results import present
from bot.services.search import search_tracks
from bot.utils import is_group, is_mentioned, strip_mention

router = Router(name="text_search")


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

    status = await message.answer(_("searching"))
    try:
        items = await search_tracks(query, limit=30)
    except Exception:
        await status.edit_text(_("generic_error"))
        return
    if not items:
        await status.edit_text(_("no_results"))
        return

    header = f"<b>{html.escape(query)}</b>"
    await present(message, _, db, header=header, items=items, per_page=10, edit=status)
