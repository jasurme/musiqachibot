"""Administrator announcements copied to every active private bot user."""

import asyncio
import logging

from aiogram import Bot, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Filter
from aiogram.types import Message

from bot.config import Config

router = Router(name="broadcast")
logger = logging.getLogger(__name__)

_BROADCAST_LOCK = asyncio.Lock()
_INACTIVE_RECIPIENT_ERROR_PARTS = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation",
    "bot cannot initiate conversation",
    "peer_id_invalid",
)
_COPYABLE_CONTENT_TYPES = {
    "animation", "audio", "contact", "dice", "document", "game",
    "location", "photo", "poll", "sticker", "text", "venue", "video",
    "video_note", "voice",
}


class AdminPrivateFilter(Filter):
    """Match only genuine private messages from the configured administrator."""

    async def __call__(self, message: Message, config: Config) -> bool:
        return bool(
            message.from_user
            and message.chat.type == "private"
            and message.from_user.id == config.admin_user_id
        )


async def _copy_to_user(bot: Bot, message: Message, user_id: int) -> str:
    """Copy once with bounded Telegram retries.

    Returns ``sent``, ``inactive``, or ``failed``. Only recipient-specific
    errors deactivate a user; an unsupported source message must not disable
    the entire audience.
    """
    for attempt in range(3):
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            return "sent"
        except TelegramRetryAfter as exc:
            if attempt >= 2:
                return "failed"
            await asyncio.sleep(max(0.0, float(exc.retry_after)) + 0.1)
        except (TelegramNetworkError, TelegramServerError):
            if attempt >= 2:
                return "failed"
            await asyncio.sleep(0.5 * (2 ** attempt))
        except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as exc:
            error = str(exc).lower()
            if any(part in error for part in _INACTIVE_RECIPIENT_ERROR_PARTS):
                return "inactive"
            logger.warning(
                "Broadcast copy rejected for one recipient error=%s",
                type(exc).__name__,
            )
            return "failed"
    return "failed"


@router.message(AdminPrivateFilter())
async def broadcast_admin_message(
    message: Message, bot: Bot, db, config: Config, _, **kwargs,
) -> None:
    """Copy any copyable admin message/media to every active private user."""
    if message.content_type not in _COPYABLE_CONTENT_TYPES:
        await message.answer(_("broadcast_unsupported"))
        return

    async with _BROADCAST_LOCK:
        sent = inactive = failed = 0
        after_user_id = 0
        interval = 1.0 / config.broadcast_rate_per_second
        next_send_at = asyncio.get_running_loop().time()
        logger.info(
            "Broadcast started source_message_id=%s", message.message_id
        )

        while True:
            user_ids = await db.get_active_user_ids(
                after_user_id=after_user_id,
                limit=500,
                exclude_user_id=config.admin_user_id,
            )
            if not user_ids:
                break
            for user_id in user_ids:
                delay = next_send_at - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)
                outcome = await _copy_to_user(bot, message, user_id)
                next_send_at = max(
                    next_send_at + interval,
                    asyncio.get_running_loop().time(),
                )
                if outcome == "sent":
                    sent += 1
                elif outcome == "inactive":
                    inactive += 1
                    # Persist immediately: if the user returns during a long
                    # campaign, the tracking middleware can safely reactivate
                    # them after this write instead of being overwritten by a
                    # delayed end-of-batch update.
                    await db.mark_users_inactive([user_id])
                else:
                    failed += 1
                after_user_id = user_id

        logger.info(
            "Broadcast completed source_message_id=%s sent=%s inactive=%s failed=%s",
            message.message_id, sent, inactive, failed,
        )
        await message.answer(
            _(
                "broadcast_done",
                sent=sent,
                inactive=inactive,
                failed=failed,
            )
        )
