"""Authorization and dispatcher coverage for the admin audience counter."""

from __future__ import annotations

import pytest

from tests.conftest import text_update


ADMIN_ID = 7645204689


async def test_total_users_reports_audience_counts_to_private_admin_only(
    dp, bot, cap, storage,
):
    await storage.touch_private_user(11)
    await storage.touch_private_user(22)
    await storage.mark_users_inactive([22])

    await dp.feed_update(
        bot,
        text_update(
            "/total_users",
            uid=100,
            lang="en",
            user_id=ADMIN_ID,
            chat_id=ADMIN_ID,
        ),
    )

    messages = cap.by("SendMessage")
    assert len(messages) == 1
    assert messages[0].chat_id == ADMIN_ID
    assert messages[0].text == (
        "👥 <b>Users</b>\n\n"
        "Total: 2\nActive: 1\nInactive: 1"
    )
    assert cap.by("CopyMessage") == []


@pytest.mark.parametrize(
    ("user_id", "chat_id", "chat_type"),
    [
        (12345, 12345, "private"),
        (ADMIN_ID, -100123, "supergroup"),
        (ADMIN_ID, 98765, "private"),
    ],
)
async def test_total_users_is_silent_outside_exact_admin_private_chat(
    dp, bot, cap, user_id, chat_id, chat_type,
):
    await dp.feed_update(
        bot,
        text_update(
            "/total_users",
            uid=200,
            user_id=user_id,
            chat_id=chat_id,
            chat_type=chat_type,
        ),
    )

    assert cap.methods == []
