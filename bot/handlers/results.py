"""Shared results list — a header + numbered tracks + number buttons, optionally
with Lyrics/Video buttons and album art. Used by Feature A (search) and
Feature C (recognition). Owns the pick / page / lyrics / video callbacks.
"""
import asyncio
import html
import logging
import os
import secrets
import time
from collections import OrderedDict
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import Config
from bot import jobs
from bot.services import downloader
from bot.services.lyrics import fetch_lyrics
from bot.services.search import SearchItem

router = Router(name="results")
logger = logging.getLogger(__name__)

# in-memory cache in front of the DB (fast path); the DB copy survives restarts
_SESS: "OrderedDict[str, dict]" = OrderedDict()
_SESS_MAX = 500
COLS = 5


def purge_owner(owner_user_id: int) -> None:
    for token, session in list(_SESS.items()):
        if session.get("owner_user_id") == owner_user_id:
            _SESS.pop(token, None)


def _serialize(sess: dict) -> dict:
    return {
        "header": sess["header"],
        "per_page": sess["per_page"],
        "extras": sess.get("extras", False),
        "artist": sess.get("artist"),
        "title": sess.get("title"),
        "listen_url": sess.get("listen_url"),
        "owner_user_id": sess.get("owner_user_id"),
        "items": [
            {"video_id": it.video_id, "title": it.title,
             "duration": it.duration, "uploader": it.uploader}
            for it in sess["items"]
        ],
    }


def _deserialize(data: dict) -> dict:
    items = [
        SearchItem(video_id=d["video_id"], title=d["title"],
                   duration=d.get("duration"), uploader=d.get("uploader") or "")
        for d in data.get("items", [])
    ]
    return {
        "header": data["header"], "per_page": data["per_page"],
        "extras": data.get("extras", False), "artist": data.get("artist"),
        "title": data.get("title"), "items": items,
        "listen_url": data.get("listen_url"),
        "owner_user_id": data.get("owner_user_id"),
    }


def _remember(sess: dict) -> str:
    token = secrets.token_urlsafe(6)
    _SESS[token] = sess
    while len(_SESS) > _SESS_MAX:
        _SESS.popitem(last=False)
    return token


async def _get_session(token: str, db) -> dict | None:
    sess = _SESS.get(token)
    if sess is not None:
        _SESS.move_to_end(token)
        return sess
    data = await db.get_session(token)
    if not data:
        return None
    sess = _deserialize(data)
    _SESS[token] = sess  # warm the cache
    while len(_SESS) > _SESS_MAX:
        _SESS.popitem(last=False)
    return sess


def _fmt_dur(seconds: int | None) -> str:
    if not seconds:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def _list_text(sess: dict, page: int) -> str:
    per, items = sess["per_page"], sess["items"]
    start = page * per
    lines = [sess["header"], ""]
    for i, it in enumerate(items[start:start + per], start=start + 1):
        dur = _fmt_dur(it.duration)
        line = f"<b>{i}.</b> <i>{html.escape(it.title)}</i>"
        if dur:
            line += f"  {dur}"
        lines.append(line)
    return "\n".join(lines)


