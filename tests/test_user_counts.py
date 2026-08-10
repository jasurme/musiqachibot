"""Focused storage tests for the administrator user-count command."""

from bot.db.storage import Storage


async def test_user_counts_partition_users_and_optionally_exclude_admin():
    db = Storage(":memory:")
    await db.init()
    assert await db.get_user_counts() == {
        "total": 0, "active": 0, "inactive": 0,
    }

    for user_id in (11, 22, 33):
        await db.touch_private_user(user_id)
    await db.mark_users_inactive([22])

    assert await db.get_user_counts() == {
        "total": 3, "active": 2, "inactive": 1,
    }
    assert await db.get_user_counts(exclude_user_id=11) == {
        "total": 2, "active": 1, "inactive": 1,
    }
    assert await db.get_user_counts(exclude_user_id=22) == {
        "total": 2, "active": 2, "inactive": 0,
    }
    await db.close()
