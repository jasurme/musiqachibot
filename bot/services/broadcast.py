"""Reusable, paced delivery to every active private bot user.

The handler that starts a campaign supplies only ``send_one``.  This module
owns Telegram retry/error classification, inactive-user pruning, pagination,
and the process-wide lock, so manual and scheduled broadcasts cannot overlap.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)

from bot.config import Config

logger = logging.getLogger(__name__)

Outcome = Literal["sent", "inactive", "failed"]
SendOne = Callable[[int], Awaitable[Any]]
OutcomeHook = Callable[[int, Outcome], Awaitable[None]]

_BROADCAST_LOCK = asyncio.Lock()
_INACTIVE_RECIPIENT_ERROR_PARTS = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation",
    "bot cannot initiate conversation",
    "peer_id_invalid",
)


@dataclass(frozen=True)
class BroadcastStats:
    sent: int = 0
    inactive: int = 0
    failed: int = 0
    last_user_id: int = 0


async def _deliver_with_retries(send_one: SendOne, user_id: int) -> Outcome:
    """Deliver to one recipient with bounded, recipient-safe retries."""
    for attempt in range(3):
        try:
            await send_one(user_id)
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
            # Errors such as VOICE_MESSAGES_FORBIDDEN apply only to this media
            # and must not silently remove an otherwise reachable user.
            logger.warning(
                "Broadcast delivery rejected for one recipient error=%s",
                type(exc).__name__,
            )
            return "failed"
    return "failed"


async def broadcast_active(
    *,
    bot: Bot,
    db,
    config: Config,
    send_one: SendOne,
    after_user_id: int = 0,
    through_user_id: int | None = None,
    on_outcome: OutcomeHook | None = None,
    exclude_user_id: int | None = None,
    notification_mask: int | None = None,
) -> BroadcastStats:
    """Run one serial, keyset-paged campaign over active private users.

    ``through_user_id`` freezes a scheduled campaign's audience at a known
    upper bound. ``on_outcome`` runs after each recipient's final result and
    can durably checkpoint ``user_id`` before the next delivery. When
    ``notification_mask`` is supplied, users who disabled that category are
    excluded even if they were below the campaign's frozen upper boundary.
    """
    # ``bot`` is part of the uniform campaign interface; the supplied closure
    # normally captures it to perform the concrete Telegram method.
    sent = inactive = failed = 0
    cursor = max(0, int(after_user_id))
    ceiling = int(through_user_id) if through_user_id is not None else None
    interval = 1.0 / config.broadcast_rate_per_second

    async with _BROADCAST_LOCK:
        next_send_at = asyncio.get_running_loop().time()
        while ceiling is None or cursor < ceiling:
            user_ids = await db.get_active_user_ids(
                after_user_id=cursor,
                limit=500,
                exclude_user_id=exclude_user_id,
                through_user_id=ceiling,
                notification_mask=notification_mask,
            )
            if not user_ids:
                break

            for user_id in user_ids:
                delay = next_send_at - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)

                outcome = await _deliver_with_retries(send_one, user_id)
                next_send_at = max(
                    next_send_at + interval,
                    asyncio.get_running_loop().time(),
                )
                if outcome == "sent":
                    sent += 1
                elif outcome == "inactive":
                    inactive += 1
                    # Persist immediately so a later private interaction can
                    # safely reactivate this user during a long campaign.
                    await db.mark_users_inactive([user_id])
                else:
                    failed += 1

                cursor = user_id
                if on_outcome is not None:
                    await on_outcome(user_id, outcome)

    return BroadcastStats(
        sent=sent,
        inactive=inactive,
        failed=failed,
        last_user_id=cursor,
    )