def _kb(token: str, sess: dict, page: int, _) -> InlineKeyboardMarkup:
    per, items = sess["per_page"], sess["items"]
    start = page * per
    chunk = items[start:start + per]
    rows: list[list[InlineKeyboardButton]] = []

    if sess.get("extras"):
        listen_url = sess.get("listen_url")
        if listen_url and urlparse(listen_url).scheme in {"http", "https"}:
            rows.append([
                InlineKeyboardButton(text="▶️ " + _("btn_listen"), url=listen_url)
            ])
        rows.append([InlineKeyboardButton(text="📃 " + _("btn_lyrics"), callback_data=f"lyr:{token}")])
        if items:
            rows.append([InlineKeyboardButton(text="🎬 " + _("btn_video"), callback_data=f"vid:{token}")])

    nums = [
        InlineKeyboardButton(text=str(start + i + 1), callback_data=f"pick:{token}:{start + i}")
        for i in range(len(chunk))
    ]
    rows += [nums[i:i + COLS] for i in range(0, len(nums), COLS)]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{token}:{page - 1}"))
    if start + per < len(items):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{token}:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def present(
    message: Message, _, db, *, header: str, items: list,
    per_page: int = 10, thumbnail: str | None = None,
    extras: bool = False, artist: str | None = None, title: str | None = None,
    listen_url: str | None = None,
    owner_user_id: int | None = None,
) -> None:
    """Render a final results list, optionally with album art."""
    if owner_user_id is None and message.from_user and not message.from_user.is_bot:
        owner_user_id = message.from_user.id
    sess = {
        "header": header, "items": items, "per_page": per_page,
        "extras": extras, "artist": artist, "title": title,
        "listen_url": listen_url,
        "owner_user_id": owner_user_id,
    }
    token = _remember(sess)
    text = _list_text(sess, 0)
    kb = _kb(token, sess, 0, _)
    persistence = asyncio.create_task(db.save_session(token, _serialize(sess)))
    try:
        if thumbnail:
            try:
                await message.answer_photo(
                    thumbnail, caption=text[:1024], reply_markup=kb
                )
                return
            except Exception:
                pass  # thumbnail not sendable → fall through to text
        await message.answer(text, reply_markup=kb)
    finally:
        try:
            await persistence
        except Exception:
            # The in-memory buttons still work; do not hide an otherwise valid
            # result merely because persistence had a transient volume error.
            logger.exception("Could not persist result button session token=%s", token)


async def _send_cached_track(
    callback: CallbackQuery, db, cache_key: str, item: SearchItem, caption: str,
) -> bool:
    cached = await db.get_cached_audio(cache_key)
    if not cached:
        return False
    try:
        await callback.message.answer_audio(
            cached, caption=caption, title=item.title or None,
            performer=item.uploader or None,
        )
    except TelegramBadRequest:
        logger.warning("Evicting invalid Telegram audio cache key=%s", cache_key)
        await db.delete_cached_audio(cache_key)
        return False
    return True


async def deliver_track(
    callback: CallbackQuery, _, config: Config, db, bot_username: str,
    item: SearchItem,
) -> None:
    """Deliver one exact search item through the shared cache/download path."""
    caption = f"👉 @{bot_username}"
    delivery_started = time.perf_counter()

    cache_key = downloader.telegram_media_cache_key(
        callback.bot.id, item.url, "audio", media_id=item.video_id
    )
    path: str | None = None
    stage = "telegram"
    claimed = False
    try:
        # Acknowledge immediately so Telegram removes the button spinner while
        # provider/cache work continues without a separate progress message.
        await callback.answer()
        stage = "cache"
        if await _send_cached_track(callback, db, cache_key, item, caption):
            logger.info(
                "track delivery video_id=%s cache_hit=true coalesced=false "
                "duration_ms=%s",
                item.video_id,
                round((time.perf_counter() - delivery_started) * 1000),
            )
            return

        # Only one user downloads/uploads a particular uncached track. Others
        # wait without consuming heavy capacity, then reuse its new file_id.
        async with downloader.media_singleflight_lock(cache_key):
            stage = "cache"
            if await _send_cached_track(callback, db, cache_key, item, caption):
                logger.info(
                    "track delivery video_id=%s cache_hit=true coalesced=true "
                    "duration_ms=%s",
                    item.video_id,
                    round((time.perf_counter() - delivery_started) * 1000),
                )
                return

            blocked_key = downloader.provider_error_key(item.url)
            if blocked_key:
                await callback.message.answer(_(blocked_key))
                return

            user_id = callback.from_user.id
            claim_error = jobs.try_claim(user_id)
            if claim_error:
                await callback.message.answer(_(claim_error))
                return
            claimed = True

            stage = "provider"
            provider_started = time.perf_counter()
            result = await downloader.download_audio(
                item.url, config.download_dir,
                max_bytes=config.max_file_mb * 1024 * 1024,
            )
            provider_ms = round((time.perf_counter() - provider_started) * 1000)
            path = result.path
            stage = "upload"
            output_bytes = os.path.getsize(path)
            size_mb = output_bytes / (1024 * 1024)
            if size_mb > config.max_file_mb:
                await callback.message.answer(
                    _("too_big", size=round(size_mb), limit=config.max_file_mb)
                )
                return
            upload_started = time.perf_counter()
            sent = await callback.message.answer_audio(
                FSInputFile(path), caption=caption,
                title=result.title or item.title,
                performer=result.uploader or item.uploader,
            )
            logger.info(
                "track delivery video_id=%s cache_hit=false ext=%s bytes=%s "
                "provider_ms=%s upload_ms=%s duration_ms=%s",
                item.video_id, result.ext, output_bytes, provider_ms,
                round((time.perf_counter() - upload_started) * 1000),
                round((time.perf_counter() - delivery_started) * 1000),
            )
            if sent.audio:
                try:
                    await db.set_cached_audio(
                        cache_key, sent.audio.file_id, item.title
                    )
                except Exception:
                    logger.exception(
                        "Audio delivered but cache write failed key=%s", cache_key
                    )
    except Exception as exc:
        if stage == "provider":
            downloader.record_provider_failure(item.url, exc)
        logger.error(
            "Track delivery failed video_id=%s stage=%s duration_ms=%s "
            "error=%s: %s",
            item.video_id, stage,
            round((time.perf_counter() - delivery_started) * 1000),
            type(exc).__name__,
            downloader.safe_error_message(exc),
        )
        if stage == "provider":
            failure = _(downloader.download_error_key(exc))
        elif stage == "upload":
            failure = _("upload_failed")
        else:
            failure = _("generic_error")
        await callback.message.answer(failure)
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        if claimed:
            jobs.release(callback.from_user.id)


