"""Per-user favorite playlists and durable heart callbacks.

Audio delivery paths register trusted track metadata before exposing a heart.
Callbacks carry only a short catalog key, while playlist reads and mutations
are always scoped to the Telegram user who pressed the button.
"""

from __future__ import annotations

import logging
import os
import re
from math import ceil
from typing import Any, Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from bot import jobs
from bot.config import Config
from bot.i18n import SUPPORTED, t
from bot.services import downloader
from bot.services.search import SearchItem

router = Router(name="favorites")
logger = logging.getLogger(__name__)

PAGE_SIZE = 10
ADD_PREFIX = "fav:a:"
SAVED_PREFIX = "fav:h:"
PLAY_PREFIX = "fav:p:"
DELETE_PREFIX = "fav:d:"
PAGE_PREFIX = "fav:g:"
_TRACK_KEY = re.compile(
    r"[a-z][a-z0-9_]{0,11}:[A-Za-z0-9._-]{1,40}"
)
_BUTTON_TITLE_MAX = 48
_OPEN_LABELS = frozenset(t("btn_open_favorites", locale) for locale in SUPPORTED)


def _is_private_user_message(message: Message) -> bool:
    return bool(
        message.from_user
        and message.chat.type == "private"
        and message.chat.id == message.from_user.id
    )


def _is_private_user_callback(callback: CallbackQuery) -> bool:
    return bool(
        isinstance(callback.message, Message)
        and callback.message.chat.type == "private"
        and callback.message.chat.id == callback.from_user.id
    )


def _is_favorites_button(message: Message) -> bool:
    """Accept the current or a stale localized persistent-keyboard label."""
    return _is_private_user_message(message) and message.text in _OPEN_LABELS


