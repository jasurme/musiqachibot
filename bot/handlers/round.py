"""Round video-notes (yumaloq video).

Two entry points:
  • `/round` command → prompt → the next video/link the user sends is converted.
  • the "⭕ Yumaloq video" button on a link (handled in url_download → deliver_round).
"""
import os
import logging
import tempfile

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, Message

from bot.config import Config
from bot import jobs
from bot.handlers.url_download import _first_supported_url
from bot.services import downloader
from bot.services import video as video_svc

router = Router(name="round")
logger = logging.getLogger(__name__)


class RoundFlow(StatesGroup):
    waiting = State()


def _is_link(message: Message) -> bool:
    return _first_supported_url(message.text) is not None


async def _fail(message: Message, text: str) -> None:
    await message.answer(text)


async def deliver_round(message: Message, src_path: str, config, _) -> None:
    """Convert a local video file to a round note and send it."""
    note = None
    try:
        note_dir = os.path.dirname(os.path.abspath(src_path)) or config.download_dir
        note = await video_svc.make_video_note(
            src_path, note_dir,
            max_bytes=config.max_file_mb * 1024 * 1024,
        )
        size_mb = os.path.getsize(note) / (1024 * 1024)
        if size_mb > config.max_file_mb:
            await _fail(
                message, _("too_big", size=round(size_mb), limit=config.max_file_mb)
            )
            return
        await message.answer_video_note(
            FSInputFile(note), length=480
        )
    finally:
        if note and os.path.exists(note):
            try:
                os.remove(note)
            except OSError:
                pass


@router.message(Command("round"))
async def cmd_round(message: Message, _, state: FSMContext, **kwargs):
    await state.set_state(RoundFlow.waiting)
    await message.answer(_("round_prompt"))


@router.message(StateFilter(RoundFlow.waiting), F.video | F.video_note | F.animation)
async def round_from_video(message: Message, _, state: FSMContext, config: Config, bot, **kwargs):
    await state.clear()
    media = message.video or message.video_note or message.animation
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
    try:
        os.makedirs(config.download_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".musiqa_round_", dir=config.download_dir
        ) as work_dir:
            src = os.path.join(work_dir, "input_video")
            await bot.download(media, destination=src, timeout=300)
            await deliver_round(message, src, config, _)
    except Exception as exc:
        logger.error(
            "Round conversion failed type=%s error=%s: %s",
            type(media).__name__, type(exc).__name__,
            downloader.safe_error_message(exc),
        )
        key = downloader.download_error_key(exc)
        await _fail(
            message, _("round_failed" if key == "download_failed" else key)
        )
    finally:
        jobs.release(user_id)


@router.message(StateFilter(RoundFlow.waiting), _is_link)
async def round_from_link(message: Message, _, state: FSMContext, config: Config, **kwargs):
    await state.clear()
    url = _first_supported_url(message.text)
    if not url:
        await message.answer(_("unsupported_link"))
        return
    user_id = message.from_user.id
    claim_error = jobs.try_claim(user_id)
    if claim_error:
        await message.answer(_(claim_error))
        return
    vpath = None
    try:
        result = await downloader.download_video_quality(
            url, config.download_dir, 480,
            max_bytes=config.max_file_mb * 1024 * 1024,
        )
        vpath = result.path
        await deliver_round(message, vpath, config, _)
    except Exception as exc:
        downloader.record_provider_failure(url, exc)
        logger.error(
            "Round-link conversion failed source=%s error=%s: %s",
            downloader.provider_name(url), type(exc).__name__,
            downloader.safe_error_message(exc),
        )
        key = downloader.download_error_key(exc)
        if key == "download_failed":
            key = "round_failed"
        await _fail(message, _(key))
    finally:
        if vpath and os.path.exists(vpath):
            try:
                os.remove(vpath)
            except OSError:
                pass
        jobs.release(user_id)
