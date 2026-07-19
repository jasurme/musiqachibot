"""Round video-notes (yumaloq video).

Two entry points:
  • `/round` command → prompt → the next video/link the user sends is converted.
  • the "⭕ Yumaloq video" button on a link (handled in url_download → deliver_round).
"""
import os

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, Message

from bot.config import Config
from bot.handlers.url_download import _first_supported_url
from bot.services import downloader
from bot.services import video as video_svc

router = Router(name="round")


class RoundFlow(StatesGroup):
    waiting = State()


def _is_link(message: Message) -> bool:
    return _first_supported_url(message.text) is not None


async def _fail(message: Message, status, text: str) -> None:
    if status is not None:
        try:
            await status.edit_text(text)
            return
        except Exception:
            pass
    await message.answer(text)


async def deliver_round(message: Message, src_path: str, config, _, status=None) -> None:
    """Convert a local video file to a round note and send it."""
    note = None
    try:
        note = await video_svc.make_video_note(src_path, config.download_dir)
        size_mb = os.path.getsize(note) / (1024 * 1024)
        if size_mb > config.max_file_mb:
            await _fail(message, status, _("too_big", size=round(size_mb)))
            return
        if status is not None:
            try:
                await status.delete()
            except Exception:
                pass
        await message.answer_video_note(FSInputFile(note), length=480)
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
    status = await message.answer(_("round_processing"))
    src = os.path.join(config.download_dir, f"round_{media.file_unique_id}")
    try:
        os.makedirs(config.download_dir, exist_ok=True)
        await bot.download(media, destination=src)
        await deliver_round(message, src, config, _, status=status)
    except Exception:
        await _fail(message, status, _("round_failed"))
    finally:
        if os.path.exists(src):
            try:
                os.remove(src)
            except OSError:
                pass


@router.message(StateFilter(RoundFlow.waiting), _is_link)
async def round_from_link(message: Message, _, state: FSMContext, config: Config, **kwargs):
    await state.clear()
    url = _first_supported_url(message.text)
    status = await message.answer(_("round_processing"))
    vpath = None
    try:
        result = await downloader.download_video_quality(url, config.download_dir, 480)
        vpath = result.path
        await deliver_round(message, vpath, config, _, status=status)
    except Exception:
        await _fail(message, status, _("round_failed"))
    finally:
        if vpath and os.path.exists(vpath):
            try:
                os.remove(vpath)
            except OSError:
                pass
