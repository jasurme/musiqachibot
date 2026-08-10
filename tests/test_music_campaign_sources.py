"""Pure source/ranking tests for weekly music campaign snapshots."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from bot.db.storage import Storage
from bot.services.music_campaigns import (
    CAMPAIGN_DISCOVERIES,
    CAMPAIGN_NEW_MUSIC,
    CAMPAIGN_RISING,
    MOOD_QUERY_TEMPLATES,
    TASHKENT_TZ,
    _campaign_descriptor,
    add_rising_fillers,
    build_mood_collection,
    deliver_next_music_campaign,
    next_campaign_slot,
    parse_apple_chart,
    refresh_apple_collections_once,
    refresh_mood_collections_once,
    refresh_mood_collection_once,
    select_new_music_candidates,
    select_rising_bootstrap_candidates,
    select_rising_candidates,
)
from bot.services.search import SearchItem


def _apple_payload(country: str = "uz", count: int = 100) -> dict:
    return {
        "feed": {
            "country": country,
            "results": [
                {
                    "id": f"apple-{index}",
                    "kind": "songs",
                    "artistName": f"Artist {index}",
                    "name": f"Song {index}",
                    "releaseDate": "2026-08-01",
                    "url": f"https://music.apple.com/{country}/song/{index}",
                    "genres": [{"name": "Pop"}],
                }
                for index in range(count)
            ],
        }
    }


def _chart(prefix: str, *, count: int = 100) -> list[dict]:
    return [
        {
            "source_id": f"{prefix}-{index}",
            "artist": f"Artist{prefix}{index}",
            "name": f"Song {index}",
            "apple_url": f"https://music.apple.com/uz/song/{index}",
            "release_date": "2026-08-01",
            "genres": ["Pop"],
            "rank": index + 1,
            "storefront": "uz",
        }
        for index in range(count)
    ]


def test_apple_top_100_parser_keeps_rank_and_source_metadata():
    chart = parse_apple_chart(_apple_payload(), "uz")

    assert len(chart) == 100
    assert chart[0] == {
        "source_id": "apple-0",
        "artist": "Artist 0",
        "name": "Song 0",
        "apple_url": "https://music.apple.com/uz/song/0",
        "release_date": "2026-08-01",
        "genres": ["Pop"],
        "rank": 1,
        "storefront": "uz",
    }


def test_new_music_uses_recent_multi_market_pool_and_history_is_soft():
    uz = _chart("uz")
    us = _chart("us")
    for index, item in enumerate(uz):
        item["release_date"] = "2020-01-01" if index > 2 else "2026-08-01"
    for index, item in enumerate(us):
        item["release_date"] = "2026-08-02" if index < 12 else "2020-01-01"
    candidates = select_new_music_candidates(
        {"uz": uz, "us": us},
        today=date(2026, 8, 8),
        candidate_limit=10,
        recently_used={"us-0", "us-1"},
    )

    assert len(candidates) == 10
    assert all(item["age_days"] <= 8 for item in candidates)
    assert "us-0" not in {item["source_id"] for item in candidates}


def test_rising_requires_real_movers_then_adds_non_top_ten_fillers():
    baseline = _chart("song", count=30)
    current = _chart("song", count=30)
    # Three tracks move from 20/21/22 to ranks 1/2/3.
    movers = [baseline.pop(index) for index in (21, 20, 19)]
    current = movers + baseline
    for rank, item in enumerate(current, start=1):
        item["rank"] = rank
    original = _chart("song", count=30)
    meaningful = select_rising_candidates(
        current,
        original,
        candidate_limit=15,
    )

    assert len(meaningful) == 3
    filled = add_rising_fillers(meaningful, current, candidate_limit=10)
    assert len(filled) >= 5
    assert sum(bool(item.get("momentum_filler")) for item in filled) >= 2
    assert all(item["rank"] > 10 for item in filled if item.get("momentum_filler"))


def test_rising_bootstrap_prefers_diverse_tracks_outside_top_ten():
    candidates = select_rising_bootstrap_candidates(
        _chart("bootstrap", count=30), candidate_limit=5,
    )

    assert [item["rank"] for item in candidates] == [11, 12, 13, 14, 15]
    assert len({item["source_id"] for item in candidates}) == 5
    assert all(item["momentum_bootstrap"] for item in candidates)


async def test_mood_search_is_bounded_filters_mixes_and_returns_ten_unique():
    calls: list[tuple[str, int]] = []

    async def searcher(query: str, limit: int) -> list[SearchItem]:
        calls.append((query, limit))
        query_index = len(calls)
        return [
            SearchItem(
                video_id=f"m{query_index}video{index:02d}",
                title=(
                    f"Artist {query_index}-{index} - Song {query_index}-{index}"
                    if index else "One Hour Chill Playlist"
                ),
                duration=180,
                uploader=f"Channel {query_index}-{index}",
            )
            for index in range(20)
        ]

    items = await build_mood_collection(
        "night", year=2026, searcher=searcher,
    )

    assert len(calls) == len(MOOD_QUERY_TEMPLATES["night"]) == 3
    assert all(limit == 20 for _, limit in calls)
    assert len(items) == len({item["video_id"] for item in items}) == 10
    assert all("Playlist" not in item["title"] for item in items)


async def test_one_failed_mood_query_uses_the_other_two_pages():
    calls = 0

    async def searcher(query: str, limit: int) -> list[SearchItem]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("one search page failed")
        return [
            SearchItem(
                video_id=f"partial{calls:02d}{index:02d}",
                title=f"Artist {calls}-{index} - Song {calls}-{index}",
                duration=180,
                uploader=f"Channel {calls}-{index}",
            )
            for index in range(limit)
        ]

    items = await build_mood_collection("workout", searcher=searcher)

    assert calls == 3
    assert len(items) == len({item["video_id"] for item in items}) == 10


async def test_cross_mood_overlap_never_leaves_later_collections_empty(tmp_path):
    db = Storage(str(tmp_path / "overlapping-moods.db"))
    await db.init()
    shared = [
        SearchItem(
            video_id=f"sharedvideo{index:02d}",
            title=f"Artist {index} - Song {index}",
            duration=180,
            uploader=f"Channel {index}",
        )
        for index in range(20)
    ]

    async def searcher(query: str, limit: int) -> list[SearchItem]:
        return list(shared)

    outcomes = await refresh_mood_collections_once(
        db, now=100, searcher=searcher,
    )

    assert all(
        outcomes[slug]["status"] == "published"
        for slug in ("night", "road", "workout", "calm", "weekend")
    )
    for slug in ("night", "road", "workout", "calm", "weekend"):
        state = await db.get_music_collection_state(f"mood:{slug}")
        assert len(state["items"]) == 10
    await db.close()


def test_tashkent_slot_and_dedupe_are_stable_inside_the_hour():
    start = int(datetime.fromisoformat("2026-08-07T13:00:00+00:00").timestamp())
    later = start + 45 * 60

    assert datetime.fromtimestamp(
        next_campaign_slot(CAMPAIGN_NEW_MUSIC, start), TASHKENT_TZ
    ).strftime("%a %H:%M") == "Fri 18:00"
    first = _campaign_descriptor(CAMPAIGN_NEW_MUSIC, start)
    second = _campaign_descriptor(CAMPAIGN_NEW_MUSIC, later)
    assert first["dedupe_key"] == second["dedupe_key"]
    assert first["header_key"] == "new_music_header"
    assert first["eligible_at"] == start
    assert second["eligible_at"] == later


def test_all_editorial_slots_use_fixed_tashkent_weekdays():
    saturday = int(
        datetime.fromisoformat("2026-08-08T00:00:00+00:00").timestamp()
    )
    expected = {
        CAMPAIGN_DISCOVERIES: "Mon 09:00",
        CAMPAIGN_RISING: "Wed 18:00",
        CAMPAIGN_NEW_MUSIC: "Fri 18:00",
    }
    for key, label in expected.items():
        local = datetime.fromtimestamp(next_campaign_slot(key, saturday), TASHKENT_TZ)
        assert local.strftime("%a %H:%M") == label


async def test_failed_empty_mood_respects_persisted_backoff(tmp_path):
    db = Storage(str(tmp_path / "moods.db"))
    await db.init()
    calls = 0

    async def unavailable(query: str, limit: int):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider unavailable")

    failed = await refresh_mood_collection_once(
        db,
        "night",
        now=100,
        searcher=unavailable,
        retry_base_seconds=1800,
    )
    assert failed["status"] == "failed"
    assert calls == 3
    assert (await db.get_music_collection_state("mood:night"))[
        "next_refresh_at"
    ] == 1900

    async def must_not_run(query: str, limit: int):
        raise AssertionError("persisted backoff must not be bypassed")

    skipped = await refresh_mood_collection_once(
        db,
        "night",
        now=200,
        searcher=must_not_run,
        retry_base_seconds=1800,
    )
    assert skipped == {"status": "not_due"}

    good_page = 0

    async def repaired(query: str, limit: int) -> list[SearchItem]:
        nonlocal good_page
        good_page += 1
        return [
            SearchItem(
                video_id=f"repaired{good_page:02d}{index:02d}",
                title=f"Artist {good_page}-{index} - Song {good_page}-{index}",
                duration=180,
                uploader=f"Channel {good_page}-{index}",
            )
            for index in range(limit)
        ]

    forced = await refresh_mood_collection_once(
        db,
        "night",
        now=200,
        searcher=repaired,
        retry_base_seconds=1800,
        force_empty=True,
    )
    assert forced["status"] == "published"
    repaired_state = await db.get_music_collection_state("mood:night")
    assert len(repaired_state["items"]) == 10
    assert repaired_state["failure_count"] == 0
    await db.close()


async def test_failed_forced_repair_restores_normal_backoff(tmp_path):
    db = Storage(str(tmp_path / "forced-mood-backoff.db"))
    await db.init()
    calls = 0

    async def unavailable(query: str, limit: int):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider unavailable")

    first = await refresh_mood_collection_once(
        db, "weekend", now=100, searcher=unavailable,
        retry_base_seconds=1800,
    )
    assert first["status"] == "failed"
    forced = await refresh_mood_collection_once(
        db, "weekend", now=200, searcher=unavailable,
        retry_base_seconds=1800, force_empty=True,
    )
    assert forced["status"] == "failed"
    assert calls == 6
    assert (await db.get_music_collection_state("mood:weekend"))[
        "next_refresh_at"
    ] == 3800

    async def must_not_run(query: str, limit: int):
        raise AssertionError("normal retry must honor the new backoff")

    assert await refresh_mood_collection_once(
        db, "weekend", now=201, searcher=must_not_run,
    ) == {"status": "not_due"}
    await db.close()


async def test_active_empty_lease_keeps_startup_repair_pending(tmp_path):
    db = Storage(str(tmp_path / "mood-lease.db"))
    await db.init()
    await db.ensure_music_collection("mood:workout", 10)
    token = await db.claim_music_collection_refresh(
        "mood:workout", 100, force=True,
    )
    assert token

    async def must_not_run(query: str, limit: int):
        raise AssertionError("an active lease must prevent duplicate work")

    outcome = await refresh_mood_collection_once(
        db, "workout", now=101, searcher=must_not_run, force_empty=True,
    )
    assert outcome == {"status": "not_due", "repair_pending": True}
    await db.release_music_collection_refresh("mood:workout", token)
    await db.close()


def _stable_campaign_charts() -> dict[str, list[dict]]:
    chart = _chart("apple", count=30)
    for index, item in enumerate(chart):
        item["release_date"] = "2026-08-07" if index < 5 else "2020-01-01"
    return {"uz": chart}


async def test_initial_snapshot_is_silent_then_unchanged_slot_is_enqueued(tmp_path):
    db = Storage(str(tmp_path / "campaign-slot.db"))
    await db.init()
    charts = _stable_campaign_charts()
    searches = 0

    async def fetcher():
        return charts

    async def searcher(query: str, limit: int):
        nonlocal searches
        searches += 1
        number = int(query.split("Song", 1)[1].split()[0])
        return [
            SearchItem(
                video_id=f"video{number:03d}",
                title=f"Artistapple{number} - Song {number}",
                duration=180,
                uploader=f"Artistapple{number}",
            )
        ]

    saturday = int(
        datetime.fromisoformat("2026-08-08T00:00:00+00:00").timestamp()
    )
    initial = await refresh_apple_collections_once(
        db, now=saturday, fetcher=fetcher, searcher=searcher,
    )
    assert initial["collections"][CAMPAIGN_NEW_MUSIC]["status"] == "published"
    assert initial["collections"][CAMPAIGN_RISING]["status"] == "published"
    assert initial["collections"][CAMPAIGN_RISING]["bootstrap"] is True
    assert initial["collections"][CAMPAIGN_DISCOVERIES]["status"] == "published"
    assert len((await db.get_music_collection_state(CAMPAIGN_NEW_MUSIC))["items"]) == 5
    rising = await db.get_music_collection_state(CAMPAIGN_RISING)
    assert len(rising["items"]) == 5
    assert rising["provider_state"]["rising_bootstrap"] is True
    assert await db.claim_next_music_campaign(
        now=saturday, week_key="2026-W32"
    ) is None

    friday_slot = int(
        datetime.fromisoformat("2026-08-14T13:00:00+00:00").timestamp()
    )
    refreshed = await refresh_apple_collections_once(
        db, now=friday_slot, fetcher=fetcher, searcher=searcher,
    )
    assert refreshed["collections"][CAMPAIGN_NEW_MUSIC]["status"] == "published"
    assert refreshed["collections"][CAMPAIGN_NEW_MUSIC]["changed"] is False
    claimed = await db.claim_next_music_campaign(
        now=friday_slot, week_key="2026-W33"
    )
    assert claimed is not None
    assert claimed["kind"] == CAMPAIGN_NEW_MUSIC
    assert claimed["payload"]["header_key"] == "new_music_header"
    assert len(claimed["payload"]["items"]) == 5
    await db.release_music_campaign(claimed["campaign_id"], claimed["runner_token"])
    await db.close()


async def test_forced_rising_bootstrap_is_silent_then_real_movers_replace_it(
    tmp_path,
):
    db = Storage(str(tmp_path / "rising-bootstrap.db"))
    await db.init()
    active_charts = _stable_campaign_charts()

    async def fetcher():
        return active_charts

    async def searcher(query: str, limit: int):
        number = int(query.split("Song", 1)[1].split()[0])
        return [
            SearchItem(
                video_id=f"risingvideo{number:03d}",
                title=f"Artistapple{number} - Song {number}",
                duration=180,
                uploader=f"Artistapple{number}",
            )
        ]

    first_wednesday = int(
        datetime.fromisoformat("2026-08-12T13:00:00+00:00").timestamp()
    )
    # Simulate the future retry time persisted by the deployed empty Rising
    # rule. The one-shot startup repair must bypass it immediately.
    await db.ensure_music_collection(CAMPAIGN_RISING, 5)
    stale_token = await db.claim_music_collection_refresh(
        CAMPAIGN_RISING, first_wednesday - 60, force=True,
    )
    assert stale_token
    assert await db.defer_music_collection_refresh(
        CAMPAIGN_RISING,
        stale_token,
        next_refresh_at=first_wednesday + 24 * 60 * 60,
    )

    initial = await refresh_apple_collections_once(
        db,
        now=first_wednesday,
        fetcher=fetcher,
        searcher=searcher,
        force_empty=True,
    )
    rising = initial["collections"][CAMPAIGN_RISING]
    assert rising["status"] == "published"
    assert rising["bootstrap"] is True
    stored = await db.get_music_collection_state(CAMPAIGN_RISING)
    assert len(stored["items"]) == 5
    assert stored["provider_state"]["rising_bootstrap"] is True
    assert stored["next_refresh_at"] == next_campaign_slot(
        CAMPAIGN_RISING, first_wednesday + 60 * 60,
    )
    assert await db.claim_next_music_campaign(
        now=first_wednesday, week_key="2026-W33",
    ) is None

    # A quiet week keeps the useful provisional list but correctly sends no
    # Rising campaign because fewer than three movements were measured.
    second_wednesday = int(
        datetime.fromisoformat("2026-08-19T13:00:00+00:00").timestamp()
    )
    quiet = await refresh_apple_collections_once(
        db,
        now=second_wednesday,
        fetcher=fetcher,
        searcher=searcher,
    )
    assert quiet["collections"][CAMPAIGN_RISING]["status"] == (
        "no_meaningful_movers"
    )
    retained = await db.get_music_collection_state(CAMPAIGN_RISING)
    assert retained["items"] == stored["items"]
    assert retained["provider_state"]["rising_bootstrap"] is True
    assert await db.claim_next_music_campaign(
        now=second_wednesday, week_key="2026-W34",
    ) is None

    # Once three tracks objectively move, they replace the provisional list
    # and the real Wednesday campaign becomes eligible exactly once.
    current = [dict(item) for item in _stable_campaign_charts()["uz"]]
    movers = [current.pop(index) for index in (21, 20, 19)]
    current = movers + current
    for rank, item in enumerate(current, start=1):
        item["rank"] = rank
    active_charts = {"uz": current}
    third_wednesday = int(
        datetime.fromisoformat("2026-08-26T13:00:00+00:00").timestamp()
    )
    measured = await refresh_apple_collections_once(
        db,
        now=third_wednesday,
        fetcher=fetcher,
        searcher=searcher,
    )
    assert measured["collections"][CAMPAIGN_RISING]["status"] == "published"
    assert measured["collections"][CAMPAIGN_RISING]["bootstrap"] is False
    stored = await db.get_music_collection_state(CAMPAIGN_RISING)
    assert stored["provider_state"]["rising_bootstrap"] is False
    claimed = await db.claim_next_music_campaign(
        now=third_wednesday, week_key="2026-W35",
    )
    assert claimed is not None and claimed["kind"] == CAMPAIGN_RISING
    await db.release_music_campaign(
        claimed["campaign_id"], claimed["runner_token"]
    )
    await db.close()


async def test_one_mood_failure_does_not_block_other_four_snapshots(tmp_path):
    db = Storage(str(tmp_path / "mood-isolation.db"))
    await db.init()
    call_number = 0

    async def searcher(query: str, limit: int):
        nonlocal call_number
        call_number += 1
        if query in MOOD_QUERY_TEMPLATES["night"]:
            raise RuntimeError("night source failed")
        return [
            SearchItem(
                video_id=f"q{call_number:02d}vid{index:02d}",
                title=f"Artist{call_number}x{index} - Song{call_number}x{index}",
                duration=180,
                uploader=f"Channel{call_number}x{index}",
            )
            for index in range(limit)
        ]

    results = await refresh_mood_collections_once(
        db, now=100, searcher=searcher,
    )

    assert results["night"]["status"] == "failed"
    for slug in ("road", "workout", "calm", "weekend"):
        assert results[slug]["status"] == "published"
        state = await db.get_music_collection_state(f"mood:{slug}")
        assert len(state["items"]) == 10
    assert (await db.get_music_collection_state("mood:night"))["items"] == []
    await db.close()


def _published_items() -> list[dict]:
    return [
        {
            "source_id": f"source-{index}",
            "artist": f"Artist {index}",
            "name": f"Song {index}",
            "video_id": f"video-{index}",
            "title": f"Artist {index} — Song {index}",
            "duration": 180,
            "uploader": f"Channel {index}",
            "apple_url": "",
        }
        for index in range(5)
    ]


async def test_scheduled_delivery_resumes_localizes_and_reaches_all_active_users(
    tmp_path,
):
    db = Storage(str(tmp_path / "localized-resume.db"))
    await db.init()
    await db.set_locale(11, "uz")
    await db.set_locale(22, "ru")
    await db.set_locale(33, "en")
    # Preserve a real legacy preference column and disabled value. Delivery
    # must ignore it without requiring a destructive Railway migration.
    await db._db.execute(
        "ALTER TABLE users ADD COLUMN notification_mask "
        "INTEGER NOT NULL DEFAULT 7"
    )
    await db._db.execute(
        "UPDATE users SET notification_mask = 0 WHERE user_id = 33"
    )
    await db._db.commit()

    slot = int(datetime.fromisoformat("2026-08-07T13:00:00+00:00").timestamp())
    await db.ensure_music_collection(CAMPAIGN_NEW_MUSIC, 5)
    token = await db.claim_music_collection_refresh(
        CAMPAIGN_NEW_MUSIC, slot, force=False,
    )
    assert token
    descriptor = _campaign_descriptor(CAMPAIGN_NEW_MUSIC, slot)
    await db.publish_music_collection(
        CAMPAIGN_NEW_MUSIC,
        _published_items(),
        now=slot,
        claim_token=token,
        refresh_seconds=7 * 24 * 60 * 60,
        campaign=descriptor,
    )

    class FakeBot:
        def __init__(self):
            self.fail_user_22_once = True
            self.sent: list[tuple[int, str, object]] = []

        async def send_message(self, *, chat_id, text, reply_markup):
            if chat_id == 22 and self.fail_user_22_once:
                self.fail_user_22_once = False
                raise RuntimeError("simulated process interruption")
            self.sent.append((chat_id, text, reply_markup))
            return object()

    bot = FakeBot()
    config = SimpleNamespace(
        default_locale="uz", broadcast_rate_per_second=1000,
    )
    with pytest.raises(RuntimeError, match="process interruption"):
        await deliver_next_music_campaign(bot, db, config, now=slot)

    sending = await db.get_music_campaign(1)
    assert sending is not None
    assert sending["status"] == "sending"
    assert sending["broadcast_cursor"] == 11

    assert await deliver_next_music_campaign(bot, db, config, now=slot + 1)
    assert [(chat_id, text) for chat_id, text, _ in bot.sent] == [
        (11, "<b>🔥 Yangi musiqalar</b>"),
        (22, "<b>🔥 Новая музыка</b>"),
        (33, "<b>🔥 New Music</b>"),
    ]
    assert len(bot.sent[0][2].inline_keyboard) == 6  # 5 tracks + moods
    assert all(
        not (button.callback_data or "").startswith("notify:")
        for _chat_id, _text, markup in bot.sent
        for row in markup.inline_keyboard
        for button in row
    )
    completed = await db.get_music_campaign(1)
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["broadcast_sent"] == 3
    await db.close()
