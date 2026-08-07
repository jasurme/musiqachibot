"""Administrator announcements and confirmed media campaigns."""

from __future__ import annotations

import asyncio  # compatibility hook for broadcast pacing tests/operations
import logging
import os
import secrets
import tempfile
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Filter
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import jobs
from bot.config import Config
from bot.i18n import t
from bot.services import broadcast as broadcast_svc
from bot.services import video as video_svc

router = Router(name="broadcast")
logger = logging.getLogger(__name__)

_DRAFT_TTL_SECONDS = 15 * 60
_COPYABLE_CONTENT_TYPES = {
    "animation", "audio", "contact", "dice", "document", "game",
    "location", "photo", "poll", "sticker", "text", "venue", "video",
    "video_note", "voice",
}
_MEDIA_KINDS = {"video", "video_note"}
_CHOICE_ACTIONS = {"normal": "video", "circle": "video_note"}


class AdminPrivateFilter(Filter):
    """Match only genuine private messages from the configured administrator."""

    async def __call__(self, message: Message, config: Config) -> bool:
        return bool(
            message.from_user
            and message.chat.type == "private"
            and message.from_user.id == config.admin_user_id
        )


class NonSlashCommandFilter(Filter):
    """Let administrator slash commands continue to their command routers."""

    async def __call__(self, message: Message) -> bool:
        text = message.text or message.caption or ""
        return not text.startswith("/")


def _choice_keyboard(token: str, _) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=_("broadcast_btn_normal"),
                callback_data=f"bc:{token}:normal",
            ),
            InlineKeyboardButton(
                text=_("broadcast_btn_circle"),
                callback_data=f"bc:{token}:circle",
            ),
        ],
        [
            InlineKeyboardButton(
                text=_("broadcast_btn_cancel"),
                callback_data=f"bc:{token}:cancel",
            )
        ],
    ])


def _confirmation_keyboard(token: str, _) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=_("broadcast_btn_confirm"),
                callback_data=f"bc:{token}:confirm",
            )
        ],
        [
            InlineKeyboardButton(
                text=_("broadcast_btn_cancel"),
                callback_data=f"bc:{token}:cancel",
            )
        ],
    ])


