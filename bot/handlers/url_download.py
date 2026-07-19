"""Feature D — send a link, pick quality (360/480/720/Audio), get the file.

1. On a supported link: `extract_meta` (no download) → preview photo + buttons.
2. On a button tap: download just that quality/audio and send it.

callback_data can't hold a full URL (64-byte cap), so URLs are stashed in a
short-lived in-memory map keyed by a random token.
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

router = Router(name="url_download")

_PENDING: "OrderedDict[str, dict]" = OrderedDict()
_PENDING_MAX = 500
VIDEO_TIERS = (360, 480, 720, 1080)


async def _remember(url: str, title: str, db) -> str:
    token = secrets.token_urlsafe(6)
    item = {"url": url, "title": title}
    _PENDING[token] = item
    while len(_PENDING) > _PENDING_MAX:
        _PENDING.popitem(last=False)
    await db.save_session(token, item)  # persist so buttons survive restarts
    return token


async def _get_pending(token: str, db) -> dict | None:
    item = _PENDING.get(token)
    if item is not None:
        return item
    data = await db.get_session(token)
    if data and "url" in data:
        _PENDING[token] = data
        return data
    return None


def _first_supported_url(text: str | None) -> str | None:
    if not text:
        return None
    for tok in text.split():
        if tok.startswith("http") and downloader.is_supported_url(tok):
            return tok
    return None


def _has_supported_url(message: Message) -> bool:
    return _first_supported_url(message.text) is not None


def _quality_keyboard(token: str, heights: list[int], _) -> InlineKeyboardMarkup:
    maxh = max(heights) if heights else None
    tiers = [t for t in VIDEO_TIERS if (maxh is None or t <= maxh)]
    if not tiers:  # very low-res source
        tiers = [min(VIDEO_TIERS, key=lambda x: abs(x - (maxh or 360)))]
    btns = [
        InlineKeyboardButton(text=f"🎬 {t}p", callback_data=f"dl:{token}:{t}")
        for t in tiers
    ]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="🎵 Audio", callback_data=f"dl:{token}:audio")])
    rows.append([InlineKeyboardButton(text="🔎 " + _("btn_find_music"),
                                      callback_data=f"dl:{token}:music")])
    rows.append([InlineKeyboardButton(text="⭕ " + _("btn_round"),
                                      callback_data=f"dl:{token}:round")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(_has_supported_url)
async def handle_url(message: Message, _, db, **kwargs):
    url = _first_supported_url(message.text)
    if not url:
        return
    await offer_qualities(message, url, _, db)


async def offer_qualities(message: Message, url: str, _, db) -> None:
    """Fetch metadata (no download) and show the preview + quality buttons.
    Reused by the recognition 'Video' button as well as direct links."""
    status = await message.answer(_("fetching"))
    try:
        meta = await downloader.extract_meta(url)
    except Exception:
        await status.edit_text(_("download_failed"))
        return

    token = await _remember(meta.url, meta.title, db)
    kb = _quality_keyboard(token, meta.heights, _)
    caption = ("🎬 " + html.escape(meta.title[:900])) if meta.title else _("choose_quality")

    await status.delete()
    try:
        if meta.thumbnail:
            await message.answer_photo(meta.thumbnail, caption=caption, reply_markup=kb)
        else:
            await message.answer(caption, reply_markup=kb)
    except Exception:
        # thumbnail not fetchable by Telegram → fall back to text
        await message.answer(caption, reply_markup=kb)


@router.callback_query(F.data.startswith("dl:"))
async def on_quality(callback: CallbackQuery, _, config: Config, db, bot_username: str, **kwargs):
    try:
        _prefix, token, quality = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return

    item = await _get_pending(token, db)
    if not item:
        await callback.answer(_("link_expired"), show_alert=True)
        return

    # "Find music" — fingerprint the song playing in the video, then present it
    if quality == "music":
        await callback.answer(_("recognizing"))
        status = await callback.message.answer(_("recognizing"))
        audio_path: str | None = None
        try:
            result = await downloader.download_audio(item["url"], config.download_dir)
            audio_path = result.path
            from bot.handlers.media_recognize import recognize_and_present
            await recognize_and_present(callback.message, _, config, db, audio_path, status=status)
        except Exception:
            try:
                await status.edit_text(_("not_recognized"))
            except Exception:
                pass
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
        return

    # "Yumaloq video" — convert the video into a round video-note
    if quality == "round":
        await callback.answer(_("round_processing"))
        status = await callback.message.answer(_("round_processing"))
        vpath: str | None = None
        try:
            result = await downloader.download_video_quality(item["url"], config.download_dir, 480)
            vpath = result.path
            from bot.handlers.round import deliver_round
            await deliver_round(callback.message, vpath, config, _, status=status)
        except Exception:
            try:
                await status.edit_text(_("round_failed"))
            except Exception:
                pass
        finally:
            if vpath and os.path.exists(vpath):
                try:
                    os.remove(vpath)
                except OSError:
                    pass
        return

    sig = f"👉 @{bot_username}"
    title = item.get("title") or ""
    video_caption = (f"🎬 {html.escape(title[:900])}\n\n{sig}") if title else sig

    # Cache hit → resend the stored file_id instantly.
    cache_key = f"dl:{item['url']}:{quality}"
    cached = await db.get_cached_audio(cache_key)
    if cached:
        await callback.answer()
        if quality == "audio":
            await callback.message.answer_audio(cached, caption=sig)
        else:
            await callback.message.answer_video(cached, caption=video_caption)
        return

    label = "Audio" if quality == "audio" else f"{quality}p"
    await callback.answer(_("downloading_quality", quality=label))
    status = await callback.message.answer(_("downloading_quality", quality=label))

    path: str | None = None
    try:
        if quality == "audio":
            result = await downloader.download_audio(item["url"], config.download_dir)
        else:
            result = await downloader.download_video_quality(
                item["url"], config.download_dir, int(quality)
            )
        path = result.path

        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > config.max_file_mb:
            await status.edit_text(_("too_big", size=round(size_mb)))
            return

        if quality == "audio":
            sent = await callback.message.answer_audio(
                FSInputFile(path), caption=sig,
                title=result.title or None, performer=result.uploader or None,
            )
            file_id = sent.audio.file_id if sent.audio else None
        else:
            sent = await callback.message.answer_video(FSInputFile(path), caption=video_caption)
            file_id = sent.video.file_id if sent.video else None
        if file_id:
            await db.set_cached_audio(cache_key, file_id, title)
        await status.delete()
    except Exception:
        await status.edit_text(_("download_failed"))
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
