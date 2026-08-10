"""Persisted Uzbekistan Top 10 chart and its two-day refresh scheduler.

The interactive `/top_music` path only reads SQLite.  Apple chart lookup and
YouTube resolution happen here in the background; a candidate becomes visible
only after all ten unique tracks have resolved successfully.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.search import SearchItem, search_tracks

logger = logging.getLogger(__name__)

APPLE_TOP_SONGS_URL = (
    "https://rss.marketingtools.apple.com/api/v2/uz/music/"
    "most-played/10/songs.json"
)
REFRESH_INTERVAL_SECONDS = 2 * 24 * 60 * 60
REFRESH_LEASE_SECONDS = 15 * 60
SCHEDULER_MAX_SLEEP_SECONDS = 60 * 60
TOP_MUSIC_HEADER = "<b>Top Music</b>"
TOP_MUSIC_PICK_PREFIX = "topdl:"
_YOUTUBE_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,32}")
# Long names intentionally wrap like Telegram's native chart keyboards. Keep a
# generous UI bound without confusing it with callback_data's separate 64-byte
# Bot API limit.
_BUTTON_TEXT_MAX = 100


def top_music_keyboard() -> InlineKeyboardMarkup:
    """Open Top Music from an administrator's video announcement."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text="🎧 Musiqani topish",
            callback_data="top_music",
        )]]
    )