def _parse_callback(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "bc" or not parts[1]:
        return None
    return parts[1], parts[2]


def _is_admin_private_callback(callback: CallbackQuery, config: Config) -> bool:
    message = callback.message
    return bool(
        callback.from_user.id == config.admin_user_id
        and isinstance(message, Message)
        and message.chat.type == "private"
        and message.chat.id == config.admin_user_id
    )


async def _answer_callback(
    callback: CallbackQuery, text: str | None = None, *, alert: bool = False,
) -> None:
    """Acknowledge even stale controls; ignore a client-side expiry race."""
    try:
        await callback.answer(text, show_alert=alert)
    except Exception:
        logger.debug("Could not answer admin broadcast callback", exc_info=True)


async def _load_bound_draft(
    callback: CallbackQuery, token: str, db, config: Config,
) -> dict[str, Any] | None:
    """Load a live draft bound to this exact admin control message."""
    if not _is_admin_private_callback(callback, config):
        return None
    draft = await db.get_broadcast_draft(token)
    if not draft or not isinstance(draft.get("payload"), dict):
        return None
    if (
        int(draft.get("owner_user_id") or 0) != config.admin_user_id
        or int(draft.get("control_chat_id") or 0) != callback.message.chat.id
        or int(draft.get("control_message_id") or 0)
        != callback.message.message_id
        or float(draft.get("expires_at") or 0) <= time.time()
    ):
        return None
    return draft


async def _edit_control_text(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except Exception:
        logger.debug("Could not edit admin broadcast control", exc_info=True)
        return False


async def _delete_control(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logger.debug("Could not delete old admin broadcast control", exc_info=True)


async def _replace_confirmation_control(
    *,
    bot: Bot,
    db,
    config: Config,
    token: str,
    old_control: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> bool:
    """Send and atomically bind a fallback when editing a control fails."""
    try:
        replacement = await bot.send_message(
            chat_id=config.admin_user_id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception(
            "Could not send replacement admin broadcast confirmation"
        )
        return False
    try:
        rebound = await db.rebind_broadcast_draft_control(
            token,
            config.admin_user_id,
            old_control.message_id,
            replacement.message_id,
        )
    except Exception:
        logger.exception("Could not persist replacement confirmation control")
        await _delete_control(bot, config.admin_user_id, replacement.message_id)
        return False
    if not rebound:
        await _delete_control(bot, config.admin_user_id, replacement.message_id)
        return False
    await _delete_control(bot, old_control.chat.id, old_control.message_id)
    return True


def _confirmation_text(selected: str, _) -> str:
    key = (
        "broadcast_confirm_circle"
        if selected == "video_note"
        else "broadcast_confirm_normal"
    )
    return _(key)


async def _upload_cross_type_preview(
    *,
    bot: Bot,
    config: Config,
    payload: dict[str, Any],
    selected: str,
    reply_markup: InlineKeyboardMarkup,
) -> Message:
    """Download once, change Bot API media type, and upload one admin preview."""
    source_size = payload.get("source_file_size")
    if source_size and int(source_size) > config.max_input_mb * 1024 * 1024:
        raise ValueError("broadcast input exceeds Telegram download limit")

    os.makedirs(config.download_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".musiqa_broadcast_", dir=config.download_dir,
    ) as work_dir:
        source_path = os.path.join(work_dir, "source.mp4")
        await bot.download(
            payload["source_file_id"], destination=source_path, timeout=300
        )

        if selected == "video_note":
            prepared_path = await video_svc.make_video_note(
                source_path,
                work_dir,
                max_bytes=config.max_file_mb * 1024 * 1024,
            )
            duration = payload.get("source_duration")
            return await bot.send_video_note(
                chat_id=config.admin_user_id,
                video_note=FSInputFile(prepared_path),
                duration=min(int(duration), 60) if duration else None,
                length=480,
                reply_markup=reply_markup,
            )

        # A video note is already a square MPEG4. Uploading the bytes through
        # sendVideo (rather than reusing its type-bound file_id) gives Telegram
        # a normal-video file_id; the original uncropped frame cannot be restored.
        return await bot.send_video(
            chat_id=config.admin_user_id,
            video=FSInputFile(source_path),
            duration=(
                int(payload["source_duration"])
                if payload.get("source_duration")
                else None
            ),
            supports_streaming=True,
            reply_markup=reply_markup,
        )


def _prepared_file_id(message: Message, selected: str) -> str | None:
    media = message.video_note if selected == "video_note" else message.video
    return media.file_id if media else None


@router.message(AdminPrivateFilter(), F.video | F.video_note)
async def stage_admin_media(
    message: Message, db, config: Config, _, **kwargs,
) -> None:
    """Stage admin video media instead of broadcasting it without confirmation."""
    media = message.video or message.video_note
    source_kind = "video" if message.video else "video_note"
    token = secrets.token_urlsafe(9)
    control = await message.answer(
        _("broadcast_choose_format"), reply_markup=_choice_keyboard(token, _)
    )
    payload = {
        "delivery_kind": "media",
        "source_chat_id": message.chat.id,
        "source_message_id": message.message_id,
        "source_kind": source_kind,
        "source_file_id": media.file_id,
        "source_file_size": media.file_size,
        "source_duration": media.duration,
        "selected": None,
        "prepared_file_id": None,
    }
    try:
        await db.create_broadcast_draft(
            token,
            config.admin_user_id,
            message.chat.id,
            control.message_id,
            payload,
            time.time() + _DRAFT_TTL_SECONDS,
        )
    except Exception:
        logger.exception("Could not persist admin broadcast draft")
        await _edit_control_text(control, _("generic_error"))


async def _choose_format(
    callback: CallbackQuery,
    draft: dict[str, Any],
    token: str,
    selected: str,
    bot: Bot,
    db,
    config: Config,
    _,
) -> None:
    control = callback.message
    payload = dict(draft["payload"])
    payload["selected"] = selected
    needs_heavy_slot = payload.get("source_kind") != selected
    source_size = payload.get("source_file_size")
    input_is_too_large = bool(
        source_size
        and int(source_size) > config.max_input_mb * 1024 * 1024
    )
    heavy_slot_claimed = False
    if needs_heavy_slot and not input_is_too_large:
        claim_error = jobs.try_claim(config.admin_user_id)
        if claim_error:
            await _answer_callback(callback, _(claim_error), alert=True)
            return
        heavy_slot_claimed = True
    try:
        claimed = await db.transition_broadcast_draft(
            token,
            config.admin_user_id,
            control.message_id,
            "choosing",
            "preparing",
            payload=payload,
        )
    except Exception:
        if heavy_slot_claimed:
            jobs.release(config.admin_user_id)
        raise
    if not claimed:
        if heavy_slot_claimed:
            jobs.release(config.admin_user_id)
        await _answer_callback(
            callback, _("broadcast_already_started"), alert=True
        )
        return

    await _answer_callback(callback)
    await _edit_control_text(control, _("broadcast_preparing"))
    confirmation_markup = _confirmation_keyboard(token, _)

    # Same-type delivery remains a Telegram-side copy and needs no upload.
    if payload["source_kind"] == selected:
        ready = await db.transition_broadcast_draft(
            token,
            config.admin_user_id,
            control.message_id,
            "preparing",
            "awaiting_confirmation",
            payload=payload,
        )
        if not ready:
            await _edit_control_text(control, _("broadcast_draft_expired"))
            return
        confirmation_text = _confirmation_text(selected, _)
        edited = await _edit_control_text(
            control,
            confirmation_text,
            reply_markup=confirmation_markup,
        )
        if not edited:
            replaced = await _replace_confirmation_control(
                bot=bot,
                db=db,
                config=config,
                token=token,
                old_control=control,
                text=confirmation_text,
                reply_markup=confirmation_markup,
            )
            if not replaced:
                logger.error(
                    "Admin broadcast confirmation control is unavailable "
                    "token=%s",
                    token,
                )
        return

    if input_is_too_large:
        await db.transition_broadcast_draft(
            token,
            config.admin_user_id,
            control.message_id,
            "preparing",
            "failed",
            payload=payload,
        )
        await _edit_control_text(
            control,
            _(
                "too_big",
                size=round(int(source_size) / (1024 * 1024)),
                limit=config.max_input_mb,
            ),
        )
        return

    preview: Message | None = None
    try:
        preview = await _upload_cross_type_preview(
            bot=bot,
            config=config,
            payload=payload,
            selected=selected,
            reply_markup=confirmation_markup,
        )
        prepared_file_id = _prepared_file_id(preview, selected)
        if not prepared_file_id:
            raise RuntimeError("Telegram preview response omitted prepared media")
        payload["prepared_file_id"] = prepared_file_id
        ready = await db.transition_broadcast_draft(
            token,
            config.admin_user_id,
            control.message_id,
            "preparing",
            "awaiting_confirmation",
            payload=payload,
            new_control_message_id=preview.message_id,
        )
        if not ready:
            await _delete_control(bot, config.admin_user_id, preview.message_id)
            await _edit_control_text(control, _("broadcast_draft_expired"))
            return
        await _delete_control(bot, control.chat.id, control.message_id)
    except Exception as exc:
        logger.error(
            "Admin media preparation failed source_kind=%s selected=%s "
            "error=%s",
            payload.get("source_kind"), selected, type(exc).__name__,
        )
        if preview is not None:
            await _delete_control(bot, config.admin_user_id, preview.message_id)
        await db.transition_broadcast_draft(
            token,
            config.admin_user_id,
            control.message_id,
            "preparing",
            "failed",
            payload=payload,
        )
        await _edit_control_text(control, _("broadcast_prepare_failed"))
    finally:
        if heavy_slot_claimed:
            jobs.release(config.admin_user_id)


async def _cancel_draft(
    callback: CallbackQuery,
    draft: dict[str, Any],
    token: str,
    bot: Bot,
    db,
    config: Config,
    _,
) -> None:
    status = str(draft.get("status") or "")
    if status not in {"choosing", "awaiting_confirmation"}:
        await _answer_callback(
            callback, _("broadcast_already_started"), alert=True
        )
        return
    cancelled = await db.transition_broadcast_draft(
        token,
        config.admin_user_id,
        callback.message.message_id,
        status,
        "cancelled",
    )
    if not cancelled:
        await _answer_callback(
            callback, _("broadcast_draft_expired"), alert=True
        )
        return
    await _answer_callback(callback)
    await _delete_control(
        bot, callback.message.chat.id, callback.message.message_id
    )
    await bot.send_message(
        chat_id=config.admin_user_id, text=_("broadcast_cancelled")
    )


def _delivery_kind(payload: dict[str, Any]) -> str:
    # Drafts from the immediately preceding release did not write the field;
    # they were all confirmed media campaigns.
    return str(payload.get("delivery_kind") or "media")


def _draft_sender(bot: Bot, payload: dict[str, Any]):
    """Build the Telegram operation for one persisted administrator campaign."""
    delivery_kind = _delivery_kind(payload)
    source_chat_id = payload.get("source_chat_id")
    source_message_id = payload.get("source_message_id")
    if delivery_kind == "copy":
        if source_chat_id is None or source_message_id is None:
            raise ValueError("direct broadcast has no source message")

        async def send_one(user_id: int):
            return await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
            )

        return send_one
    if delivery_kind != "media":
        raise ValueError("broadcast has invalid delivery kind")

    from bot.services.top_music import top_music_keyboard

    markup = top_music_keyboard()
    selected = payload.get("selected")
    if selected not in _MEDIA_KINDS:
        raise ValueError("confirmed broadcast has invalid selected media")

    if payload.get("source_kind") == selected:
        if not source_chat_id or not source_message_id:
            raise ValueError("same-type broadcast has no source message")

        async def send_one(user_id: int):
            return await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=markup,
            )

        return send_one

    prepared_file_id = payload.get("prepared_file_id")
    if not prepared_file_id:
        raise ValueError("converted broadcast has no prepared file_id")
    if selected == "video_note":

        async def send_one(user_id: int):
            return await bot.send_video_note(
                chat_id=user_id,
                video_note=prepared_file_id,
                duration=(
                    min(int(payload["source_duration"]), 60)
                    if payload.get("source_duration")
                    else None
                ),
                length=480,
                reply_markup=markup,
            )

        return send_one

    async def send_one(user_id: int):
        return await bot.send_video(
            chat_id=user_id,
            video=prepared_file_id,
            duration=(
                int(payload["source_duration"])
                if payload.get("source_duration")
                else None
            ),
            supports_streaming=True,
            reply_markup=markup,
        )

    return send_one


async def _run_sending_draft(
    draft: dict[str, Any], bot: Bot, db, config: Config,
) -> broadcast_svc.BroadcastStats:
    """Deliver or resume one already-confirmed administrator campaign."""
    token = str(draft["token"])
    send_one = _draft_sender(bot, draft["payload"])

    async def checkpoint(user_id: int, outcome: broadcast_svc.Outcome) -> None:
        saved = await db.checkpoint_broadcast_draft(
            token, config.admin_user_id, user_id, outcome
        )
        if not saved:
            raise RuntimeError("manual broadcast checkpoint was rejected")

    await broadcast_svc.broadcast_active(
        bot=bot,
        db=db,
        config=config,
        send_one=send_one,
        after_user_id=int(draft.get("broadcast_cursor") or 0),
        through_user_id=int(draft.get("audience_upper_user_id") or 0),
        on_outcome=checkpoint,
        exclude_user_id=config.admin_user_id,
    )
    completed = await db.transition_broadcast_draft(
        token,
        config.admin_user_id,
        int(draft["control_message_id"]),
        "sending",
        "completed",
    )
    if not completed:
        raise RuntimeError("manual broadcast completion was rejected")
    final = await db.get_broadcast_draft(token)
    if final is None:
        raise RuntimeError("completed manual broadcast disappeared")
    return broadcast_svc.BroadcastStats(
        sent=int(final.get("broadcast_sent") or 0),
        inactive=int(final.get("broadcast_inactive") or 0),
        failed=int(final.get("broadcast_failed") or 0),
        last_user_id=int(final.get("broadcast_cursor") or 0),
    )


async def run_pending_admin_broadcasts(
    bot: Bot, db, config: Config, *, drafts: list[dict[str, Any]] | None = None,
) -> None:
    """Resume administrator campaigns after a Railway/process restart."""
    if drafts is None:
        drafts = await db.list_sending_broadcast_drafts(config.admin_user_id)
    if not drafts:
        return
    locale = await db.get_locale(config.admin_user_id) or config.default_locale

    def translate(key: str, **kwargs) -> str:
        return t(key, locale, **kwargs)

    logger.info("Resuming %s confirmed admin broadcast(s)", len(drafts))

    for draft in drafts:
        # Use the newest durable cursor. Main snapshots the token list before
        # polling starts, preventing a freshly confirmed campaign from racing
        # into both this startup worker and its callback handler.
        current = await db.get_broadcast_draft(str(draft["token"]))
        if current is None or current.get("status") != "sending":
            continue
        draft = current
        if _delivery_kind(draft["payload"]) == "media":
            try:
                await bot.edit_message_reply_markup(
                    chat_id=int(draft["control_chat_id"]),
                    message_id=int(draft["control_message_id"]),
                    reply_markup=None,
                )
            except Exception:
                logger.debug(
                    "Could not clear resumed broadcast controls", exc_info=True
                )
        try:
            stats = await _run_sending_draft(draft, bot, db, config)
        except asyncio.CancelledError:
            raise
        except ValueError:
            logger.exception(
                "Persisted admin broadcast is invalid token=%s", draft["token"]
            )
            await db.transition_broadcast_draft(
                str(draft["token"]),
                config.admin_user_id,
                int(draft["control_message_id"]),
                "sending",
                "failed",
            )
            try:
                await bot.send_message(
                    chat_id=config.admin_user_id,
                    text=translate(
                        "broadcast_prepare_failed"
                        if _delivery_kind(draft["payload"]) == "media"
                        else "generic_error"
                    ),
                )
            except Exception:
                logger.debug(
                    "Could not notify admin about invalid resumed campaign",
                    exc_info=True,
                )
            continue
        except Exception:
            # Keep the status and last durable cursor. A subsequent Railway
            # restart can safely continue instead of silently dropping the
            # remaining audience.
            logger.exception(
                "Resumed admin broadcast paused token=%s cursor=%s",
                draft["token"], draft.get("broadcast_cursor"),
            )
            break

        logger.info(
            "Resumed admin broadcast completed token=%s sent=%s "
            "inactive=%s failed=%s",
            draft["token"], stats.sent, stats.inactive, stats.failed,
        )
        try:
            await bot.send_message(
                chat_id=config.admin_user_id,
                text=translate(
                    "broadcast_done",
                    sent=stats.sent,
                    inactive=stats.inactive,
                    failed=stats.failed,
                ),
            )
        except Exception:
            logger.debug(
                "Could not send resumed broadcast summary", exc_info=True
            )


async def _confirm_draft(
    callback: CallbackQuery,
    draft: dict[str, Any],
    token: str,
    bot: Bot,
    db,
    config: Config,
    _,
) -> None:
    if draft.get("status") != "awaiting_confirmation":
        await _answer_callback(
            callback, _("broadcast_already_started"), alert=True
        )
        return
    audience_upper_user_id = await db.get_active_user_upper_bound(
        exclude_user_id=config.admin_user_id
    )
    claimed = await db.begin_broadcast_draft(
        token,
        config.admin_user_id,
        callback.message.message_id,
        audience_upper_user_id,
    )
    if not claimed:
        await _answer_callback(
            callback, _("broadcast_already_started"), alert=True
        )
        return

    await _answer_callback(callback)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not remove confirmation keyboard", exc_info=True)

    sending_draft = await db.get_broadcast_draft(token)
    if sending_draft is None:
        logger.error("Confirmed admin media draft disappeared token=%s", token)
        await callback.message.answer(_("generic_error"))
        return
    payload = sending_draft["payload"]
    selected = payload.get("selected")
    logger.info(
        "Confirmed admin media broadcast started source_message_id=%s kind=%s "
        "audience_upper_user_id=%s",
        payload.get("source_message_id"), selected, audience_upper_user_id,
    )
    try:
        stats = await _run_sending_draft(sending_draft, bot, db, config)
    except asyncio.CancelledError:
        # ``sending`` and the last completed recipient remain persisted for the
        # startup resume worker.
        raise
    except ValueError:
        logger.exception("Confirmed admin media broadcast payload is invalid")
        await db.transition_broadcast_draft(
            token,
            config.admin_user_id,
            callback.message.message_id,
            "sending",
            "failed",
        )
        await callback.message.answer(_("broadcast_prepare_failed"))
        return
    except Exception:
        # Do not discard a partially delivered campaign. Its frozen boundary,
        # cursor, and counters are resumed on the next process startup.
        logger.exception("Confirmed admin media broadcast paused")
        await callback.message.answer(_("generic_error"))
        return
    logger.info(
        "Confirmed admin media broadcast completed source_message_id=%s "
        "sent=%s inactive=%s failed=%s",
        payload.get("source_message_id"),
        stats.sent, stats.inactive, stats.failed,
    )
    await callback.message.answer(
        _(
            "broadcast_done",
            sent=stats.sent,
            inactive=stats.inactive,
            failed=stats.failed,
        )
    )


@router.callback_query(F.data.startswith("bc:"))
async def on_admin_broadcast_control(
    callback: CallbackQuery, bot: Bot, db, config: Config, _, **kwargs,
) -> None:
    parsed = _parse_callback(callback.data)
    if parsed is None or not _is_admin_private_callback(callback, config):
        await _answer_callback(callback, _("invalid_action"), alert=True)
        return
    token, action = parsed
    if action not in {*_CHOICE_ACTIONS, "confirm", "cancel"}:
        await _answer_callback(callback, _("invalid_action"), alert=True)
        return
    draft = await _load_bound_draft(callback, token, db, config)
    if draft is None:
        await _answer_callback(
            callback, _("broadcast_draft_expired"), alert=True
        )
        return

    if action in _CHOICE_ACTIONS:
        await _choose_format(
            callback, draft, token, _CHOICE_ACTIONS[action], bot, db, config, _
        )
    elif action == "cancel":
        await _cancel_draft(callback, draft, token, bot, db, config, _)
    else:
        await _confirm_draft(callback, draft, token, bot, db, config, _)


@router.message(AdminPrivateFilter(), NonSlashCommandFilter())
async def broadcast_admin_message(
    message: Message, bot: Bot, db, config: Config, _, **kwargs,
) -> None:
    """Persist, then immediately copy non-video admin content to every user."""
    if message.content_type not in _COPYABLE_CONTENT_TYPES:
        await message.answer(_("broadcast_unsupported"))
        return

    token = secrets.token_urlsafe(9)
    try:
        await db.create_direct_broadcast(
            token,
            config.admin_user_id,
            message.chat.id,
            message.message_id,
            message.content_type,
        )
        draft = await db.get_broadcast_draft(token)
        if draft is None:
            raise RuntimeError("persisted direct broadcast disappeared")
        upper_user_id = int(draft["audience_upper_user_id"])
        logger.info(
            "Direct admin broadcast started source_message_id=%s "
            "audience_upper_user_id=%s",
            message.message_id, upper_user_id,
        )
        stats = await _run_sending_draft(draft, bot, db, config)
    except asyncio.CancelledError:
        raise
    except ValueError:
        logger.exception("Direct admin broadcast payload is invalid")
        await db.transition_broadcast_draft(
            token,
            config.admin_user_id,
            message.message_id,
            "sending",
            "failed",
        )
        await message.answer(_("generic_error"))
        return
    except Exception:
        # If persistence already succeeded, startup resumes from its last
        # checkpoint. This preserves the remainder across Railway restarts.
        logger.exception("Direct admin broadcast paused")
        await message.answer(_("generic_error"))
        return
    logger.info(
        "Direct admin broadcast completed source_message_id=%s sent=%s "
        "inactive=%s failed=%s",
        message.message_id, stats.sent, stats.inactive, stats.failed,
    )
    await message.answer(
        _(
            "broadcast_done",
            sent=stats.sent,
            inactive=stats.inactive,
            failed=stats.failed,
        )
    )
