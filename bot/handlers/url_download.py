"""Feature D — send a link, pick quality (360/480/720/Audio), get the file.

1. On a supported link: `extract_meta` (no download) → preview photo + buttons.
2. On a button tap: download just that quality/audio and send it.

callback_data can't hold a full URL (64-byte cap), so URLs are stashed in a
short-lived in-memory map keyed by a random token.
"""
import html
import logging
import os
import re
import secrets
from collections import OrderedDict

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
from bot.utils import is_group, is_mentioned

router = Router(name="url_download")
logger = logging.getLogger(__name__)

_PENDING: "OrderedDict[str, dict]" = OrderedDict()
_PENDING_MAX = 500
VIDEO_TIERS = (360, 480, 720, 1080)


def purge_owner(owner_user_id: int) -> None:
    for token, item in list(_PENDING.items()):
        if item.get("owner_user_id") == owner_user_id:
            _PENDING.pop(token, None)


async def _remember(
    url: str, title: str, db, owner_user_id: int | None = None,
) -> str:
    token = secrets.token_urlsafe(6)
    item = {"url": url, "title": title, "owner_user_id": owner_user_id}
    _PENDING[token] = item
    while len(_PENDING) > _PENDING_MAX:
        _PENDING.popitem(last=False)
    await db.save_session(token, item)  # persist so buttons survive restarts
    return token


async def _get_pending(token: str, db) -> dict | None:
    item = _PENDING.get(token)
    if item is not None:
        _PENDING.move_to_end(token)
        return item
    data = await db.get_session(token)
    if data and "url" in data:
        _PENDING[token] = data
        while len(_PENDING) > _PENDING_MAX:
            _PENDING.popitem(last=False)
        return data
    return None


def _first_supported_url(text: str | None) -> str | None:
    if not text:
        return None
    for match in re.finditer(r"https?://[^\s<>\"']+", text, flags=re.IGNORECASE):
        url = match.group(0).rstrip(".,!?;:)]}")
        if downloader.is_supported_url(url):
            return url
    return None


def _has_supported_url(message: Message) -> bool:
    return _first_supported_url(message.text) is not None


