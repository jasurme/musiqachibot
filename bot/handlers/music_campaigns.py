"""Instant stored music collections and mood playlists.

Every request path in this router reads SQLite only. Provider lookups and
collection refreshes belong to the background scheduler, so commands never
need a progress message and always render either a complete snapshot or one
final unavailable response.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.services.top_music import TOP_MUSIC_PICK_PREFIX

router = Router(name="music_campaigns")

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,32}")
_BUTTON_TEXT_MAX = 100

COLLECTIONS = {
    "new_music": (5, "new_music_header"),
    "rising": (5, "rising_header"),
    "discoveries": (5, "discoveries_header"),
}
MOODS = {
    "night": ("mood:night", "mood_night", "mood_night_header"),
    "road": ("mood:road", "mood_road", "mood_road_header"),
    "workout": ("mood:workout", "mood_workout", "mood_workout_header"),
    "calm": ("mood:calm", "mood_calm", "mood_calm_header"),
    "weekend": ("mood:weekend", "mood_weekend", "mood_weekend_header"),
}


def _value(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def _song_rows(items: list[Any]) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        video_id = str(_value(item, "video_id") or "").strip()
        title = " ".join(str(_value(item, "title") or "").split())
        if _VIDEO_ID.fullmatch(video_id) is None or not title:
            raise ValueError("music collection contains an invalid track")
        if len(title) > _BUTTON_TEXT_MAX:
            title = title[:_BUTTON_TEXT_MAX - 1].rstrip() + "…"
        rows.append([
            InlineKeyboardButton(
                text=title,
                callback_data=f"{TOP_MUSIC_PICK_PREFIX}{video_id}",
            )
        ])
    return rows


def campaign_list_keyboard(
    items: list[Any], _: Callable[..., str], *,
    include_moods: bool = False,
    back_to_moods: bool = False,
) -> InlineKeyboardMarkup:
    """Build full-width durable song buttons plus optional utility controls."""
    rows = _song_rows(items)
    if include_moods:
        rows.append([
            InlineKeyboardButton(
                text=_("btn_open_moods"), callback_data="moods:open"
            )
        ])
    if back_to_moods:
        rows.append([
            InlineKeyboardButton(text=_("btn_back"), callback_data="mood:menu")
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mood_menu_keyboard(_: Callable[..., str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_(label), callback_data=f"mood:{slug}")]
        for slug, (_key, label, _header) in MOODS.items()
    ])


async def _load_complete(db, key: str, expected: int) -> list[dict]:
    state = await db.get_music_collection_state(key)
    items = state.get("items") if isinstance(state, dict) else None
    if not isinstance(items, list) or len(items) != expected:
        return []
    try:
        _song_rows(items)
    except ValueError:
        return []
    return items


async def _send_collection(
    message: Message, _, db, collection_key: str,
) -> None:
    expected, header_key = COLLECTIONS[collection_key]
    items = await _load_complete(db, collection_key, expected)
    if not items:
        await message.answer(_("music_campaign_unavailable"))
        return
    await message.answer(
        _(header_key),
        reply_markup=campaign_list_keyboard(
            items,
            _,
            include_moods=collection_key in {"new_music", "discoveries"},
        ),
    )


@router.message(Command("new_music"))
async def cmd_new_music(message: Message, _, db, **kwargs) -> None:
    await _send_collection(message, _, db, "new_music")


@router.message(Command("rising"))
async def cmd_rising(message: Message, _, db, **kwargs) -> None:
    await _send_collection(message, _, db, "rising")


@router.message(Command("discoveries"))
async def cmd_discoveries(message: Message, _, db, **kwargs) -> None:
    await _send_collection(message, _, db, "discoveries")


@router.message(Command("moods"))
async def cmd_moods(message: Message, _, **kwargs) -> None:
    await message.answer(_("moods_header"), reply_markup=mood_menu_keyboard(_))


async def _edit_or_answer(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        # An old message may no longer be editable. This is still one final
        # result, not a progress update.
        await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "moods:open")
async def open_moods(callback: CallbackQuery, _, **kwargs) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            _("moods_header"), reply_markup=mood_menu_keyboard(_)
        )


@router.callback_query(F.data.startswith("mood:"))
async def on_mood(callback: CallbackQuery, _, db, **kwargs) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    slug = (callback.data or "").removeprefix("mood:")
    if slug == "menu":
        await callback.answer()
        await _edit_or_answer(
            callback.message, _("moods_header"), mood_menu_keyboard(_)
        )
        return
    mood = MOODS.get(slug)
    if mood is None:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    collection_key, _label_key, header_key = mood
    items = await _load_complete(db, collection_key, 10)
    if not items:
        await callback.answer(_("music_campaign_unavailable"), show_alert=True)
        return
    await callback.answer()
    await _edit_or_answer(
        callback.message,
        _(header_key),
        campaign_list_keyboard(items, _, back_to_moods=True),
    )


@router.callback_query(F.data.startswith("notify:"))
async def reject_retired_notification_control(
    callback: CallbackQuery, _, **kwargs,
) -> None:
    """Acknowledge buttons sent by the retired settings implementation."""
    await callback.answer(_("invalid_action"), show_alert=True)
