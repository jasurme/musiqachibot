"""Instant stored music collections, moods, and notification preferences.

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

from bot.db.storage import (
    NOTIFY_ALL_MUSIC,
    NOTIFY_DISCOVERIES,
    NOTIFY_NEW_MUSIC,
    NOTIFY_RISING_MUSIC,
)
from bot.services.top_music import TOP_MUSIC_PICK_PREFIX

router = Router(name="music_campaigns")

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,32}")
_BUTTON_TEXT_MAX = 100

COLLECTIONS = {
    "new_music": (5, "new_music_header", "new"),
    "rising": (5, "rising_header", "rising"),
    "discoveries": (5, "discoveries_header", "discoveries"),
}
MOODS = {
    "night": ("mood:night", "mood_night", "mood_night_header"),
    "road": ("mood:road", "mood_road", "mood_road_header"),
    "workout": ("mood:workout", "mood_workout", "mood_workout_header"),
    "calm": ("mood:calm", "mood_calm", "mood_calm_header"),
    "weekend": ("mood:weekend", "mood_weekend", "mood_weekend_header"),
}
NOTIFICATION_OPTIONS = {
    "new": (NOTIFY_NEW_MUSIC, "notification_new_music"),
    "rising": (NOTIFY_RISING_MUSIC, "notification_rising"),
    "discoveries": (NOTIFY_DISCOVERIES, "notification_discoveries"),
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
    notification_slug: str | None = None,
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
    if notification_slug is not None:
        if notification_slug not in NOTIFICATION_OPTIONS:
            raise ValueError("unknown notification category")
        rows.append([
            InlineKeyboardButton(
                text=_("btn_notifications_off"),
                callback_data=f"notify:off:{notification_slug}",
            ),
            InlineKeyboardButton(
                text=_("btn_notification_settings"),
                callback_data="notify:open",
            ),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mood_menu_keyboard(_: Callable[..., str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_(label), callback_data=f"mood:{slug}")]
        for slug, (_key, label, _header) in MOODS.items()
    ])


def notification_keyboard(
    mask: int, _: Callable[..., str],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for slug, (bit, label_key) in NOTIFICATION_OPTIONS.items():
        enabled = bool(mask & bit)
        rows.append([
            InlineKeyboardButton(
                text=f"{'✅' if enabled else '❌'} {_(label_key)}",
                callback_data=f"notify:toggle:{slug}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text=_("btn_notifications_all_on"), callback_data="notify:all:on"
        ),
        InlineKeyboardButton(
            text=_("btn_notifications_all_off"), callback_data="notify:all:off"
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    expected, header_key, notification_slug = COLLECTIONS[collection_key]
    items = await _load_complete(db, collection_key, expected)
    if not items:
        await message.answer(_("music_campaign_unavailable"))
        return
    await message.answer(
        _(header_key),
        reply_markup=campaign_list_keyboard(
            items,
            _,
            notification_slug=notification_slug,
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


@router.message(Command("notifications"))
async def cmd_notifications(message: Message, _, db, **kwargs) -> None:
    mask = await db.get_notification_mask(message.from_user.id)
    await message.answer(
        _("notifications_header"), reply_markup=notification_keyboard(mask, _)
    )


async def _edit_or_answer(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        # An old message may no longer be editable. This is still one final
        # result, not a progress notification.
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


async def _show_notification_settings(
    message: Message, user_id: int, _, db, *, edit: bool,
) -> None:
    mask = await db.get_notification_mask(user_id)
    markup = notification_keyboard(mask, _)
    if edit:
        await _edit_or_answer(message, _("notifications_header"), markup)
    else:
        await message.answer(_("notifications_header"), reply_markup=markup)


@router.callback_query(F.data.startswith("notify:"))
async def on_notification(callback: CallbackQuery, _, db, **kwargs) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if parts == ["notify", "open"]:
        await callback.answer()
        await _show_notification_settings(
            callback.message, callback.from_user.id, _, db, edit=False
        )
        return
    if len(parts) != 3:
        await callback.answer(_("invalid_action"), show_alert=True)
        return

    action, value = parts[1], parts[2]
    if action == "all" and value in {"on", "off"}:
        enabled = value == "on"
        await db.set_notification_mask(
            callback.from_user.id, NOTIFY_ALL_MUSIC if enabled else 0
        )
        await callback.answer(
            _(
                "notifications_all_enabled"
                if enabled else "notifications_all_disabled"
            )
        )
        await _show_notification_settings(
            callback.message, callback.from_user.id, _, db, edit=True
        )
        return

    option = NOTIFICATION_OPTIONS.get(value)
    if option is None or action not in {"toggle", "off"}:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    bit, label_key = option
    if action == "toggle":
        await db.toggle_notification_mask(callback.from_user.id, bit)
        await callback.answer(_("notifications_saved"))
        await _show_notification_settings(
            callback.message, callback.from_user.id, _, db, edit=True
        )
        return

    current = await db.get_notification_mask(callback.from_user.id)
    await db.set_notification_mask(callback.from_user.id, current & ~bit)
    await callback.answer(
        _("notifications_category_disabled", name=_(label_key))
    )