async def _show_failure(message: Message, status: Message | None, text: str) -> None:
    if status is not None:
        try:
            await status.edit_text(text)
            return
        except Exception:
            pass
    await message.answer(text)


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
    rows.append([
        InlineKeyboardButton(
            text="🎵 " + _("btn_audio"), callback_data=f"dl:{token}:audio"
        )
    ])
    rows.append([InlineKeyboardButton(text="🔎 " + _("btn_find_music"),
                                      callback_data=f"dl:{token}:music")])
    rows.append([InlineKeyboardButton(text="⭕ " + _("btn_round"),
                                      callback_data=f"dl:{token}:round")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(_has_supported_url)
async def handle_url(message: Message, _, db, bot_username: str, **kwargs):
    # Links in busy groups are opt-in, just like text search.
    if is_group(message) and not is_mentioned(message, bot_username):
        return
    url = _first_supported_url(message.text)
    if not url:
        return
    await offer_qualities(
        message, url, _, db, owner_user_id=message.from_user.id
    )


async def offer_qualities(
    message: Message, url: str, _, db, owner_user_id: int | None = None,
) -> None:
    """Fetch metadata (no download) and show the preview + quality buttons.
    Reused by the recognition 'Video' button as well as direct links."""
    blocked_key = downloader.provider_error_key(url)
    if blocked_key:
        await message.answer(_(blocked_key))
        return
    status = await message.answer(_("fetching"))
    try:
        meta = await downloader.extract_meta(url)
    except Exception as exc:
        downloader.record_provider_failure(url, exc)
        logger.error(
            "Metadata extraction failed source=%s error=%s: %s",
            downloader.provider_name(url), type(exc).__name__,
            downloader.safe_error_message(exc),
        )
        await _show_failure(
            message, status, _(downloader.download_error_key(exc))
        )
        return

    if owner_user_id is None and message.from_user and not message.from_user.is_bot:
        owner_user_id = message.from_user.id
    token = await _remember(meta.url, meta.title, db, owner_user_id)
    kb = _quality_keyboard(token, meta.heights, _)
    caption = ("🎬 " + html.escape(meta.title[:900])) if meta.title else _("choose_quality")

    try:
        await status.delete()
    except Exception:
        logger.debug("Could not delete metadata status message", exc_info=True)
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

    allowed = {"audio", "music", "round", *(str(tier) for tier in VIDEO_TIERS)}
    if quality not in allowed:
        await callback.answer(_("invalid_action"), show_alert=True)
        return

    item = await _get_pending(token, db)
    if not item:
        await callback.answer(_("link_expired"), show_alert=True)
        return
    owner_user_id = item.get("owner_user_id")
    if owner_user_id is not None and owner_user_id != callback.from_user.id:
        await callback.answer(_("invalid_action"), show_alert=True)
        return

    sig = f"👉 @{bot_username}"
    title = item.get("title") or ""
    video_caption = (f"🎬 {html.escape(title[:900])}\n\n{sig}") if title else sig
    cache_key = f"bot:{callback.bot.id}:dl:{item['url']}:{quality}"

    # Cached Telegram file_ids need no source access. Serve them even while the
    # provider circuit is cooling down, and do not occupy an expensive-job slot.
    if quality not in {"music", "round"}:
        try:
            cached = await db.get_cached_audio(cache_key)
        except Exception as exc:
            logger.error("Media cache lookup failed error=%s", type(exc).__name__)
            await callback.answer(_("generic_error"), show_alert=True)
            return
        if cached:
            try:
                if quality == "audio":
                    await callback.message.answer_audio(cached, caption=sig)
                else:
                    await callback.message.answer_video(cached, caption=video_caption)
            except TelegramBadRequest:
                logger.warning("Evicting invalid Telegram media cache key=%s", cache_key)
                try:
                    await db.delete_cached_audio(cache_key)
                except Exception as exc:
                    logger.error("Media cache eviction failed error=%s", type(exc).__name__)
                    await callback.answer(_("generic_error"), show_alert=True)
                    return
            except Exception as exc:
                logger.error("Cached media delivery failed error=%s", type(exc).__name__)
                await callback.answer(_("generic_error"), show_alert=True)
                return
            else:
                await callback.answer()
                return

    blocked_key = downloader.provider_error_key(item["url"])
    if blocked_key:
        await callback.answer(_(blocked_key), show_alert=True)
        return

    user_id = callback.from_user.id
    claim_error = jobs.try_claim(user_id)
    if claim_error:
        await callback.answer(_(claim_error), show_alert=True)
        return

    try:
        await callback.answer()
        # "Find music" — fingerprint the song playing in the video, then present it
        if quality == "music":
            status = await callback.message.answer(_("recognizing"))
            audio_path: str | None = None
            try:
                result = await downloader.download_audio(
                    item["url"], config.download_dir,
                    max_bytes=config.max_file_mb * 1024 * 1024,
                )
                audio_path = result.path
                from bot.handlers.media_recognize import recognize_and_present
                await recognize_and_present(
                    callback.message, _, config, db, audio_path, status=status,
                    owner_user_id=callback.from_user.id,
                )
            except Exception as exc:
                downloader.record_provider_failure(item["url"], exc)
                logger.error(
                    "Link recognition failed source=%s error=%s: %s",
                    downloader.provider_name(item["url"]), type(exc).__name__,
                    downloader.safe_error_message(exc),
                )
                key = downloader.download_error_key(exc)
                if key == "download_failed":
                    key = "generic_error"
                await _show_failure(callback.message, status, _(key))
            finally:
                if audio_path and os.path.exists(audio_path):
                    try:
                        os.remove(audio_path)
                    except OSError:
                        pass
            return

        # "Yumaloq video" — convert the video into a round video-note
        if quality == "round":
            status = await callback.message.answer(_("round_processing"))
            vpath: str | None = None
            try:
                result = await downloader.download_video_quality(
                    item["url"], config.download_dir, 480,
                    max_bytes=config.max_file_mb * 1024 * 1024,
                )
                vpath = result.path
                from bot.handlers.round import deliver_round
                await deliver_round(callback.message, vpath, config, _, status=status)
            except Exception as exc:
                downloader.record_provider_failure(item["url"], exc)
                logger.error(
                    "Round-link job failed source=%s error=%s: %s",
                    downloader.provider_name(item["url"]), type(exc).__name__,
                    downloader.safe_error_message(exc),
                )
                key = downloader.download_error_key(exc)
                if key == "download_failed":
                    key = "round_failed"
                await _show_failure(callback.message, status, _(key))
            finally:
                if vpath and os.path.exists(vpath):
                    try:
                        os.remove(vpath)
                    except OSError:
                        pass
            return

        label = _("btn_audio") if quality == "audio" else f"{quality}p"
        status = await callback.message.answer(_("downloading_quality", quality=label))

        path: str | None = None
        stage = "provider"
        try:
            if quality == "audio":
                result = await downloader.download_audio(
                    item["url"], config.download_dir,
                    max_bytes=config.max_file_mb * 1024 * 1024,
                )
            else:
                result = await downloader.download_video_quality(
                    item["url"], config.download_dir, int(quality),
                    max_bytes=config.max_file_mb * 1024 * 1024,
                )
            path = result.path
            stage = "upload"

            size_mb = os.path.getsize(path) / (1024 * 1024)
            if size_mb > config.max_file_mb:
                await status.edit_text(
                    _("too_big", size=round(size_mb), limit=config.max_file_mb)
                )
                return

            if quality == "audio":
                sent = await callback.message.answer_audio(
                    FSInputFile(path), caption=sig,
                    title=result.title or None, performer=result.uploader or None,
                )
                file_id = sent.audio.file_id if sent.audio else None
            else:
                sent = await callback.message.answer_video(
                    FSInputFile(path), caption=video_caption,
                    supports_streaming=True,
                )
                file_id = sent.video.file_id if sent.video else None
            if file_id:
                try:
                    await db.set_cached_audio(cache_key, file_id, title)
                except Exception:
                    logger.exception("Media delivered but cache write failed key=%s", cache_key)
            try:
                await status.delete()
            except Exception:
                logger.debug("Could not delete download status message", exc_info=True)
        except Exception as exc:
            if stage == "provider":
                downloader.record_provider_failure(item["url"], exc)
            logger.error(
                "Media delivery failed source=%s quality=%s stage=%s error=%s: %s",
                downloader.provider_name(item["url"]), quality, stage,
                type(exc).__name__,
                downloader.safe_error_message(exc),
            )
            key = (
                downloader.download_error_key(exc)
                if stage == "provider" else "upload_failed"
            )
            await _show_failure(callback.message, status, _(key))
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
    finally:
        jobs.release(user_id)
