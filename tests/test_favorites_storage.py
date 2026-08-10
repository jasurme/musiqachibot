"""Persistence invariants for restart-safe per-user favorite playlists."""

import asyncio
import sqlite3

import pytest

from bot.db.storage import Storage


def _youtube_item(index: int, **overrides) -> dict:
    item = {
        "video_id": f"FavVid{index:05d}",
        "title": f"Artist {index} - Song {index}",
        "duration": 180 + index,
        "uploader": f"Artist {index}",
    }
    item.update(overrides)
    return item


async def test_favorite_catalog_and_membership_are_idempotent_and_persistent(
    tmp_path,
):
    path = tmp_path / "favorites.db"
    db = Storage(str(path))
    await db.init()

    key = await db.upsert_favorite_track(
        _youtube_item(1), file_id="TELEGRAM_AUDIO_1"
    )
    assert key == "yt:FavVid00001"
    assert len(f"fav:a:{key}".encode()) <= 64
    assert await db.add_favorite(101, key) is True
    assert await db.add_favorite(101, key) is False
    assert await db.add_favorite(202, key) is True
    assert await db.count_favorites(101) == 1
    assert await db.is_favorite(101, "FavVid00001") is True

    favorite = await db.get_favorite(101, key)
    assert favorite == {
        "track_key": key,
        "video_id": "FavVid00001",
        "source_url": "https://www.youtube.com/watch?v=FavVid00001",
        "title": "Artist 1 - Song 1",
        "duration": 181,
        "uploader": "Artist 1",
        "file_id": "TELEGRAM_AUDIO_1",
        "updated_at": favorite["updated_at"],
        "saved_at": favorite["saved_at"],
    }
    saved_at = favorite["saved_at"]

    # A repeated catalog observation refreshes richer metadata without moving
    # either user's saved-playlist position or erasing the reusable file_id.
    assert await db.upsert_favorite_track(
        _youtube_item(
            1, title="  Better   Title  ", duration=None, uploader=""
        )
    ) == key
    refreshed = await db.get_favorite(101, key)
    assert refreshed["title"] == "Better Title"
    assert refreshed["duration"] == 181
    assert refreshed["uploader"] == "Artist 1"
    assert refreshed["file_id"] == "TELEGRAM_AUDIO_1"
    assert refreshed["saved_at"] == saved_at
    await db.close()

    reopened = Storage(str(path))
    await reopened.init()
    assert (await reopened.get_favorite(101, key))["title"] == "Better Title"
    assert await reopened.count_favorites(202) == 1
    assert await reopened.remove_favorite(101, key) is True
    assert await reopened.remove_favorite(101, key) is False
    assert await reopened.is_favorite(101, key) is False
    assert await reopened.is_favorite(202, key) is True
    await reopened.close()


async def test_direct_social_audio_gets_bounded_stable_callback_key():
    db = Storage(":memory:")
    await db.init()
    source_url = "https://www.instagram.com/reel/example/?utm_source=copy_link"
    alternate_url = "https://www.instagram.com/reel/example/?igsh=new-share"
    key = await db.upsert_favorite_track({
        "source_key": "instagram:reel:123456",
        "source_url": source_url,
        "title": "Creator - Reel audio",
        "duration": 25,
        "uploader": "Creator",
        "file_id": "INSTAGRAM_AUDIO_FILE",
    })

    assert key.startswith("m:")
    assert len(key) == 42
    assert len(f"fav:add:{key}".encode()) <= 64
    assert key == await db.upsert_favorite_track({
        "source_key": "instagram:reel:123456",
        "source_url": alternate_url,
        "title": "Creator - Reel audio",
        "duration": 25,
        "uploader": "Creator",
    })
    assert await db.add_favorite(77, key) is True
    saved = await db.get_favorite(77, key)
    assert saved["video_id"] is None
    assert saved["source_url"] == alternate_url
    assert saved["file_id"] == "INSTAGRAM_AUDIO_FILE"
    await db.close()


