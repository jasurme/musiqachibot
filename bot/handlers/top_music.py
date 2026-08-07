"""Instant Uzbekistan Top 10 command and announcement-button callback."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.handlers.results import present
from bot.services.top_music import load_top_music

router = Router(name="top_music")

_APPLE_CHART_URL = "https://music.apple.com/uz/new/top-charts/songs"


async def send_top_music(
    message: Message, _, db, *, owner_user_id: int | None,
) -> None:
    """Render only the last complete DB snapshot; never perform network work."""
    items = await load_top_music(db)
    if len(items) != 10:
        await message.answer(_("top_music_unavailable"))
        return
    header = (
        _("top_music_header")
        + "\n"
        + _("top_music_source", url=_APPLE_CHART_URL)
    )
    await present(
        message, _, db,
        header=header, items=items, per_page=10,
        owner_user_id=owner_user_id,
    )


@router.message(Command("top_music"))
async def cmd_top_music(message: Message, _, db, **kwargs) -> None:
    await send_top_music(
        message, _, db,
        owner_user_id=message.from_user.id if message.from_user else None,
    )


@router.callback_query(F.data == "top_music")
async def on_top_music(callback: CallbackQuery, _, db, **kwargs) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await send_top_music(
        callback.message, _, db, owner_user_id=callback.from_user.id,
    )
