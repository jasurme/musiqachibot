"""Shared results list — a header + numbered tracks + number buttons, optionally
with Lyrics/Video buttons and album art. Used by Feature A (search) and
Feature C (recognition). Owns the pick / page / lyrics / video callbacks.
"""
import html
import os
import secrets
from collections import OrderedDict

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import Config
from bot.services import downloader
from bot.services.lyrics import fetch_lyrics
from bot.services.search import SearchItem

router = Router(name="results")

# in-memory cache in front of the DB (fast path); the DB copy survives restarts
_SESS: "OrderedDict[str, dict]" = OrderedDict()
_SESS_MAX = 500
COLS = 5


def _serialize(sess: dict) -> dict:
    return {
        "header": sess["header"],
        "per_page": sess["per_page"],
        "extras": sess.get("extras", False),
        "artist": sess.get("artist"),
        "title": sess.get("title"),
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
    }


async def _remember(sess: dict, db) -> str:
    token = secrets.token_urlsafe(6)
    _SESS[token] = sess
    while len(_SESS) > _SESS_MAX:
        _SESS.popitem(last=False)
    await db.save_session(token, _serialize(sess))
    return token


async def _get_session(token: str, db) -> dict | None:
    sess = _SESS.get(token)
    if sess is not None:
        return sess
    data = await db.get_session(token)
    if not data:
        return None
    sess = _deserialize(data)
    _SESS[token] = sess  # warm the cache
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
        rows.append([InlineKeyboardButton(text="📃 " + _("btn_lyrics"), callback_data=f"lyr:{token}")])
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
    edit: Message | None = None,
) -> None:
    """Render the results list. `edit` reuses a status message (search flow);
    `thumbnail` sends album art with the list as caption (recognition flow)."""
    sess = {
        "header": header, "items": items, "per_page": per_page,
        "extras": extras, "artist": artist, "title": title,
    }
    token = await _remember(sess, db)
    text = _list_text(sess, 0)
    kb = _kb(token, sess, 0, _)

    if thumbnail:
        try:
            await message.answer_photo(thumbnail, caption=text[:1024], reply_markup=kb)
            return
        except Exception:
            pass  # thumbnail not sendable → fall through to text
    if edit is not None:
        await edit.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pick:"))
async def on_pick(callback: CallbackQuery, _, config: Config, db, bot_username: str, **kwargs):
    try:
        _prefix, token, idx = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return
    sess = await _get_session(token, db)
    if not sess:
        await callback.answer(_("link_expired"), show_alert=True)
        return
    idx = int(idx)
    items = sess["items"]
    if idx >= len(items):
        await callback.answer()
        return
    item = items[idx]
    caption = f"👉 @{bot_username}"

    cache_key = f"ytaudio:{item.video_id}"
    cached = await db.get_cached_audio(cache_key)
    if cached:
        await callback.answer()
        await callback.message.answer_audio(
            cached, caption=caption, title=item.title or None, performer=item.uploader or None
        )
        return

    await callback.answer(_("sending_track"))
    status = await callback.message.answer(_("sending_track"))
    path: str | None = None
    try:
        result = await downloader.download_audio(item.url, config.download_dir)
        path = result.path
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > config.max_file_mb:
            await status.edit_text(_("too_big", size=round(size_mb)))
            return
        sent = await callback.message.answer_audio(
            FSInputFile(path), caption=caption,
            title=result.title or item.title, performer=result.uploader or item.uploader,
        )
        if sent.audio:
            await db.set_cached_audio(cache_key, sent.audio.file_id, item.title)
        await status.delete()
    except Exception:
        await status.edit_text(_("download_failed"))
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


@router.callback_query(F.data.startswith("page:"))
async def on_page(callback: CallbackQuery, _, db, **kwargs):
    try:
        _prefix, token, page = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return
    sess = await _get_session(token, db)
    if not sess:
        await callback.answer()
        return
    page = int(page)
    try:
        await callback.message.edit_text(_list_text(sess, page), reply_markup=_kb(token, sess, page, _))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("lyr:"))
async def on_lyrics(callback: CallbackQuery, _, db, **kwargs):
    token = callback.data.split(":", 1)[1]
    sess = await _get_session(token, db)
    if not sess:
        await callback.answer(_("link_expired"), show_alert=True)
        return
    await callback.answer(_("searching"))
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
    await callback.answer()
    from bot.handlers.url_download import offer_qualities  # lazy: avoid import cycle
    await offer_qualities(callback.message, sess["items"][0].url, _, db)