def _top_music_item_value(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def top_music_list_keyboard(items: list[Any]) -> InlineKeyboardMarkup:
    """Build ten full-row, durable direct-download buttons."""
    if len(items) != 10:
        raise ValueError("Top Music keyboard requires exactly 10 tracks")
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        video_id = str(_top_music_item_value(item, "video_id") or "").strip()
        if _YOUTUBE_VIDEO_ID.fullmatch(video_id) is None:
            raise ValueError("Top Music track has an invalid YouTube video ID")
        title = " ".join(
            str(_top_music_item_value(item, "title") or "").split()
        )
        if not title:
            raise ValueError("Top Music track has an empty title")
        if len(title) > _BUTTON_TEXT_MAX:
            title = title[:_BUTTON_TEXT_MAX - 1].rstrip() + "…"
        rows.append([InlineKeyboardButton(
            text=title,
            callback_data=f"{TOP_MUSIC_PICK_PREFIX}{video_id}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_top_music_pick(data: str | None) -> str | None:
    """Return a validated durable video ID from a chart-track callback."""
    if not data or not data.startswith(TOP_MUSIC_PICK_PREFIX):
        return None
    video_id = data.removeprefix(TOP_MUSIC_PICK_PREFIX)
    return video_id if _YOUTUBE_VIDEO_ID.fullmatch(video_id) else None


def _parse_apple_feed(payload: Any) -> list[dict]:
    try:
        feed = payload["feed"]
        raw_results = feed["results"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Apple Top Songs response has an invalid shape") from exc
    if feed.get("country") != "uz" or not isinstance(raw_results, list):
        raise ValueError("Apple Top Songs response is not the Uzbekistan feed")
    if len(raw_results) < 10:
        raise ValueError("Apple Top Songs returned fewer than 10 songs")

    songs: list[dict] = []
    source_ids: set[str] = set()
    for raw in raw_results[:10]:
        if not isinstance(raw, dict) or raw.get("kind") != "songs":
            raise ValueError("Apple Top Songs contained a non-song result")
        source_id = str(raw.get("id") or "").strip()
        artist = str(raw.get("artistName") or "").strip()
        name = str(raw.get("name") or "").strip()
        apple_url = str(raw.get("url") or "").strip()
        if not source_id or not artist or not name:
            raise ValueError("Apple Top Songs contained incomplete metadata")
        if source_id in source_ids:
            raise ValueError("Apple Top Songs contained duplicate source IDs")
        source_ids.add(source_id)
        songs.append({
            "source_id": source_id,
            "artist": artist,
            "name": name,
            "apple_url": apple_url,
        })
    return songs


async def fetch_apple_top_songs() -> list[dict]:
    """Fetch exactly ten public chart entries from Apple's official UZ feed."""
    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    headers = {"Accept": "application/json", "User-Agent": "MusiqaBot/1.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(APPLE_TOP_SONGS_URL) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
    return _parse_apple_feed(payload)


def _valid_resolution(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    video_id = value.get("video_id")
    if not isinstance(video_id, str) or not video_id.strip():
        return None
    duration = value.get("duration")
    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return None
        if duration < 0:
            return None
    uploader = value.get("uploader") or ""
    if not isinstance(uploader, str):
        return None
    return {
        "video_id": video_id.strip(),
        "duration": duration,
        "uploader": uploader.strip(),
    }


async def _resolve_chart(
    songs: list[dict], prior_resolutions: dict,
    searcher: Callable[[str, int], Awaitable[list[SearchItem]]],
) -> list[dict]:
    """Resolve only unseen Apple IDs, without accepting duplicate videos."""
    if len(songs) != 10:
        raise ValueError("Top 10 resolution requires exactly ten source songs")
    resolved: list[dict] = []
    used_source_ids: set[str] = set()
    used_video_ids: set[str] = set()

    for song in songs:
        source_id = song["source_id"]
        if source_id in used_source_ids:
            raise ValueError("Top 10 source IDs must be unique")
        used_source_ids.add(source_id)

        resolution = _valid_resolution(prior_resolutions.get(source_id))
        # A cached mapping can collide with another song that entered the chart
        # in a later generation. Re-resolve that one entry instead of leaving
        # every future refresh permanently stuck on the same conflict.
        if resolution and resolution["video_id"] in used_video_ids:
            resolution = None
        if resolution is None:
            # Ask for several flat results once, then select the first video not
            # already used by a higher-ranked source song.
            query = f'{song["artist"]} - {song["name"]} audio'
            candidates = await searcher(query, 5)
            candidate = next(
                (
                    item for item in candidates
                    if item.video_id and item.video_id not in used_video_ids
                ),
                None,
            )
            if candidate is None:
                raise ValueError(f"no unique public video resolved for Apple ID {source_id}")
            resolution = {
                "video_id": candidate.video_id,
                "duration": candidate.duration,
                "uploader": candidate.uploader,
            }
        if resolution["video_id"] in used_video_ids:
            raise ValueError("Top 10 resolution returned a duplicate video")
        used_video_ids.add(resolution["video_id"])
        resolved.append({
            **song,
            **resolution,
            "title": f'{song["artist"]} — {song["name"]}',
        })

    if len(resolved) != 10 or len(used_video_ids) != 10:
        raise ValueError("Top 10 did not resolve to ten unique videos")
    return resolved


async def load_top_music(db) -> list[SearchItem]:
    """Load the current chart without provider work (the handler fast path)."""
    state = await db.get_top_music_state()
    return [
        SearchItem(
            video_id=item["video_id"],
            title=item["title"],
            duration=item.get("duration"),
            uploader=item.get("uploader") or "",
        )
        for item in state["items"]
    ]


def render_top_music_chart(items: list[Any]) -> str:
    """Render the intentionally minimal chart heading; tracks are buttons."""
    if len(items) != 10:
        raise ValueError("Top Music campaign requires exactly 10 tracks")
    return TOP_MUSIC_HEADER


async def refresh_top_music_once(
    db, *, now: int | None = None,
    fetcher: Callable[[], Awaitable[list[dict]]] | None = None,
    searcher: Callable[[str, int], Awaitable[list[SearchItem]]] | None = None,
    refresh_seconds: int = REFRESH_INTERVAL_SECONDS,
    retry_base_seconds: int = 30 * 60,
    queue_broadcast: bool = True,
) -> bool:
    """Refresh a due/empty snapshot once; return whether its ranking changed."""
    timestamp = int(time.time()) if now is None else int(now)
    state = await db.get_top_music_state()
    force = len(state["items"]) != 10
    claim_token = await db.claim_top_music_refresh(
        timestamp, lease_seconds=REFRESH_LEASE_SECONDS, force=force,
    )
    if claim_token is None:
        return False

    fetch = fetcher or fetch_apple_top_songs
    search = searcher or search_tracks
    try:
        songs = await fetch()
        chart = await _resolve_chart(songs, state.get("resolutions") or {}, search)
        audience_upper_bound = await db.get_active_user_upper_bound()
        changed = await db.publish_top_music(
            chart,
            now=timestamp,
            claim_token=claim_token,
            audience_upper_bound=audience_upper_bound,
            refresh_seconds=refresh_seconds,
        )
        if changed and not queue_broadcast:
            # The generalized music cadence owns proactive campaigns. Keep
            # Top Music's fast 48-hour snapshot refresh without adding another
            # three or four messages per week on top of that cadence.
            published = await db.get_top_music_state()
            pending_generation = published.get("pending_generation")
            if pending_generation is not None:
                await db.complete_top_music_broadcast(pending_generation)
        logger.info(
            "Top 10 refresh completed changed=%s generation=%s",
            str(changed).lower(),
            (await db.get_top_music_state())["generation"],
        )
        return changed
    except asyncio.CancelledError:
        await db.release_top_music_refresh(claim_token)
        raise
    except Exception as exc:
        retry_at = await db.fail_top_music_refresh(
            claim_token, now=timestamp, base_seconds=retry_base_seconds,
        )
        logger.warning(
            "Top 10 refresh failed error=%s retry_at=%s",
            type(exc).__name__, retry_at,
        )
        return False


async def broadcast_pending_top_music(
    bot: Bot, db, config, *,
    broadcaster: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """Resume and finish the current chart's persisted fan-out, if any."""
    state = await db.get_top_music_state()
    generation = state["pending_generation"]
    if generation is None:
        return False
    if generation != state["generation"] or len(state["items"]) != 10:
        logger.error("Abandoning invalid pending Top 10 generation=%s", generation)
        await db.complete_top_music_broadcast(generation)
        return True

    if broadcaster is None:
        # Imported lazily so storage/refresh tests do not depend on dispatcher
        # wiring, and so this module has no cycle with announcement handlers.
        from bot.services.broadcast import broadcast_active
        broadcaster = broadcast_active

    text = render_top_music_chart(state["items"])
    markup = top_music_list_keyboard(state["items"])

    async def send_one(user_id: int):
        return await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=markup,
        )

    async def checkpoint(user_id: int, outcome: str) -> None:
        await db.checkpoint_top_music_broadcast(generation, user_id, outcome)

    stats = await broadcaster(
        bot=bot,
        db=db,
        config=config,
        send_one=send_one,
        after_user_id=state["broadcast_cursor"],
        through_user_id=state["broadcast_upper_user_id"],
        exclude_user_id=None,
        on_outcome=checkpoint,
    )
    await db.complete_top_music_broadcast(generation)
    final_state = await db.get_top_music_state()
    logger.info(
        "Top 10 broadcast completed generation=%s sent=%s inactive=%s failed=%s",
        generation,
        final_state["broadcast_sent"],
        final_state["broadcast_inactive"],
        final_state["broadcast_failed"],
    )
    # Accessing these fields also verifies the agreed generic-service contract.
    _ = (stats.sent, stats.inactive, stats.failed)
    return True


def _scheduler_delay(state: dict, now: int) -> float:
    next_refresh = int(state.get("next_refresh_at") or 0)
    lease_until = int(state.get("refresh_lease_until") or 0)
    wake_at = next_refresh
    if next_refresh <= now < lease_until:
        wake_at = lease_until
    return float(max(1, min(SCHEDULER_MAX_SLEEP_SECONDS, wake_at - now)))


async def run_top_music_scheduler(bot: Bot, db, config) -> None:
    """Refresh the instant Top Music snapshot without proactive fan-out."""
    logger.info(
        "Top 10 scheduler started source_country=uz interval_hours=%s",
        config.top_music_refresh_hours,
    )
    while True:
        try:
            state = await db.get_top_music_state()
            # Drain a pending generation left by an older deployment. The
            # snapshot remains available; only its obsolete automatic fan-out
            # is retired in favour of the weekly campaign cadence.
            if state["pending_generation"] is not None:
                await db.complete_top_music_broadcast(
                    state["pending_generation"]
                )
                state = await db.get_top_music_state()
            now = int(time.time())
            if len(state["items"]) != 10 or state["next_refresh_at"] <= now:
                await refresh_top_music_once(
                    db,
                    now=now,
                    refresh_seconds=config.top_music_refresh_hours * 60 * 60,
                    retry_base_seconds=config.top_music_retry_minutes * 60,
                    queue_broadcast=False,
                )
                state = await db.get_top_music_state()
                now = int(time.time())
            await asyncio.sleep(_scheduler_delay(state, now))
        except asyncio.CancelledError:
            logger.info("Top 10 scheduler stopped")
            raise
        except Exception as exc:
            logger.error(
                "Top 10 scheduler cycle failed error=%s", type(exc).__name__,
            )
            await asyncio.sleep(60)