def favorites_reply_keyboard(locale: str) -> ReplyKeyboardMarkup:
    """One-tap playlist access that remains beside Telegram's input field."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("btn_open_favorites", locale))]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _track_key(data: str | None, prefix: str) -> str | None:
    if not data or not data.startswith(prefix):
        return None
    key = data.removeprefix(prefix)
    return key if _TRACK_KEY.fullmatch(key) else None


def _button(data: str, text: str) -> InlineKeyboardButton:
    if not 1 <= len(data.encode("utf-8")) <= 64:
        raise ValueError("favorite callback exceeds Telegram's byte limit")
    return InlineKeyboardButton(text=text, callback_data=data)


def favorite_unsaved_markup(
    track_key: str, _: Callable[..., str],
) -> InlineKeyboardMarkup:
    key = _track_key(f"{ADD_PREFIX}{track_key}", ADD_PREFIX)
    if key is None:
        raise ValueError("invalid favorite track key")
    return InlineKeyboardMarkup(inline_keyboard=[[
        _button(f"{ADD_PREFIX}{key}", _("btn_add_favorite"))
    ]])


def favorite_saved_markup(
    track_key: str, _: Callable[..., str],
) -> InlineKeyboardMarkup:
    key = _track_key(f"{SAVED_PREFIX}{track_key}", SAVED_PREFIX)
    if key is None:
        raise ValueError("invalid favorite track key")
    return InlineKeyboardMarkup(inline_keyboard=[[
        _button(f"{SAVED_PREFIX}{key}", _("btn_favorite_saved"))
    ]])


async def prepare_favorite_audio(
    db,
    user_id: int,
    item: Any,
    _: Callable[..., str],
    *,
    file_id: str | None = None,
) -> tuple[str | None, InlineKeyboardMarkup | None]:
    """Best-effort catalog registration; music delivery must never depend on it."""
    try:
        track_key = await db.upsert_favorite_track(item, file_id=file_id)
        saved = await db.is_favorite(user_id, track_key)
        markup = (
            favorite_saved_markup(track_key, _)
            if saved
            else favorite_unsaved_markup(track_key, _)
        )
        return track_key, markup
    except Exception as exc:
        logger.warning(
            "Favorite heart unavailable error=%s", type(exc).__name__
        )
        return None, None


def _title(value: Any) -> str:
    title = " ".join(str(value or "").split()) or "Music"
    if len(title) > _BUTTON_TITLE_MAX:
        title = title[:_BUTTON_TITLE_MAX - 1].rstrip() + "…"
    return title


def _playlist_markup(
    items: list[dict], page: int, page_count: int,
) -> InlineKeyboardMarkup | None:
    if not items:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        key = str(item["track_key"])
        rows.append([
            _button(f"{PLAY_PREFIX}{key}", f"🎵 {_title(item.get('title'))}"),
            _button(f"{DELETE_PREFIX}{key}:{page}", "🗑"),
        ])
    if page_count > 1:
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(_button(f"{PAGE_PREFIX}{page - 1}", "⬅️"))
        navigation.append(
            _button(f"{PAGE_PREFIX}{page}", f"{page + 1}/{page_count}")
        )
        if page + 1 < page_count:
            navigation.append(_button(f"{PAGE_PREFIX}{page + 1}", "➡️"))
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _playlist_page(db, user_id: int, page: int) -> tuple[str, Any, int]:
    count = await db.count_favorites(user_id)
    if count <= 0:
        return "favorites_empty", None, 0
    page_count = max(1, ceil(count / PAGE_SIZE))
    safe_page = min(max(0, int(page)), page_count - 1)
    items = await db.list_favorites(
        user_id, limit=PAGE_SIZE, offset=safe_page * PAGE_SIZE
    )
    return "favorites_header", (items, count, page_count), safe_page


async def _send_playlist(
    message: Message,
    _: Callable[..., str],
    db,
    user_id: int,
    *,
    page: int = 0,
    edit: bool = False,
) -> None:
    key, payload, safe_page = await _playlist_page(db, user_id, page)
    if payload is None:
        text = _(key)
        markup = None
    else:
        items, count, page_count = payload
        text = _(key, count=count)
        markup = _playlist_markup(items, safe_page, page_count)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
        except Exception:
            pass
    await message.answer(text, reply_markup=markup)


@router.message(Command("favorites", "sevimlilar"), _is_private_user_message)
async def show_favorites(message: Message, _, db, **kwargs) -> None:
    try:
        await _send_playlist(message, _, db, message.from_user.id)
    except Exception as exc:
        logger.error("Favorite playlist read failed error=%s", type(exc).__name__)
        await message.answer(_("generic_error"))


@router.message(_is_favorites_button)
async def show_favorites_from_keyboard(message: Message, _, db, **kwargs) -> None:
    """Handle all localized bottom-button labels before search/admin broadcast."""
    await show_favorites(message, _, db)


@router.callback_query(F.data.startswith(ADD_PREFIX))
async def add_favorite(callback: CallbackQuery, _, db, **kwargs) -> None:
    if not _is_private_user_callback(callback):
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    key = _track_key(callback.data, ADD_PREFIX)
    if key is None:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    try:
        added = await db.add_favorite(callback.from_user.id, key)
    except KeyError:
        await callback.answer(_("favorite_unavailable"), show_alert=True)
        return
    except Exception as exc:
        logger.error("Favorite add failed error=%s", type(exc).__name__)
        await callback.answer(_("generic_error"), show_alert=True)
        return
    await callback.answer(_("favorite_added" if added else "favorite_exists"))
    try:
        await callback.message.edit_reply_markup(
            reply_markup=favorite_saved_markup(key, _)
        )
    except Exception:
        # Saving succeeded; an old/non-editable audio message does not change
        # playlist correctness.
        pass


@router.callback_query(F.data.startswith(SAVED_PREFIX))
async def ensure_favorite_saved(
    callback: CallbackQuery, _, db, **kwargs,
) -> None:
    if not _is_private_user_callback(callback):
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    key = _track_key(callback.data, SAVED_PREFIX)
    if key is None:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    try:
        added = await db.add_favorite(callback.from_user.id, key)
    except KeyError:
        await callback.answer(_("favorite_unavailable"), show_alert=True)
        return
    except Exception as exc:
        logger.error("Favorite save check failed error=%s", type(exc).__name__)
        await callback.answer(_("generic_error"), show_alert=True)
        return
    await callback.answer(_("favorite_added" if added else "favorite_exists"))


def _delete_payload(data: str | None) -> tuple[str, int] | None:
    if not data or not data.startswith(DELETE_PREFIX):
        return None
    payload = data.removeprefix(DELETE_PREFIX)
    try:
        key, raw_page = payload.rsplit(":", 1)
    except ValueError:
        return None
    if _TRACK_KEY.fullmatch(key) is None or not raw_page.isdecimal():
        return None
    page = int(raw_page)
    return (key, page) if page <= 1_000_000 else None


@router.callback_query(F.data.startswith(DELETE_PREFIX))
async def delete_favorite(callback: CallbackQuery, _, db, **kwargs) -> None:
    if not _is_private_user_callback(callback):
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    payload = _delete_payload(callback.data)
    if payload is None:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    key, page = payload
    try:
        removed = await db.remove_favorite(callback.from_user.id, key)
    except Exception as exc:
        logger.error("Favorite removal failed error=%s", type(exc).__name__)
        await callback.answer(_("generic_error"), show_alert=True)
        return
    await callback.answer(
        _("favorite_removed" if removed else "favorite_unavailable")
    )
    try:
        await _send_playlist(
            callback.message, _, db, callback.from_user.id, page=page, edit=True
        )
    except Exception as exc:
        logger.error("Favorite playlist refresh failed error=%s", type(exc).__name__)
        await callback.message.answer(_("generic_error"))


@router.callback_query(F.data.startswith(PAGE_PREFIX))
async def favorites_page(callback: CallbackQuery, _, db, **kwargs) -> None:
    if not _is_private_user_callback(callback):
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    raw_page = (callback.data or "").removeprefix(PAGE_PREFIX)
    if not raw_page.isdecimal() or int(raw_page) > 1_000_000:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    await callback.answer()
    try:
        await _send_playlist(
            callback.message,
            _,
            db,
            callback.from_user.id,
            page=int(raw_page),
            edit=True,
        )
    except Exception as exc:
        logger.error("Favorite playlist page failed error=%s", type(exc).__name__)
        await callback.message.answer(_("generic_error"))


async def _send_direct_favorite(
    callback: CallbackQuery,
    _: Callable[..., str],
    config: Config,
    db,
    bot_username: str,
    favorite: dict,
) -> None:
    """Replay non-YouTube audio after a stale Telegram file_id."""
    source_url = str(favorite.get("source_url") or "").strip()
    if not source_url:
        await callback.message.answer(_("favorite_unavailable"))
        return
    blocked_key = downloader.provider_error_key(source_url)
    if blocked_key:
        await callback.message.answer(_(blocked_key))
        return

    cache_key = downloader.telegram_media_cache_key(
        callback.bot.id, source_url, "audio"
    )
    async with downloader.media_singleflight_lock(cache_key):
        cached = await db.get_cached_audio(cache_key)
        if cached:
            try:
                sent = await callback.message.answer_audio(
                    cached,
                    caption=f"👉 @{bot_username}",
                    title=favorite.get("title") or None,
                    performer=favorite.get("uploader") or None,
                    reply_markup=favorite_saved_markup(
                        favorite["track_key"], _
                    ),
                )
            except TelegramBadRequest:
                await db.delete_cached_audio(cache_key)
            else:
                if sent.audio:
                    try:
                        await db.set_favorite_file_id(
                            favorite["track_key"], sent.audio.file_id
                        )
                    except Exception:
                        logger.exception(
                            "Favorite replay file_id update failed key=%s",
                            favorite["track_key"],
                        )
                return

        claim_error = jobs.try_claim(callback.from_user.id)
        if claim_error:
            await callback.message.answer(_(claim_error))
            return
        path: str | None = None
        stage = "provider"
        try:
            result = await downloader.download_audio(
                source_url,
                config.download_dir,
                max_bytes=config.max_file_mb * 1024 * 1024,
            )
            path = result.path
            stage = "upload"
            size_mb = os.path.getsize(path) / (1024 * 1024)
            if size_mb > config.max_file_mb:
                await callback.message.answer(
                    _("too_big", size=round(size_mb), limit=config.max_file_mb)
                )
                return
            _, markup = await prepare_favorite_audio(
                db,
                callback.from_user.id,
                {
                    **favorite,
                    "title": result.title or favorite.get("title") or "Music",
                    "duration": result.duration or favorite.get("duration"),
                    "uploader": result.uploader or favorite.get("uploader") or "",
                },
                _,
            )
            sent = await callback.message.answer_audio(
                FSInputFile(path),
                caption=f"👉 @{bot_username}",
                title=result.title or favorite.get("title") or None,
                performer=result.uploader or favorite.get("uploader") or None,
                reply_markup=markup,
            )
            if sent.audio:
                try:
                    await db.set_favorite_file_id(
                        favorite["track_key"], sent.audio.file_id
                    )
                    await db.set_cached_audio(
                        cache_key,
                        sent.audio.file_id,
                        favorite.get("title") or "",
                    )
                except Exception:
                    logger.exception(
                        "Favorite audio delivered but cache update failed key=%s",
                        favorite["track_key"],
                    )
        except Exception as exc:
            if stage == "provider":
                downloader.record_provider_failure(source_url, exc)
            logger.error(
                "Favorite source replay failed stage=%s error=%s: %s",
                stage,
                type(exc).__name__,
                downloader.safe_error_message(exc),
            )
            key = (
                downloader.download_error_key(exc)
                if stage == "provider"
                else "upload_failed"
            )
            await callback.message.answer(_(key))
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            jobs.release(callback.from_user.id)


@router.callback_query(F.data.startswith(PLAY_PREFIX))
async def play_favorite(
    callback: CallbackQuery,
    _,
    config: Config,
    db,
    bot_username: str,
    **kwargs,
) -> None:
    if not _is_private_user_callback(callback):
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    key = _track_key(callback.data, PLAY_PREFIX)
    if key is None:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    try:
        favorite = await db.get_favorite(callback.from_user.id, key)
    except Exception as exc:
        logger.error("Favorite lookup failed error=%s", type(exc).__name__)
        await callback.answer(_("generic_error"), show_alert=True)
        return
    if favorite is None:
        await callback.answer(_("favorite_unavailable"), show_alert=True)
        return

    await callback.answer()
    file_id = favorite.get("file_id")
    if file_id:
        try:
            await callback.message.answer_audio(
                file_id,
                caption=f"👉 @{bot_username}",
                title=favorite.get("title") or None,
                performer=favorite.get("uploader") or None,
                reply_markup=favorite_saved_markup(key, _),
            )
            return
        except TelegramBadRequest:
            try:
                await db.set_favorite_file_id(key, None)
            except Exception:
                logger.exception(
                    "Could not clear invalid favorite file_id key=%s", key
                )
        except Exception as exc:
            logger.error(
                "Favorite Telegram replay failed error=%s", type(exc).__name__
            )
            await callback.message.answer(_("generic_error"))
            return

    video_id = str(favorite.get("video_id") or "").strip()
    if video_id:
        from bot.handlers.results import deliver_track

        await deliver_track(
            callback,
            _,
            config,
            db,
            bot_username,
            SearchItem(
                video_id=video_id,
                title=favorite.get("title") or "Music",
                duration=favorite.get("duration"),
                uploader=favorite.get("uploader") or "",
            ),
            acknowledge=False,
        )
        return
    await _send_direct_favorite(
        callback, _, config, db, bot_username, favorite
    )
