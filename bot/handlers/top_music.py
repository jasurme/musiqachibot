"""Instant Top Music command and direct-download callbacks."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.config import Config
from bot.handlers.results import deliver_track
from bot.services.search import SearchItem
from bot.services.top_music import (
    TOP_MUSIC_PICK_PREFIX,
    load_top_music,
    parse_top_music_pick,
    top_music_list_keyboard,
)

router = Router(name="top_music")


async def send_top_music(message: Message, _, db) -> None:
    """Render only the last complete DB snapshot; never perform network work."""
    items = await load_top_music(db)
    if len(items) != 10:
        await message.answer(_("top_music_unavailable"))
        return
    await message.answer(
        _("top_music_header"),
        reply_markup=top_music_list_keyboard(items),
    )


@router.message(Command("top_music"))
async def cmd_top_music(message: Message, _, db, **kwargs) -> None:
    await send_top_music(message, _, db)


@router.callback_query(F.data == "top_music")
async def on_top_music(callback: CallbackQuery, _, db, **kwargs) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    await send_top_music(callback.message, _, db)


@router.callback_query(F.data.startswith(TOP_MUSIC_PICK_PREFIX))
async def on_top_music_pick(
    callback: CallbackQuery, _, config: Config, db, bot_username: str, **kwargs,
) -> None:
    """Download the exact durable video ID embedded in a Top Music button."""
    video_id = parse_top_music_pick(callback.data)
    if video_id is None or not isinstance(callback.message, Message):
        await callback.answer(_("invalid_action"), show_alert=True)
        return

    # Current metadata improves cached-file titles. The video ID in callback
    # data remains sufficient after chart replacement or a Railway restart.
    items = await load_top_music(db)
    item = next((item for item in items if item.video_id == video_id), None)
    if item is None:
        item = SearchItem(
            video_id=video_id, title="", duration=None, uploader=""
        )
    await deliver_track(callback, _, config, db, bot_username, item)
