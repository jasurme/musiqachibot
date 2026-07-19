"""Feature C — recognize music from voice / audio / video / video-note.

`recognize_and_present` (fingerprint a local file → results list with album art +
Song/Artist header + Lyrics/Video buttons) is shared with the link "Find music"
button in url_download.
"""
import html
import os

from aiogram import F, Router
from aiogram.types import Message

from bot.config import Config
from bot.handlers.results import present
from bot.services import audio as audio_svc
from bot.services.recognizer import get_recognizer
from bot.services.search import search_tracks
from bot.utils import is_group

router = Router(name="media_recognize")


async def _fail(message: Message, status, text: str) -> None:
    if status is not None:
        try:
            await status.edit_text(text)
            return
        except Exception:
            pass
    await message.answer(text)


async def recognize_and_present(message, _, config, db, media_path, status=None) -> None:
    """Fingerprint a local audio/video file and present the match as a results
    list. Used by media messages AND the link 'Find music' button."""
    sample = None
    try:
        sample = await audio_svc.make_sample(media_path, config.download_dir, seconds=15)
        track = await get_recognizer(config).recognize(sample)
        if not track:
            await _fail(message, status, _("not_recognized"))
            return
        header = _("rec_header", title=html.escape(track.title), artist=html.escape(track.artist))
        items = await search_tracks(track.query, limit=5)
        if not items:
            await _fail(message, status, header)
            return
        if status is not None:
            try:
                await status.delete()
            except Exception:
                pass
        await present(
            message, _, db,
            header=header, items=items, per_page=5,
            thumbnail=track.cover, extras=True,
            artist=track.artist, title=track.title,
        )
    finally:
        if sample and os.path.exists(sample):
            try:
                os.remove(sample)
            except OSError:
                pass


@router.message(F.voice | F.audio | F.video | F.video_note)
async def handle_media(message: Message, _, config: Config, db, bot, **kwargs):
    if is_group(message):
        return  # recognition is DM-only (avoid firing on every group clip)
    media = message.voice or message.audio or message.video or message.video_note
    status = await message.answer(_("recognizing"))
    src = os.path.join(config.download_dir, f"rec_{media.file_unique_id}")
    try:
        os.makedirs(config.download_dir, exist_ok=True)
        await bot.download(media, destination=src)
        await recognize_and_present(message, _, config, db, src, status=status)
    except Exception:
        await _fail(message, status, _("not_recognized"))
    finally:
        if os.path.exists(src):
            try:
                os.remove(src)
            except OSError:
                pass
