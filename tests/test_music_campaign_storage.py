"""Storage invariants for generalized editorial collections and fan-out."""

import asyncio
import sqlite3

import pytest

from bot.db.storage import (
    NOTIFY_ALL_MUSIC,
    NOTIFY_DISCOVERIES,
    NOTIFY_NEW_MUSIC,
    NOTIFY_RISING_MUSIC,
    Storage,
)


def _items(prefix: str, count: int) -> list[dict]:
    return [
        {
            "source_id": f"{prefix}-source-{index}",
            "artist": f"Artist {index}",
            "name": f"Song {index}",
            "video_id": f"video-{prefix}-{index}",
            "title": f"Artist {index} — Song {index}",
            "duration": 180 + index,
            "uploader": f"Channel {index}",
            "release_date": "2026-08-01",
            "genres": ["Pop"],
            "rank": index + 1,
            "storefront": "uz",
        }
        for index in range(count)
    ]


def _campaign(prefix: str, mask: int, eligible_at: int = 100) -> dict:
    return {
        "dedupe_key": f"{prefix}:{eligible_at}",
        "kind": prefix,
        "header_key": f"{prefix}_header",
        "notification_mask": mask,
        "eligible_at": eligible_at,
        "expires_at": eligible_at + 3600,
        "priority": 10,
    }


async def test_user_notification_mask_migrates_and_filters(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, locale TEXT)"
        )
        connection.executemany(
            "INSERT INTO users (user_id, locale) VALUES (?, 'uz')",
            [(11,), (22,), (33,)],
        )

    db = Storage(str(path))
    await db.init()
    assert await db.get_notification_mask(11) == NOTIFY_ALL_MUSIC
    await db.set_notification_mask(11, NOTIFY_NEW_MUSIC)
    await db.set_notification_mask(22, NOTIFY_RISING_MUSIC)
    await db.set_notification_mask(33, 0)

    assert await db.get_active_user_ids(
        notification_mask=NOTIFY_NEW_MUSIC
    ) == [11]
    assert await db.get_active_user_ids(
        notification_mask=NOTIFY_RISING_MUSIC
    ) == [22]
    assert await db.get_active_user_upper_bound(
        notification_mask=NOTIFY_DISCOVERIES
    ) == 0
    assert await db.toggle_notification_mask(11, NOTIFY_DISCOVERIES) == (
        NOTIFY_NEW_MUSIC | NOTIFY_DISCOVERIES
    )
    assert await db.toggle_notification_mask(11, NOTIFY_NEW_MUSIC) == (
        NOTIFY_DISCOVERIES
    )
    await asyncio.gather(
        db.toggle_notification_mask(44, NOTIFY_RISING_MUSIC),
        db.toggle_notification_mask(44, NOTIFY_RISING_MUSIC),
    )
    assert await db.get_notification_mask(44) == NOTIFY_ALL_MUSIC
    with pytest.raises(ValueError, match="unsupported"):
        await db.set_notification_mask(11, 8)
    await db.close()


async def test_collection_publish_is_atomic_and_campaign_is_idempotent():
    db = Storage(":memory:")
    await db.init()
    await db.ensure_music_collection("new_music", 5)
    claim = await db.claim_music_collection_refresh("new_music", 100, force=True)
    assert claim
    result = await db.publish_music_collection(
        "new_music",
        _items("new", 5),
        now=100,
        claim_token=claim,
        refresh_seconds=604800,
        resolutions={"one": {"video_id": "video-new-0"}},
        provider_state={"recent_source_ids": ["new-source-0"]},
        campaign=_campaign("new_music", NOTIFY_NEW_MUSIC),
    )
    assert result == {
        "changed": True,
        "generation": 1,
        "campaign_id": 1,
        "campaign_created": True,
    }
    state = await db.get_music_collection_state("new_music")
    assert len(state["items"]) == 5
    assert state["generation"] == 1
    assert state["provider_state"]["recent_source_ids"] == ["new-source-0"]

    claim = await db.claim_music_collection_refresh("new_music", 604900, force=True)
    assert claim
    unchanged = await db.publish_music_collection(
        "new_music",
        _items("new", 5),
        now=604900,
        claim_token=claim,
        refresh_seconds=604800,
        campaign=_campaign("new_music", NOTIFY_NEW_MUSIC),
    )
    assert unchanged["changed"] is False
    assert unchanged["generation"] == 1
    assert unchanged["campaign_id"] is None

    claim = await db.claim_music_collection_refresh("new_music", 605000, force=True)
    assert claim
    with pytest.raises(ValueError, match="exactly 5"):
        await db.publish_music_collection(
            "new_music",
            _items("broken", 4),
            now=605000,
            claim_token=claim,
            refresh_seconds=604800,
        )
    # Invalid publication rolls back both the snapshot and its lease release.
    state = await db.get_music_collection_state("new_music")
    assert state["generation"] == 1
    assert state["refresh_lease_token"] == claim
    assert await db.defer_music_collection_refresh(
        "new_music", claim, next_refresh_at=700000
    )
    state = await db.get_music_collection_state("new_music")
    assert state["next_refresh_at"] == 700000
    assert state["failure_count"] == 0
    assert state["refresh_lease_token"] is None
    await db.close()


async def test_collection_batch_is_all_or_nothing():
    db = Storage(":memory:")
    await db.init()
    for key in ("mood:night", "mood:road"):
        await db.ensure_music_collection(key, 10)
    first = await db.claim_music_collection_refresh(
        "mood:night", 100, force=True
    )
    second = await db.claim_music_collection_refresh(
        "mood:road", 100, force=True
    )
    assert first and second
    with pytest.raises(ValueError, match="exactly 10"):
        await db.publish_music_collection_batch([
            {
                "key": "mood:night", "items": _items("night", 10),
                "now": 100, "claim_token": first, "refresh_seconds": 604800,
            },
            {
                "key": "mood:road", "items": _items("road", 9),
                "now": 100, "claim_token": second, "refresh_seconds": 604800,
            },
        ])
    assert (await db.get_music_collection_state("mood:night"))["items"] == []
    assert (await db.get_music_collection_state("mood:road"))["items"] == []
    await db.close()


