"""Focused tests for the persisted, off-request-path Top 10 pipeline."""

from types import SimpleNamespace

import pytest

from bot.db.storage import Storage
from bot.services.search import SearchItem
from bot.services.top_music import (
    _resolve_chart,
    broadcast_pending_top_music,
    load_top_music,
    refresh_top_music_once,
    render_top_music_chart,
    top_music_keyboard,
)
from tests.conftest import callback_update, text_update


def _songs(prefix: str = "apple") -> list[dict]:
    return [
        {
            "source_id": f"{prefix}-{index}",
            "artist": f"Artist {index}",
            "name": f"Song {index}",
            "apple_url": f"https://music.apple.com/uz/song/{index}",
        }
        for index in range(10)
    ]


def _chart(prefix: str = "apple") -> list[dict]:
    return [
        {
            **song,
            "video_id": f"video-{index}",
            "title": f'{song["artist"]} — {song["name"]}',
            "duration": 180 + index,
            "uploader": f"Channel {index}",
        }
        for index, song in enumerate(_songs(prefix))
    ]


async def _publish(db: Storage, items: list[dict], now: int = 100) -> None:
    token = await db.claim_top_music_refresh(now, force=True)
    assert token
    assert await db.publish_top_music(
        items, now=now, claim_token=token, audience_upper_bound=999,
    )


async def test_top_music_snapshot_is_complete_atomic_and_persistent(tmp_path):
    path = tmp_path / "top-music.db"
    db = Storage(str(path))
    await db.init()
    await _publish(db, _chart())

    state = await db.get_top_music_state()
    assert state["generation"] == state["pending_generation"] == 1
    assert [item["source_id"] for item in state["items"]] == [
        f"apple-{index}" for index in range(10)
    ]
    assert len(state["resolutions"]) == 10

    # Validation happens before the single atomic UPDATE; a partial candidate
    # cannot replace the last-good chart.
    token = state["refresh_lease_token"]
    with pytest.raises(ValueError, match="exactly 10"):
        await db.publish_top_music(
            _chart()[:9], now=200, claim_token=token or "missing",
            audience_upper_bound=999,
        )
    assert (await db.get_top_music_state())["generation"] == 1
    await db.close()

    reopened = Storage(str(path))
    await reopened.init()
    assert len(await load_top_music(reopened)) == 10
    assert (await reopened.get_top_music_state())["generation"] == 1
    await reopened.close()


async def test_refresh_resolves_only_new_apple_ids_and_skips_unchanged_broadcast():
    db = Storage(":memory:")
    await db.init()
    await _publish(db, _chart(), now=100)
    await db.complete_top_music_broadcast(1)

    calls: list[str] = []

    async def no_search(query: str, limit: int):
        calls.append(query)
        raise AssertionError("known Apple IDs must reuse persisted resolutions")

    async def same_feed():
        return _songs()

    changed = await refresh_top_music_once(
        db,
        now=100 + 172800,
        fetcher=same_feed,
        searcher=no_search,
        refresh_seconds=172800,
    )
    state = await db.get_top_music_state()
    assert changed is False
    assert calls == []
    assert state["generation"] == 1
    assert state["pending_generation"] is None
    assert state["next_refresh_at"] == 100 + 2 * 172800

    replacement = _songs()
    replacement[-1] = {
        "source_id": "apple-new",
        "artist": "New Artist",
        "name": "New Song",
        "apple_url": "https://music.apple.com/uz/song/new",
    }

    async def changed_feed():
        return replacement

    async def one_search(query: str, limit: int):
        calls.append(query)
        assert limit == 5
        return [SearchItem("video-new", "result", 201, "New Channel")]

    changed = await refresh_top_music_once(
        db,
        now=100 + 2 * 172800,
        fetcher=changed_feed,
        searcher=one_search,
    )
    state = await db.get_top_music_state()
    assert changed is True
    assert calls == ["New Artist - New Song audio"]
    assert state["generation"] == state["pending_generation"] == 2
    assert state["items"][-1]["video_id"] == "video-new"
    await db.close()


async def test_resolution_repairs_a_cached_video_collision():
    songs = _songs()
    prior = {
        song["source_id"]: {
            "video_id": "duplicate" if index < 2 else f"video-{index}",
            "duration": 180,
            "uploader": "Channel",
        }
        for index, song in enumerate(songs)
    }
    calls: list[str] = []

    async def replacement(query: str, limit: int):
        calls.append(query)
        return [SearchItem("replacement", "Replacement", 181, "Channel")]

    chart = await _resolve_chart(songs, prior, replacement)

    assert calls == ["Artist 1 - Song 1 audio"]
    assert [item["video_id"] for item in chart[:2]] == [
        "duplicate", "replacement",
    ]


async def test_failed_refresh_keeps_last_good_and_persists_backoff():
    db = Storage(":memory:")
    await db.init()
    await _publish(db, _chart(), now=100)
    await db.complete_top_music_broadcast(1)

    async def unavailable():
        raise RuntimeError("temporary source outage")

    changed = await refresh_top_music_once(
        db,
        now=100 + 172800,
        fetcher=unavailable,
        retry_base_seconds=1800,
    )
    state = await db.get_top_music_state()
    assert changed is False
    assert state["generation"] == 1
    assert len(state["items"]) == 10
    assert state["failure_count"] == 1
    assert state["next_refresh_at"] == 100 + 172800 + 1800
    assert state["refresh_lease_token"] is None
    await db.close()