@router.callback_query(F.data.startswith("pick:"))
async def on_pick(callback: CallbackQuery, _, config: Config, db, bot_username: str, **kwargs):
    try:
        _prefix, token, idx = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return
    if not idx.isdecimal():
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    sess = await _get_session(token, db)
    if not sess:
        await callback.answer(_("link_expired"), show_alert=True)
        return
    if sess.get("owner_user_id") not in {None, callback.from_user.id}:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    idx = int(idx)
    items = sess["items"]
    if not 0 <= idx < len(items):
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    await deliver_track(
        callback, _, config, db, bot_username, items[idx]
    )


@router.callback_query(F.data.startswith("page:"))
async def on_page(callback: CallbackQuery, _, db, **kwargs):
    try:
        _prefix, token, page = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return
    if not page.isdecimal():
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    sess = await _get_session(token, db)
    if not sess:
        await callback.answer()
        return
    if sess.get("owner_user_id") not in {None, callback.from_user.id}:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    page = int(page)
    page_count = (len(sess["items"]) + sess["per_page"] - 1) // sess["per_page"]
    if not 0 <= page < page_count:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_text(_list_text(sess, page), reply_markup=_kb(token, sess, page, _))
    except Exception:
        pass


@router.callback_query(F.data.startswith("lyr:"))
async def on_lyrics(callback: CallbackQuery, _, db, **kwargs):
    token = callback.data.split(":", 1)[1]
    sess = await _get_session(token, db)
    if not sess:
        await callback.answer(_("link_expired"), show_alert=True)
        return
    if sess.get("owner_user_id") not in {None, callback.from_user.id}:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    await callback.answer()
    text = await fetch_lyrics(sess.get("artist") or "", sess.get("title") or "")
    if not text:
        await callback.message.answer(_("lyrics_not_found"))
        return
    head = f"📃 <b>{html.escape(sess.get('artist') or '')} — {html.escape(sess.get('title') or '')}</b>\n\n"
    await callback.message.answer((head + html.escape(text))[:4096])


@router.callback_query(F.data.startswith("vid:"))
async def on_video(callback: CallbackQuery, _, db, **kwargs):
    token = callback.data.split(":", 1)[1]
    sess = await _get_session(token, db)
    if not sess or not sess["items"]:
        await callback.answer(_("link_expired"), show_alert=True)
        return
    if sess.get("owner_user_id") not in {None, callback.from_user.id}:
        await callback.answer(_("invalid_action"), show_alert=True)
        return
    await callback.answer()
    from bot.handlers.url_download import offer_qualities  # lazy: avoid import cycle
    await offer_qualities(
        callback.message, sess["items"][0].url, _, db,
        owner_user_id=callback.from_user.id,
    )