async def test_music_transaction_cannot_be_committed_by_unrelated_write(
    tmp_path, monkeypatch,
):
    """A concurrent primary-connection commit cannot split music publication."""
    db = Storage(str(tmp_path / "transaction-isolation.db"))
    await db.init()
    await db.ensure_music_collection("new_music", 5)
    first_claim = await db.claim_music_collection_refresh(
        "new_music", 100, force=True
    )
    await db.publish_music_collection(
        "new_music",
        _items("old", 5),
        now=100,
        claim_token=first_claim,
        refresh_seconds=604800,
    )
    second_claim = await db.claim_music_collection_refresh(
        "new_music", 200, force=True
    )

    transaction_updated = asyncio.Event()
    allow_failure = asyncio.Event()

    async def pause_then_fail(**kwargs):
        transaction_updated.set()
        await allow_failure.wait()
        raise RuntimeError("forced failure after snapshot update")

    monkeypatch.setattr(db, "_enqueue_music_campaign_locked", pause_then_fail)
    publication = asyncio.create_task(db.publish_music_collection(
        "new_music",
        _items("new", 5),
        now=200,
        claim_token=second_claim,
        refresh_seconds=604800,
        campaign=_campaign("new_music", NOTIFY_NEW_MUSIC, eligible_at=200),
    ))
    await transaction_updated.wait()

    unrelated = asyncio.create_task(db.set_locale(777, "uz"))
    await asyncio.sleep(0.05)
    assert not unrelated.done(), "unrelated COMMIT crossed the music transaction"

    allow_failure.set()
    with pytest.raises(RuntimeError, match="forced failure"):
        await publication
    await unrelated

    state = await db.get_music_collection_state("new_music")
    assert state["generation"] == 1
    assert state["items"][0]["source_id"] == "old-source-0"
    assert state["refresh_lease_token"] == second_claim
    assert await db.get_locale(777) == "uz"
    async with db._music_db.execute(
        "SELECT COUNT(*) FROM music_campaign_outbox"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0
    await db.close()


async def test_chart_history_preserves_source_metadata_and_selects_baseline():
    db = Storage(":memory:")
    await db.init()
    first = _items("history-a", 5)
    second = _items("history-b", 5)
    assert await db.record_music_chart_snapshot(
        "apple:uz:top100", first, captured_at=100
    )
    assert await db.record_music_chart_snapshot(
        "apple:uz:top100", second, captured_at=200
    )
    latest = await db.get_latest_music_chart_snapshot("apple:uz:top100")
    baseline = await db.get_music_chart_snapshot_before(
        "apple:uz:top100", 200
    )
    assert latest["captured_at"] == 200
    assert baseline["captured_at"] == 100
    assert baseline["items"][0]["release_date"] == "2026-08-01"
    assert baseline["items"][0]["genres"] == ["Pop"]
    await db.close()


async def test_outbox_freezes_payload_filters_audience_and_enforces_week_quota():
    db = Storage(":memory:")
    await db.init()
    for user_id, mask in (
        (11, NOTIFY_NEW_MUSIC),
        (22, NOTIFY_RISING_MUSIC),
        (33, NOTIFY_ALL_MUSIC),
    ):
        await db.touch_private_user(user_id)
        await db.set_notification_mask(user_id, mask)

    for index, (key, bit) in enumerate((
        ("new_music", NOTIFY_NEW_MUSIC),
        ("rising", NOTIFY_RISING_MUSIC),
        ("discoveries", NOTIFY_DISCOVERIES),
    )):
        await db.ensure_music_collection(key, 5)
        claim = await db.claim_music_collection_refresh(key, 100, force=True)
        assert claim
        await db.publish_music_collection(
            key,
            _items(key, 5),
            now=100,
            claim_token=claim,
            refresh_seconds=604800,
            campaign=_campaign(key, bit, eligible_at=100 + index),
        )

    first = await db.claim_next_music_campaign(
        now=100, week_key="2026-08-03", max_per_week=2
    )
    assert first["kind"] == "new_music"
    assert first["audience_upper_user_id"] == 33
    frozen_title = first["payload"]["items"][0]["title"]
    assert frozen_title == "Artist 0 — Song 0"
    assert await db.checkpoint_music_campaign(
        first["campaign_id"], first["runner_token"], 11, "sent"
    )
    # A repeated recipient cannot inflate counters.
    assert not await db.checkpoint_music_campaign(
        first["campaign_id"], first["runner_token"], 11, "sent"
    )
    assert await db.checkpoint_music_campaign(
        first["campaign_id"], first["runner_token"], 33, "sent"
    )
    assert await db.complete_music_campaign(
        first["campaign_id"], first["runner_token"]
    )

    second = await db.claim_next_music_campaign(
        now=101, week_key="2026-08-03", max_per_week=2
    )
    assert second["kind"] == "rising"
    await db.release_music_campaign(second["campaign_id"], second["runner_token"])
    resumed = await db.claim_next_music_campaign(
        now=101, week_key="2026-08-03", max_per_week=2
    )
    assert resumed["campaign_id"] == second["campaign_id"]
    assert resumed["slot_no"] == 2
    assert await db.complete_music_campaign(
        resumed["campaign_id"], resumed["runner_token"]
    )
    assert await db.claim_next_music_campaign(
        now=102, week_key="2026-08-03", max_per_week=2
    ) is None
    await db.close()