async def test_refresh_backoff_cap_never_undercuts_configured_retry_base():
    db = Storage(":memory:")
    await db.init()
    claim = await db.claim_top_music_refresh(100, force=True)
    assert claim
    retry_at = await db.fail_top_music_refresh(
        claim, now=100, base_seconds=3600, max_seconds=1800,
    )
    assert retry_at == 3700
    assert (await db.get_top_music_state())["next_refresh_at"] == 3700
    await db.close()


async def test_pending_chart_broadcast_checkpoints_and_completes():
    db = Storage(":memory:")
    await db.init()
    for user_id in (11, 22):
        await db.touch_private_user(user_id)
    await _publish(db, _chart(), now=100)

    sent_to: list[int] = []

    class FakeBot:
        async def send_message(self, *, chat_id, text, reply_markup):
            sent_to.append(chat_id)
            assert "Apple Music" in text
            assert reply_markup.inline_keyboard[0][0].callback_data == "top_music"
            return object()

    async def broadcaster(**kwargs):
        assert kwargs["after_user_id"] == 0
        assert kwargs["through_user_id"] == 999
        for user_id in (11, 22):
            await kwargs["send_one"](user_id)
            await kwargs["on_outcome"](user_id, "sent")
        return SimpleNamespace(sent=2, inactive=0, failed=0)

    did_work = await broadcast_pending_top_music(
        FakeBot(), db, SimpleNamespace(broadcast_rate_per_second=20),
        broadcaster=broadcaster,
    )
    state = await db.get_top_music_state()
    assert did_work is True
    assert sent_to == [11, 22]
    assert state["pending_generation"] is None
    assert state["broadcast_cursor"] == 22
    assert state["broadcast_sent"] == 2
    await db.close()


async def test_pending_chart_broadcast_resumes_after_restart(tmp_path):
    path = tmp_path / "resume.db"
    db = Storage(str(path))
    await db.init()
    for user_id in (11, 22):
        await db.touch_private_user(user_id)
    await _publish(db, _chart(), now=100)

    class FakeBot:
        async def send_message(self, **kwargs):
            return object()

    async def interrupted(**kwargs):
        await kwargs["send_one"](11)
        await kwargs["on_outcome"](11, "sent")
        raise RuntimeError("simulated Railway restart")

    with pytest.raises(RuntimeError, match="Railway restart"):
        await broadcast_pending_top_music(
            FakeBot(), db, SimpleNamespace(broadcast_rate_per_second=20),
            broadcaster=interrupted,
        )
    state = await db.get_top_music_state()
    assert state["pending_generation"] == 1
    assert state["broadcast_cursor"] == 11
    await db.close()

    reopened = Storage(str(path))
    await reopened.init()

    async def resumed(**kwargs):
        assert kwargs["after_user_id"] == 11
        await kwargs["send_one"](22)
        await kwargs["on_outcome"](22, "sent")
        return SimpleNamespace(sent=1, inactive=0, failed=0)

    await broadcast_pending_top_music(
        FakeBot(), reopened, SimpleNamespace(broadcast_rate_per_second=20),
        broadcaster=resumed,
    )
    state = await reopened.get_top_music_state()
    assert state["pending_generation"] is None
    assert state["broadcast_cursor"] == 22
    assert state["broadcast_sent"] == 2
    await reopened.close()


def test_top_music_markup_and_attributed_chart_text():
    button = top_music_keyboard().inline_keyboard[0][0]
    assert button.text == "🎧 Musiqani topish 🇺🇿"
    assert button.callback_data == "top_music"
    text = render_top_music_chart(_chart())
    assert "Oʻzbekistondagi Top 10" in text
    assert "Apple Music" in text
    assert "music.apple.com/uz" in text


async def test_top_music_command_and_media_callback_are_snapshot_only(
    dp, bot, cap, storage, monkeypatch,
):
    await _publish(storage, _chart())

    async def forbidden_network(*args, **kwargs):
        raise AssertionError("the Top 10 request path performed provider work")

    monkeypatch.setattr(
        "bot.services.top_music.fetch_apple_top_songs", forbidden_network
    )
    monkeypatch.setattr("bot.services.top_music.search_tracks", forbidden_network)

    await dp.feed_update(bot, text_update("/top_music", user_id=321, chat_id=321))
    first = cap.last("SendMessage")
    assert "Uzbekistan Top 10" in first.text
    assert "Apple Music" in first.text
    assert "<b>10.</b>" in first.text
    picks = [
        button.callback_data
        for row in first.reply_markup.inline_keyboard
        for button in row
    ]
    assert len(picks) == 10
    assert all(data.startswith("pick:") for data in picks)

    before = len(cap.by("SendMessage"))
    await dp.feed_update(
        bot,
        callback_update("top_music", uid=99, user_id=321, chat_id=321),
    )
    assert len(cap.by("SendMessage")) == before + 1
    assert cap.last("AnswerCallbackQuery") is not None
