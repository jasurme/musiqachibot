"""Feature C — recognize music from voice / audio / video / video-note.

`recognize_and_present` (fingerprint a local file → results list with album art +
Song/Artist header + Lyrics/Video buttons) is shared with the link "Find music"
button in url_download.
"""
import html
import logging
import os
import tempfile

from aiogram import F, Router
from aiogram.types import Message

from bot.config import Config
from bot import jobs
from bot.handlers.results import present
from bot.services import audio as audio_svc
from bot.services.recognizer import Track, get_recognizer
from bot.services.search import search_tracks
from bot.utils import is_group

router = Router(name="media_recognize")
logger = logging.getLogger(__name__)


async def _fail(message: Message, status, text: str) -> None:
    if status is not None:
        try:
            await status.edit_text(text)
            return
        except Exception:
            pass
    await message.answer(text)


async def _present_track(
    message, _, db, track: Track, status=None,
    owner_user_id: int | None = None,
) -> None:
    header = _(
        "rec_header", title=html.escape(track.title), artist=html.escape(track.artist)
    )
    try:
        items = await search_tracks(track.query, limit=5)
    except Exception as exc:
        # Fingerprinting already succeeded. YouTube search is an optional
        # enrichment and must not turn a recognized song into a false failure.
        from bot.services.downloader import safe_error_message
        logger.warning(
            "Recognition enrichment search failed error=%s: %s",
            type(exc).__name__, safe_error_message(exc),
        )
        items = []
    if status is not None:
        try:
            await status.delete()
        except Exception:
            logger.debug("Could not delete recognition status message", exc_info=True)
    if owner_user_id is None and message.from_user and not message.from_user.is_bot:
        owner_user_id = message.from_user.id
    await present(
        message, _, db,
        header=header, items=items, per_page=5,
        thumbnail=track.cover, extras=True,
        artist=track.artist, title=track.title,
        listen_url=track.url,
        owner_user_id=owner_user_id,
    )


async def recognize_and_present(
    message, _, config, db, media_path, status=None, recognition_key: str | None = None,
    owner_user_id: int | None = None,
) -> None:
    """Fingerprint local media and present a cached, provider-backed track card."""
    samples: list[str] = []
    try:
        sample_dir = os.path.dirname(os.path.abspath(media_path)) or config.download_dir
        recognizer = get_recognizer(config)
        sample = await audio_svc.make_sample(media_path, sample_dir, seconds=12)
        samples.append(sample)
        track = await recognizer.recognize(sample)
        if not track:
            # Intros and speech overlays are common in social clips. Try one
            # later slice only after a clean no-match, keeping the fast path fast.
            try:
                alternate = await audio_svc.make_sample(
                    media_path, sample_dir, seconds=12, start_seconds=12
                )
                samples.append(alternate)
                track = await recognizer.recognize(alternate)
            except Exception:
                logger.debug("Alternate recognition slice unavailable", exc_info=True)
        if not track:
            await _fail(message, status, _("not_recognized"))
            return
        if recognition_key:
            try:
                await db.set_recognition(
                    recognition_key, track.title, track.artist, track.url, track.cover,
                    owner_user_id=owner_user_id,
                )
            except Exception:
                logger.exception("Recognition succeeded but cache write failed")
        await _present_track(
            message, _, db, track, status=status,
            owner_user_id=owner_user_id,
        )
    finally:
        for sample in samples:
            if os.path.exists(sample):
                try:
                    os.remove(sample)
                except OSError:
                    pass


@router.message(F.voice | F.audio | F.video | F.video_note)
async def handle_media(message: Message, _, config: Config, db, bot, **kwargs):
    if is_group(message):
        return  # recognition is DM-only (avoid firing on every group clip)
    media = message.voice or message.audio or message.video or message.video_note
    if media.file_size and media.file_size > config.max_input_mb * 1024 * 1024:
        await message.answer(
            _(
                "too_big", size=round(media.file_size / (1024 * 1024)),
                limit=config.max_input_mb,
            )
        )
        return
    user_id = message.from_user.id
    claim_error = jobs.try_claim(user_id)
    if claim_error:
        await message.answer(_(claim_error))
        return
    status = None
    try:
        status = await message.answer(_("recognizing"))
        cached = await db.get_recognition(
            media.file_unique_id, owner_user_id=user_id
        )
        if cached:
            await _present_track(message, _, db, Track(**cached), status=status)
            return
        os.makedirs(config.download_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".musiqa_recognize_", dir=config.download_dir
        ) as work_dir:
            src = os.path.join(work_dir, "input_media")
            await bot.download(media, destination=src, timeout=300)
            await recognize_and_present(
                message, _, config, db, src, status=status,
                recognition_key=media.file_unique_id,
                owner_user_id=user_id,
            )
    except Exception:
        logger.exception("Media recognition failed type=%s", type(media).__name__)
        await _fail(message, status, _("recognition_failed"))
    finally:
        jobs.release(user_id)