async def test_favorite_pages_are_bounded_newest_first_and_user_scoped():
    db = Storage(":memory:")
    await db.init()
    keys = []
    for index in range(12):
        key = await db.upsert_favorite_track(_youtube_item(index))
        keys.append(key)
        assert await db.add_favorite(1, key) is True
    other = await db.upsert_favorite_track(_youtube_item(99))
    await db.add_favorite(2, other)

    assert [item["track_key"] for item in await db.list_favorites(
        1, limit=5
    )] == list(reversed(keys[-5:]))
    assert [item["track_key"] for item in await db.list_favorites(
        1, limit=5, offset=5
    )] == list(reversed(keys[2:7]))
    assert len(await db.list_favorites(1, limit=1000)) == 12
    assert await db.count_favorites(1) == 12
    assert await db.count_favorites(2) == 1
    assert await db.get_favorite(1, other) is None
    await db.close()


async def test_concurrent_favorite_adds_create_one_membership():
    db = Storage(":memory:")
    await db.init()
    key = await db.upsert_favorite_track(_youtube_item(7))

    outcomes = await asyncio.gather(*(
        db.add_favorite(5, key) for _ in range(20)
    ))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 19
    assert await db.count_favorites(5) == 1
    await db.close()


async def test_catalog_cleanup_never_prunes_a_referenced_track():
    db = Storage(":memory:")
    await db.init()
    kept = await db.upsert_favorite_track(_youtube_item(1))
    stale = await db.upsert_favorite_track(_youtube_item(2))
    await db.add_favorite(9, kept)
    await db._db.execute(
        "UPDATE favorite_tracks SET updated_at = 0 WHERE track_key IN (?, ?)",
        (kept, stale),
    )
    await db._db.commit()
    db._favorite_prune_after = 0

    await db.upsert_favorite_track(_youtube_item(3))
    assert await db.get_favorite_track(kept) is not None
    assert await db.get_favorite_track(stale) is None
    await db.close()


async def test_file_id_refresh_and_toggle_are_catalog_scoped():
    db = Storage(":memory:")
    await db.init()
    key = await db.upsert_favorite_track(_youtube_item(4))
    assert await db.set_favorite_file_id(key, "AUDIO_NEW") is True
    assert (await db.get_favorite_track(key))["file_id"] == "AUDIO_NEW"
    assert await db.set_favorite_file_id(key, None) is True
    assert (await db.get_favorite_track(key))["file_id"] is None

    assert await db.toggle_favorite(6, key) is True
    assert await db.toggle_favorite(6, key) is False
    assert await db.count_favorites(6) == 0
    edge_key = await db.upsert_favorite_track({
        "video_id": "_Leading001",
        "title": "Leading symbol is valid on YouTube",
    })
    assert await db.add_favorite(6, edge_key) is True
    assert await db.is_favorite(6, "_Leading001") is True
    with pytest.raises(KeyError):
        await db.add_favorite(6, "yt:Missing01")
    await db.close()


async def test_user_data_deletion_removes_memberships_but_not_shared_catalog():
    db = Storage(":memory:")
    await db.init()
    key = await db.upsert_favorite_track(_youtube_item(8))
    await db.add_favorite(1, key)
    await db.add_favorite(2, key)

    removed = await db.delete_user_data(1)

    assert removed == 2  # users row + favorite membership
    assert await db.count_favorites(1) == 0
    assert await db.count_favorites(2) == 1
    assert await db.get_favorite_track(key) is not None
    await db.close()


async def test_favorite_schema_is_safe_on_a_legacy_database(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE users (user_id INTEGER PRIMARY KEY, locale TEXT)"
    )
    connection.execute("INSERT INTO users VALUES (42, 'uz')")
    connection.commit()
    connection.close()

    db = Storage(str(path))
    await db.init()
    key = await db.upsert_favorite_track(_youtube_item(6))
    assert await db.add_favorite(42, key) is True
    assert (await db.list_favorites(42))[0]["track_key"] == key
    assert await db.get_locale(42) == "uz"
    await db.close()


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"video_id": "bad id", "title": "Song"},
        {"video_id": "Valid001", "title": ""},
        {"video_id": "Valid001", "title": "Song", "duration": -1},
        {"source_url": "file:///tmp/song", "title": "Song"},
    ],
)
async def test_invalid_favorite_metadata_is_rejected(item):
    db = Storage(":memory:")
    await db.init()
    with pytest.raises(ValueError):
        await db.upsert_favorite_track(item)
    await db.close()
