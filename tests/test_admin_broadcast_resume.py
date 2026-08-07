"""Restart-durability tests for confirmed administrator media campaigns."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.types import Chat, Message, Update, User

from bot import jobs
from bot.db.storage import Storage
from bot.handlers import broadcast
from bot.i18n import t


async def _confirmed_draft(db: Storage, *, token: str = "campaign") -> dict:
    payload = {
        "source_chat_id": 7645204689,
        "source_message_id": 55,
        "source_kind": "video",
        "source_file_id": "telegram-file-id",
        "source_file_size": 100,
        "source_duration": 10,
        "selected": "video",
        "prepared_file_id": None,
    }
    await db.create_broadcast_draft(
        token, 7645204689, 7645204689, 99, payload, 9_999_999_999,
    )
    assert await db.transition_broadcast_draft(
        token, 7645204689, 99, "choosing", "preparing", payload=payload,
    )
    assert await db.transition_broadcast_draft(
        token, 7645204689, 99, "preparing", "awaiting_confirmation",
        payload=payload,
    )
    assert await db.begin_broadcast_draft(token, 7645204689, 99, 33)
    draft = await db.get_broadcast_draft(token)
    assert draft is not None
    return draft


async def test_confirmed_media_campaign_resumes_from_durable_cursor(
    tmp_path, monkeypatch,
):
    path = tmp_path / "manual-resume.db"
    db = Storage(str(path))
    await db.init()
    for user_id in (11, 22, 33, 7645204689):
        await db.touch_private_user(user_id)
    draft = await _confirmed_draft(db)

    copied: list[int] = []
    summaries: list[str] = []

    class FakeBot:
        async def copy_message(self, **kwargs):
            copied.append(kwargs["chat_id"])
            assert kwargs["from_chat_id"] == 7645204689
            assert kwargs["message_id"] == 55
            button = kwargs["reply_markup"].inline_keyboard[0][0]
            assert button.callback_data == "top_music"
            return object()

        async def edit_message_reply_markup(self, **kwargs):
            return True

        async def send_message(self, **kwargs):
            summaries.append(kwargs["text"])
            return object()

    async def interrupted(**kwargs):
        assert kwargs["after_user_id"] == 0
        assert kwargs["through_user_id"] == 33
        await kwargs["send_one"](11)
        await kwargs["on_outcome"](11, "sent")
        raise RuntimeError("simulated Railway restart")

    monkeypatch.setattr(broadcast.broadcast_svc, "broadcast_active", interrupted)
    config = SimpleNamespace(
        admin_user_id=7645204689,
        default_locale="en",
        broadcast_rate_per_second=20,
    )
    with pytest.raises(RuntimeError, match="Railway restart"):
        await broadcast._run_sending_draft(draft, FakeBot(), db, config)
    state = await db.get_broadcast_draft("campaign")
    assert state["status"] == "sending"
    assert state["audience_upper_user_id"] == 33
    assert state["broadcast_cursor"] == 11
    assert state["broadcast_sent"] == 1
    await db.close()

    reopened = Storage(str(path))
    await reopened.init()

    async def resumed(**kwargs):
        assert kwargs["after_user_id"] == 11
        assert kwargs["through_user_id"] == 33
        for user_id, outcome in ((22, "inactive"), (33, "sent")):
            await kwargs["send_one"](user_id)
            await kwargs["on_outcome"](user_id, outcome)
        return SimpleNamespace(sent=1, inactive=1, failed=0, last_user_id=33)

    monkeypatch.setattr(broadcast.broadcast_svc, "broadcast_active", resumed)
    await broadcast.run_pending_admin_broadcasts(FakeBot(), reopened, config)

    state = await reopened.get_broadcast_draft("campaign")
    assert state["status"] == "completed"
    assert state["broadcast_cursor"] == 33
    assert state["broadcast_sent"] == 2
    assert state["broadcast_inactive"] == 1
    assert state["broadcast_failed"] == 0
    assert copied == [11, 22, 33]
    assert len(summaries) == 1
    assert "Sent: 2" in summaries[0]
    assert "Inactive: 1" in summaries[0]
    await reopened.close()


async def test_manual_broadcast_checkpoint_is_monotonic_and_idempotent():
    db = Storage(":memory:")
    await db.init()
    await _confirmed_draft(db)

    assert await db.checkpoint_broadcast_draft(
        "campaign", 7645204689, 11, "sent"
    )
    assert not await db.checkpoint_broadcast_draft(
        "campaign", 7645204689, 11, "sent"
    )
    assert not await db.checkpoint_broadcast_draft(
        "campaign", 7645204689, 44, "sent"
    )
    state = await db.get_broadcast_draft("campaign")
    assert state["broadcast_cursor"] == 11
    assert state["broadcast_sent"] == 1
    await db.close()


async def test_new_draft_cleanup_never_removes_expired_sending_campaign():
    db = Storage(":memory:")
    await db.init()
    await _confirmed_draft(db, token="in-flight")
    await db._db.execute(
        "UPDATE broadcast_drafts SET expires_at = 0 WHERE token = 'in-flight'"
    )
    await db._db.commit()

    # Creating another control triggers both expiry and bounded-history cleanup.
    await db.create_broadcast_draft(
        "new-control", 7645204689, 7645204689, 100,
        {"source_kind": "video"}, 9_999_999_999,
    )
    in_flight = await db.get_broadcast_draft("in-flight")
    assert in_flight is not None
    assert in_flight["status"] == "sending"
    await db.close()


async def test_cross_format_preparation_respects_heavy_job_guard():
    db = Storage(":memory:")
    await db.init()
    payload = {
        "source_kind": "video",
        "source_file_id": "source",
        "source_file_size": 100,
        "source_duration": 10,
    }
    await db.create_broadcast_draft(
        "busy", 7645204689, 7645204689, 99, payload, 9_999_999_999,
    )
    draft = await db.get_broadcast_draft("busy")
    assert draft is not None
    answers: list[tuple[str | None, bool]] = []

    class FakeCallback:
        message = SimpleNamespace(message_id=99)

        async def answer(self, text=None, show_alert=False):
            answers.append((text, show_alert))

    jobs.clear()
    assert jobs.try_claim(7645204689) is None
    try:
        await broadcast._choose_format(
            FakeCallback(), draft, "busy", "video_note", object(), db,
            SimpleNamespace(
                admin_user_id=7645204689, max_input_mb=20,
                max_file_mb=50, download_dir="unused",
            ),
            lambda key, **kwargs: t(key, "en", **kwargs),
        )
    finally:
        jobs.release(7645204689)
    assert answers and answers[0][1] is True
    assert "processing" in answers[0][0].lower()
    assert (await db.get_broadcast_draft("busy"))["status"] == "choosing"
    await db.close()


async def test_direct_copy_campaign_resumes_without_top_music_markup(
    tmp_path, monkeypatch,
):
    path = tmp_path / "direct-resume.db"
    db = Storage(str(path))
    await db.init()
    for user_id in (11, 22, 7645204689):
        await db.touch_private_user(user_id)
    await db.create_direct_broadcast(
        "direct", 7645204689, 7645204689, 77, "document",
    )
    draft = await db.get_broadcast_draft("direct")
    assert draft is not None
    copied: list[int] = []
    summaries: list[str] = []

    class FakeBot:
        async def copy_message(self, **kwargs):
            copied.append(kwargs["chat_id"])
            assert kwargs["from_chat_id"] == 7645204689
            assert kwargs["message_id"] == 77
            assert "reply_markup" not in kwargs
            return object()

        async def edit_message_reply_markup(self, **kwargs):
            raise AssertionError("direct campaigns have no control markup")

        async def send_message(self, **kwargs):
            summaries.append(kwargs["text"])
            return object()

    async def interrupted(**kwargs):
        assert kwargs["after_user_id"] == 0
        assert kwargs["through_user_id"] == 22
        await kwargs["send_one"](11)
        await kwargs["on_outcome"](11, "sent")
        raise RuntimeError("simulated Railway restart")

    config = SimpleNamespace(
        admin_user_id=7645204689,
        default_locale="en",
        broadcast_rate_per_second=20,
    )
    monkeypatch.setattr(broadcast.broadcast_svc, "broadcast_active", interrupted)
    with pytest.raises(RuntimeError, match="Railway restart"):
        await broadcast._run_sending_draft(draft, FakeBot(), db, config)
    assert (await db.get_broadcast_draft("direct"))["broadcast_cursor"] == 11
    await db.close()

    reopened = Storage(str(path))
    await reopened.init()

    async def resumed(**kwargs):
        assert kwargs["after_user_id"] == 11
        assert kwargs["through_user_id"] == 22
        await kwargs["send_one"](22)
        await kwargs["on_outcome"](22, "sent")
        return SimpleNamespace(sent=1, inactive=0, failed=0, last_user_id=22)

    monkeypatch.setattr(broadcast.broadcast_svc, "broadcast_active", resumed)
    await broadcast.run_pending_admin_broadcasts(FakeBot(), reopened, config)
    state = await reopened.get_broadcast_draft("direct")
    assert state["status"] == "completed"
    assert state["broadcast_sent"] == 2
    assert copied == [11, 22]
    assert len(summaries) == 1 and "Sent: 2" in summaries[0]
    await reopened.close()


async def test_same_type_edit_failure_rebinds_fallback_confirmation():
    db = Storage(":memory:")
    await db.init()
    payload = {
        "delivery_kind": "media",
        "source_chat_id": 7645204689,
        "source_message_id": 55,
        "source_kind": "video",
        "source_file_id": "source",
        "source_file_size": 100,
        "source_duration": 10,
        "selected": None,
        "prepared_file_id": None,
    }
    await db.create_broadcast_draft(
        "fallback", 7645204689, 7645204689, 99, payload, 9_999_999_999,
    )
    draft = await db.get_broadcast_draft("fallback")
    assert draft is not None
    replacements = []
    deleted = []

    class BrokenControl:
        message_id = 99
        chat = SimpleNamespace(id=7645204689)

        async def edit_text(self, *args, **kwargs):
            raise RuntimeError("message cannot be edited")

    class FakeCallback:
        message = BrokenControl()

        async def answer(self, *args, **kwargs):
            return None

    class FakeBot:
        async def send_message(self, **kwargs):
            replacements.append(kwargs)
            return SimpleNamespace(message_id=100)

        async def delete_message(self, **kwargs):
            deleted.append(kwargs)
            return True

    await broadcast._choose_format(
        FakeCallback(), draft, "fallback", "video", FakeBot(), db,
        SimpleNamespace(
            admin_user_id=7645204689, max_input_mb=20,
            max_file_mb=50, download_dir="unused",
        ),
        lambda key, **kwargs: t(key, "en", **kwargs),
    )
    state = await db.get_broadcast_draft("fallback")
    assert state["status"] == "awaiting_confirmation"
    assert state["control_message_id"] == 100
    assert len(replacements) == 1
    confirm = replacements[0]["reply_markup"].inline_keyboard[0][0]
    assert confirm.callback_data == "bc:fallback:confirm"
    assert deleted == [{"chat_id": 7645204689, "message_id": 99}]
    await db.close()


async def test_admin_slash_command_reaches_normal_command_router(
    dp, bot, cap, storage,
):
    await storage.touch_private_user(11)
    update = Update(
        update_id=500,
        message=Message(
            message_id=500,
            date=datetime.now(timezone.utc),
            chat=Chat(id=7645204689, type="private"),
            from_user=User(
                id=7645204689, is_bot=False, first_name="Admin",
                language_code="en",
            ),
            text="/start",
        ),
    )
    await dp.feed_update(bot, update)
    assert cap.by("CopyMessage") == []
    assert cap.by("SendMessage")
    assert "music" in cap.last("SendMessage").text.lower()
