"""Off-request-path source and selection logic for scheduled music collections.

The public Apple Marketing Tools feeds provide rankings, not a dedicated new
release or mood feed.  This module therefore keeps the source rules explicit:

* new music, rising tracks, and discoveries are derived from validated Apple
  Top-100 snapshots;
* moods use a small, bounded set of YouTube searches and are fully materialized
  before publication;
* handlers only call :func:`load_music_campaign`, which reads the last complete
  SQLite snapshot and never performs network work.

Persistence, leases, fan-out cursors, and scheduling belong to the storage and
scheduler layers.  The functions here are deliberately deterministic and can
be tested without either of them.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

import aiohttp

from bot.services.search import SearchItem, search_tracks

logger = logging.getLogger(__name__)


class NoMeaningfulRising(RuntimeError):
    """The source is healthy, but fewer than three tracks are truly rising."""

CAMPAIGN_NEW_MUSIC = "new_music"
CAMPAIGN_RISING = "rising"
CAMPAIGN_DISCOVERIES = "discoveries"
MOOD_SLUGS = ("night", "road", "workout", "calm", "weekend")
MOOD_CAMPAIGN_KEYS = tuple(f"mood:{slug}" for slug in MOOD_SLUGS)
MUSIC_CAMPAIGN_KEYS = (
    CAMPAIGN_NEW_MUSIC,
    CAMPAIGN_RISING,
    CAMPAIGN_DISCOVERIES,
    *MOOD_CAMPAIGN_KEYS,
)

EDITORIAL_CAMPAIGN_SIZE = 5
MOOD_CAMPAIGN_SIZE = 10
APPLE_CHART_LIMIT = 100
APPLE_RSS_URL = (
    "https://rss.marketingtools.apple.com/api/v2/{storefront}/music/"
    "most-played/100/songs.json"
)

# UZ is the product's home market.  The nearby storefronts prevent a quiet
# release week from producing fewer than five genuinely recent songs, while a
# small US weight supplies major international releases.
APPLE_MARKET_WEIGHTS: dict[str, float] = {
    "uz": 5.0,
    "kz": 2.0,
    "ru": 2.0,
    "tr": 1.5,
    "us": 1.0,
}
APPLE_MARKETS = tuple(APPLE_MARKET_WEIGHTS)
RRF_K = 60.0

MIN_TRACK_SECONDS = 60
MAX_TRACK_SECONDS = 10 * 60
APPLE_RESOLVE_RESULTS = 5
MOOD_RESULTS_PER_QUERY = 20
MOOD_MAX_SEARCH_CALLS = 3
WEEK_SECONDS = 7 * 24 * 60 * 60
APPLE_CHART_HISTORY_KEY = "apple:uz:top100"
APPLE_RISING_BASELINE_SECONDS = 6 * 24 * 60 * 60
REFRESH_LEASE_SECONDS = 15 * 60

CAMPAIGN_HEADER_KEYS: dict[str, str] = {
    CAMPAIGN_NEW_MUSIC: "new_music_header",
    CAMPAIGN_RISING: "rising_header",
    CAMPAIGN_DISCOVERIES: "discoveries_header",
}
CAMPAIGN_PRIORITIES: dict[str, int] = {
    CAMPAIGN_NEW_MUSIC: 30,
    CAMPAIGN_RISING: 20,
    CAMPAIGN_DISCOVERIES: 10,
}
CAMPAIGN_SLOTS: dict[str, tuple[int, int]] = {
    # Python weekday: Monday=0. Times are fixed Asia/Tashkent (UTC+5).
    CAMPAIGN_DISCOVERIES: (0, 9),
    CAMPAIGN_RISING: (2, 18),
    CAMPAIGN_NEW_MUSIC: (4, 18),
}
TASHKENT_TZ = timezone(timedelta(hours=5), name="Asia/Tashkent")
CAMPAIGN_SLOT_WINDOW_SECONDS = 60 * 60
CAMPAIGN_EXPIRY_SECONDS = 24 * 60 * 60

_YOUTUBE_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,32}")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_TITLE_SEPARATOR = re.compile(r"\s+[\-\u2013\u2014]\s+", re.UNICODE)
_REJECT_SEARCH_TITLE = re.compile(
    r"(?:\b(?:full\s+album|playlist|nonstop|nightcore|karaoke|mashup|"
    r"slowed|reverb|sped\s+up|one\s+hour|1\s*hour|live\s+stream|"
    r"compilation|top\s*100)\b|\b(?:сборник|подборка|караоке|концерт|"
    r"часовая)\b|#shorts)",
    re.IGNORECASE,
)

# These are discovery queries, not claims that the result itself is an
# official upload. Broad genre/activity phrases are intentional: year-scoped
# "hits" queries frequently collapse to playlists and mixes, leaving fewer
# than ten usable videos even when YouTube search itself is healthy. Candidate
# validation below still removes long mixes, streams, karaoke, duplicate
# videos, and implausible durations.
MOOD_QUERY_TEMPLATES: dict[str, tuple[str, ...]] = {
    "night": (
        "night drive music official audio",
        "midnight pop official audio",
        "dreamy pop official audio",
    ),
    "road": (
        "road trip song official audio",
        "driving rock official audio",
        "summer driving song official audio",
    ),
    "workout": (
        "upbeat pop official audio",
        "hardstyle official audio",
        "electronic workout song official audio",
    ),
    "calm": (
        "acoustic pop official audio",
        "soft pop official audio",
        "acoustic indie official audio",
    ),
    "weekend": (
        "dance pop official audio",
        "party dance song official audio",
        "afro house official audio",
    ),
}


def mood_campaign_key(slug: str) -> str:
    """Return the durable collection key for a supported mood slug."""
    normalized = str(slug or "").strip().lower()
    if normalized not in MOOD_SLUGS:
        raise ValueError(f"unsupported mood slug: {slug!r}")
    return f"mood:{normalized}"


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _parse_release_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def parse_apple_chart(payload: Any, storefront: str) -> list[dict[str, Any]]:
    """Validate and normalize one public Apple Top-100 songs response."""
    wanted_country = str(storefront or "").strip().lower()
    try:
        feed = payload["feed"]
        results = feed["results"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Apple chart response has an invalid shape") from exc
    if not isinstance(feed, dict) or feed.get("country") != wanted_country:
        raise ValueError("Apple chart response has the wrong storefront")
    if not isinstance(results, list) or len(results) < 10:
        raise ValueError("Apple chart returned fewer than 10 songs")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank, raw in enumerate(results[:APPLE_CHART_LIMIT], start=1):
        if not isinstance(raw, dict) or raw.get("kind") != "songs":
            raise ValueError("Apple chart contained a non-song result")
        source_id = _text(raw.get("id"))
        artist = _text(raw.get("artistName"))
        name = _text(raw.get("name"))
        if not source_id or not artist or not name:
            raise ValueError("Apple chart contained incomplete song metadata")
        if source_id in seen:
            raise ValueError("Apple chart contained duplicate song IDs")
        seen.add(source_id)
        raw_genres = raw.get("genres")
        genres = []
        if isinstance(raw_genres, list):
            genres = [
                _text(genre.get("name"))
                for genre in raw_genres
                if isinstance(genre, dict) and _text(genre.get("name"))
            ]
        normalized.append({
            "source_id": source_id,
            "artist": artist,
            "name": name,
            "apple_url": _text(raw.get("url")),
            "release_date": _text(raw.get("releaseDate")),
            "genres": genres,
            "rank": rank,
            "storefront": wanted_country,
        })
    return normalized


async def fetch_apple_market_chart(
    storefront: str,
    *,
    session: aiohttp.ClientSession | None = None,
) -> list[dict[str, Any]]:
    """Fetch one official Apple Marketing Tools Top-100 songs feed."""
    wanted_country = str(storefront or "").strip().lower()
    if not re.fullmatch(r"[a-z]{2}", wanted_country):
        raise ValueError("Apple storefront must be a two-letter country code")
    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    headers = {"Accept": "application/json", "User-Agent": "MusiqaBot/1.0"}
    owns_session = session is None
    client = session or aiohttp.ClientSession(timeout=timeout, headers=headers)
    try:
        async with client.get(
            APPLE_RSS_URL.format(storefront=wanted_country)
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
    finally:
        if owns_session:
            await client.close()
    return parse_apple_chart(payload, wanted_country)


async def fetch_apple_market_charts(
    storefronts: Sequence[str] = APPLE_MARKETS,
    *,
    minimum_markets: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch market feeds concurrently, requiring UZ plus a safe quorum."""
    countries = tuple(dict.fromkeys(str(value).strip().lower() for value in storefronts))
    if "uz" not in countries:
        raise ValueError("the UZ chart is required")
    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    headers = {"Accept": "application/json", "User-Agent": "MusiqaBot/1.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        values = await asyncio.gather(
            *(fetch_apple_market_chart(country, session=session) for country in countries),
            return_exceptions=True,
        )

    charts: dict[str, list[dict[str, Any]]] = {}
    failures: dict[str, BaseException] = {}
    for country, value in zip(countries, values, strict=True):
        if isinstance(value, BaseException):
            failures[country] = value
        else:
            charts[country] = value
    if "uz" not in charts:
        raise RuntimeError("the required Apple UZ chart is unavailable") from failures.get("uz")
    required = min(len(countries), max(1, int(minimum_markets)))
    if len(charts) < required:
        raise RuntimeError(
            f"only {len(charts)} of {len(countries)} Apple charts were available"
        )
    for country, error in failures.items():
        logger.warning(
            "Optional Apple chart unavailable storefront=%s error=%s",
            country,
            type(error).__name__,
        )
    return charts


def aggregate_market_charts(
    charts: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    weights: Mapping[str, float] = APPLE_MARKET_WEIGHTS,
) -> list[dict[str, Any]]:
    """Merge storefront rankings with weighted reciprocal-rank fusion."""
    merged: dict[str, dict[str, Any]] = {}
    for storefront, songs in charts.items():
        weight = max(0.0, float(weights.get(storefront, 1.0)))
        for fallback_rank, source in enumerate(songs, start=1):
            source_id = _text(source.get("source_id"))
            artist = _text(source.get("artist"))
            name = _text(source.get("name"))
            if not source_id or not artist or not name:
                continue
            try:
                rank = max(1, int(source.get("rank") or fallback_rank))
            except (TypeError, ValueError):
                rank = fallback_rank
            item = merged.get(source_id)
            if item is None:
                item = {
                    "source_id": source_id,
                    "artist": artist,
                    "name": name,
                    "apple_url": _text(source.get("apple_url")),
                    "release_date": _text(source.get("release_date")),
                    "genres": list(source.get("genres") or []),
                    "market_ranks": {},
                    "rrf_score": 0.0,
                }
                merged[source_id] = item
            # Prefer UZ metadata when the same catalog ID appears in several
            # feeds, while retaining the first complete fallback otherwise.
            if storefront == "uz":
                item.update({
                    "artist": artist,
                    "name": name,
                    "apple_url": _text(source.get("apple_url")),
                    "release_date": _text(source.get("release_date")),
                    "genres": list(source.get("genres") or []),
                })
            item["market_ranks"][storefront] = rank
            item["rrf_score"] += weight / (RRF_K + rank)
    return sorted(
        merged.values(),
        key=lambda item: (-float(item["rrf_score"]), item["source_id"]),
    )


def _artist_key(value: Any) -> str:
    return " ".join(_tokens(value))


def _diverse_take(
    candidates: Sequence[dict[str, Any]],
    *,
    limit: int,
    excluded: set[str] | None = None,
    max_per_artist: int = 1,
) -> list[dict[str, Any]]:
    excluded_ids = excluded or set()
    selected: list[dict[str, Any]] = []
    artist_counts: dict[str, int] = {}
    seen: set[str] = set()
    for item in candidates:
        source_id = _text(item.get("source_id"))
        artist = _artist_key(item.get("artist")) or _text(item.get("artist")).casefold()
        if (
            not source_id
            or source_id in seen
            or source_id in excluded_ids
            or artist_counts.get(artist, 0) >= max_per_artist
        ):
            continue
        selected.append(dict(item))
        seen.add(source_id)
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def select_new_music_candidates(
    charts: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    today: date | None = None,
    candidate_limit: int = 20,
    recently_used: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Rank recent charting releases, widening age only when necessary."""
    merged = aggregate_market_charts(charts)
    current_day = today or datetime.now(TASHKENT_TZ).date()
    recent_ids = {_text(value) for value in recently_used if _text(value)}
    dated: list[dict[str, Any]] = []
    for item in merged:
        released = _parse_release_date(item.get("release_date"))
        if released is None:
            continue
        age_days = (current_day - released).days
        if age_days < 0 or age_days > 365:
            continue
        candidate = dict(item)
        candidate["age_days"] = age_days
        dated.append(candidate)

    # Prefer the last release week, but expand rather than publishing an
    # incomplete list during a quiet UZ week.
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for age_limit in (8, 14, 28, 56, 365):
        window = sorted(
            (item for item in dated if item["age_days"] <= age_limit),
            key=lambda item: (
                item["age_days"],
                -float(item["rrf_score"]),
                item["source_id"],
            ),
        )
        for item in window:
            if item["source_id"] not in seen:
                seen.add(item["source_id"])
                ordered.append(item)
        if len(ordered) >= candidate_limit:
            break

    # History exclusion is a quality preference, not a reason to destroy the
    # last-good collection when the source pool is temporarily small.
    selected = _diverse_take(
        ordered,
        limit=candidate_limit,
        excluded=recent_ids,
        max_per_artist=2,
    )
    if len(selected) < candidate_limit:
        already = {item["source_id"] for item in selected}
        selected.extend(_diverse_take(
            [item for item in ordered if item["source_id"] not in already],
            limit=candidate_limit - len(selected),
            max_per_artist=2,
        ))
    return selected


def select_rising_candidates(
    current_chart: Sequence[Mapping[str, Any]],
    baseline_chart: Sequence[Mapping[str, Any]],
    *,
    candidate_limit: int = 15,
    recently_used: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Select meaningful seven-day rank gains and strong new entries."""
    baseline_ranks = {
        _text(item.get("source_id")): int(item.get("rank") or rank)
        for rank, item in enumerate(baseline_chart, start=1)
        if _text(item.get("source_id"))
    }
    used = {_text(value) for value in recently_used if _text(value)}
    candidates: list[dict[str, Any]] = []
    for fallback_rank, source in enumerate(current_chart, start=1):
        item = dict(source)
        source_id = _text(item.get("source_id"))
        if not source_id:
            continue
        rank = max(1, int(item.get("rank") or fallback_rank))
        old_rank = baseline_ranks.get(source_id)
        if old_rank is None:
            # A new entry near the bottom is noise; a new Top-50 entry is a
            # useful signal even though its exact prior rank is unknowable.
            if rank > 50:
                continue
            rise = None
            score = 200.0 + (101 - rank)
            new_entry = True
        else:
            rise = old_rank - rank
            if rise < 3:
                continue
            score = rise * 5.0 + (101 - rank) / 100.0
            new_entry = False
        item.update({
            "rank": rank,
            "previous_rank": old_rank,
            "rank_rise": rise,
            "new_entry": new_entry,
            "momentum_score": score,
        })
        candidates.append(item)
    candidates.sort(
        key=lambda item: (-float(item["momentum_score"]), item["rank"], item["source_id"])
    )
    selected = _diverse_take(
        candidates,
        limit=candidate_limit,
        excluded=used,
        max_per_artist=1,
    )
    if len(selected) < candidate_limit:
        already = {item["source_id"] for item in selected}
        selected.extend(_diverse_take(
            [item for item in candidates if item["source_id"] not in already],
            limit=candidate_limit - len(selected),
            max_per_artist=2,
        ))
    return selected


def add_rising_fillers(
    meaningful: Sequence[Mapping[str, Any]],
    current_chart: Sequence[Mapping[str, Any]],
    *,
    candidate_limit: int = 15,
) -> list[dict[str, Any]]:
    """Add strong non-Top-10 fallbacks after at least three real movers."""
    if len(meaningful) < 3:
        return [dict(item) for item in meaningful]
    combined = [dict(item) for item in meaningful]
    seen = {_text(item.get("source_id")) for item in combined}
    artist_counts: dict[str, int] = {}
    for item in combined:
        artist = _artist_key(item.get("artist"))
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
    for fallback_rank, source in enumerate(current_chart, start=1):
        source_id = _text(source.get("source_id"))
        rank = max(1, int(source.get("rank") or fallback_rank))
        artist = _artist_key(source.get("artist"))
        if (
            not source_id
            or source_id in seen
            or rank <= 10
            or artist_counts.get(artist, 0) >= 1
        ):
            continue
        item = dict(source)
        item.update({
            "rank": rank,
            "previous_rank": None,
            "rank_rise": None,
            "new_entry": False,
            "momentum_filler": True,
        })
        combined.append(item)
        seen.add(source_id)
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
        if len(combined) >= candidate_limit:
            break
    return combined


def select_rising_bootstrap_candidates(
    current_chart: Sequence[Mapping[str, Any]],
    *,
    candidate_limit: int = 15,
    recently_used: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Build a useful initial Rising pool while rank history accumulates.

    A new installation needs roughly six days before it can prove movement.
    Keeping the collection empty during that period makes the instant command
    unusable, so initialization prefers strong entries just outside the Top 10
    and falls back to the rest of the current chart. This bootstrap is never
    proactively broadcast and is replaced by measured movers at a later slot.
    """
    used = {_text(value) for value in recently_used if _text(value)}
    outside_top_ten: list[dict[str, Any]] = []
    top_ten: list[dict[str, Any]] = []
    for fallback_rank, source in enumerate(current_chart, start=1):
        source_id = _text(source.get("source_id"))
        artist = _text(source.get("artist"))
        name = _text(source.get("name"))
        if not source_id or not artist or not name:
            continue
        try:
            rank = max(1, int(source.get("rank") or fallback_rank))
        except (TypeError, ValueError):
            rank = fallback_rank
        item = dict(source)
        item.update({
            "rank": rank,
            "previous_rank": None,
            "rank_rise": None,
            "new_entry": False,
            "momentum_bootstrap": True,
        })
        (outside_top_ten if rank > 10 else top_ten).append(item)
    ordered = sorted(outside_top_ten, key=lambda item: item["rank"])
    ordered.extend(sorted(top_ten, key=lambda item: item["rank"]))

    selected = _diverse_take(
        ordered,
        limit=candidate_limit,
        excluded=used,
        max_per_artist=1,
    )
    if len(selected) < candidate_limit:
        already = {item["source_id"] for item in selected}
        selected.extend(_diverse_take(
            [item for item in ordered if item["source_id"] not in already],
            limit=candidate_limit - len(selected),
            max_per_artist=2,
        ))
    return selected


def select_discovery_candidates(
    charts: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    baseline_uz: Sequence[Mapping[str, Any]] = (),
    today: date | None = None,
    candidate_limit: int = 20,
    recently_used: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Find diverse charting tracks outside the obvious UZ Top 10."""
    merged = aggregate_market_charts(charts)
    baseline_ranks = {
        _text(item.get("source_id")): int(item.get("rank") or rank)
        for rank, item in enumerate(baseline_uz, start=1)
        if _text(item.get("source_id"))
    }
    current_day = today or datetime.now(TASHKENT_TZ).date()
    used = {_text(value) for value in recently_used if _text(value)}
    candidates: list[dict[str, Any]] = []
    for source in merged:
        market_ranks = dict(source.get("market_ranks") or {})
        uz_rank = market_ranks.get("uz")
        if uz_rank is not None and int(uz_rank) <= 10:
            continue
        regional_presence = sum(
            1 for country in ("uz", "kz", "ru", "tr")
            if country in market_ranks and int(market_ranks[country]) <= 75
        )
        if regional_presence == 0:
            continue
        momentum = 0
        if uz_rank is not None and source["source_id"] in baseline_ranks:
            momentum = max(0, baseline_ranks[source["source_id"]] - int(uz_rank))
        released = _parse_release_date(source.get("release_date"))
        age_days = (current_day - released).days if released else 365
        freshness = max(0.0, 1.0 - max(0, age_days) / 365.0)
        item = dict(source)
        item["discovery_score"] = (
            float(source["rrf_score"]) * 1000.0
            + regional_presence * 2.0
            + momentum * 0.4
            + freshness
        )
        item["regional_presence"] = regional_presence
        item["rank_rise"] = momentum
        candidates.append(item)
    candidates.sort(
        key=lambda item: (-float(item["discovery_score"]), item["source_id"])
    )
    selected = _diverse_take(
        candidates,
        limit=candidate_limit,
        excluded=used,
        max_per_artist=1,
    )
    if len(selected) < candidate_limit:
        already = {item["source_id"] for item in selected}
        selected.extend(_diverse_take(
            [item for item in candidates if item["source_id"] not in already],
            limit=candidate_limit - len(selected),
            max_per_artist=2,
        ))
    return selected


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value).casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _tokens(value: Any) -> tuple[str, ...]:
    return tuple(token for token in _WORD.findall(_fold(value)) if len(token) > 1)


def _valid_search_item(item: Any) -> bool:
    if not isinstance(item, SearchItem):
        return False
    if _YOUTUBE_VIDEO_ID.fullmatch(_text(item.video_id)) is None:
        return False
    if not _text(item.title) or _REJECT_SEARCH_TITLE.search(_fold(item.title)):
        return False
    if item.duration is None:
        return False
    try:
        duration = int(item.duration)
    except (TypeError, ValueError):
        return False
    return MIN_TRACK_SECONDS <= duration <= MAX_TRACK_SECONDS


def _valid_resolution(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    video_id = _text(value.get("video_id"))
    if _YOUTUBE_VIDEO_ID.fullmatch(video_id) is None:
        return None
    duration = value.get("duration")
    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return None
        if duration < 0 or duration > MAX_TRACK_SECONDS:
            return None
    return {
        "video_id": video_id,
        "duration": duration,
        "uploader": _text(value.get("uploader")),
    }


def _match_score(item: SearchItem, artist: str, name: str) -> float:
    haystack = set(_tokens(f"{item.title} {item.uploader}"))
    title_tokens = set(_tokens(name))
    artist_tokens = set(_tokens(artist))
    title_match = len(haystack & title_tokens) / max(1, len(title_tokens))
    artist_match = len(haystack & artist_tokens) / max(1, len(artist_tokens))
    return title_match * 2.0 + artist_match


async def resolve_apple_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    count: int,
    prior_resolutions: Mapping[str, Any] | None = None,
    searcher: Callable[[str, int], Awaitable[list[SearchItem]]] = search_tracks,
) -> list[dict[str, Any]]:
    """Resolve a ranked surplus to exactly ``count`` unique YouTube tracks."""
    wanted = max(1, int(count))
    cache = prior_resolutions or {}
    resolved: list[dict[str, Any]] = []
    used_sources: set[str] = set()
    used_videos: set[str] = set()

    for raw in candidates:
        source_id = _text(raw.get("source_id"))
        artist = _text(raw.get("artist"))
        name = _text(raw.get("name"))
        if not source_id or not artist or not name or source_id in used_sources:
            continue
        used_sources.add(source_id)
        resolution = _valid_resolution(cache.get(source_id))
        if resolution and resolution["video_id"] in used_videos:
            resolution = None
        if resolution is None:
            results = await searcher(f"{artist} - {name} official audio", APPLE_RESOLVE_RESULTS)
            usable = [item for item in results if _valid_search_item(item)]
            usable.sort(key=lambda item: -_match_score(item, artist, name))
            match = next(
                (item for item in usable if item.video_id not in used_videos),
                None,
            )
            if match is None or _match_score(match, artist, name) < 0.75:
                logger.warning("No reliable video match for source_id=%s", source_id)
                continue
            resolution = {
                "video_id": match.video_id,
                "duration": match.duration,
                "uploader": match.uploader,
            }
        used_videos.add(resolution["video_id"])
        resolved.append({
            "source_id": source_id,
            "artist": artist,
            "name": name,
            "apple_url": _text(raw.get("apple_url")),
            "video_id": resolution["video_id"],
            "title": f"{artist} — {name}",
            "duration": resolution["duration"],
            "uploader": resolution["uploader"],
        })
        if len(resolved) == wanted:
            break
    if len(resolved) != wanted:
        raise ValueError(f"only {len(resolved)} of {wanted} campaign tracks resolved")
    return resolved


def _mood_item(item: SearchItem) -> dict[str, Any]:
    title = _text(item.title)
    parts = _TITLE_SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2 and all(_text(part) for part in parts):
        artist, name = (_text(parts[0]), _text(parts[1]))
    else:
        artist, name = (_text(item.uploader) or "Music", title)
    return {
        "source_id": f"youtube:{item.video_id}",
        "artist": artist,
        "name": name,
        "apple_url": "",
        "video_id": item.video_id,
        "title": f"{artist} — {name}" if artist != "Music" else name,
        "duration": int(item.duration) if item.duration is not None else None,
        "uploader": _text(item.uploader),
    }


async def build_mood_collection(
    mood: str,
    *,
    count: int = MOOD_CAMPAIGN_SIZE,
    year: int | None = None,
    recently_used: Iterable[str] = (),
    reserved_source_ids: Iterable[str] = (),
    searcher: Callable[[str, int], Awaitable[list[SearchItem]]] = search_tracks,
) -> list[dict[str, Any]]:
    """Build one mood snapshot with at most three bounded provider searches."""
    slug = str(mood or "").strip().lower()
    if slug not in MOOD_SLUGS:
        raise ValueError(f"unsupported mood slug: {mood!r}")
    wanted = max(1, int(count))
    query_year = int(year or datetime.now(timezone.utc).year)
    query_results: list[list[SearchItem]] = []
    query_failures: list[Exception] = []
    for query_index, template in enumerate(
        MOOD_QUERY_TEMPLATES[slug][:MOOD_MAX_SEARCH_CALLS], start=1,
    ):
        query = template.format(year=query_year)
        try:
            results = await searcher(query, MOOD_RESULTS_PER_QUERY)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Search pages are independent. One malformed/localized result
            # page must not discard enough good candidates from the others to
            # leave an otherwise healthy mood unavailable.
            query_failures.append(exc)
            logger.warning(
                "Mood search query failed mood=%s query=%s error=%s",
                slug,
                query_index,
                type(exc).__name__,
            )
            query_results.append([])
            continue
        query_results.append([item for item in results if _valid_search_item(item)])

    if query_failures and len(query_failures) == len(query_results):
        raise query_failures[0]

    # Round-robin preserves local/global query diversity instead of allowing
    # one result page to consume the whole collection.
    ranked: list[SearchItem] = []
    longest = max((len(items) for items in query_results), default=0)
    for rank in range(longest):
        for items in query_results:
            if rank < len(items):
                ranked.append(items[rank])

    historical = {_text(value) for value in recently_used if _text(value)}
    reserved = {_text(value) for value in reserved_source_ids if _text(value)}
    selected: list[dict[str, Any]] = []
    seen_videos: set[str] = set()
    artist_counts: dict[str, int] = {}

    def take(
        *, exclude_history: bool, exclude_reserved: bool,
        max_per_artist: int,
    ) -> None:
        for result in ranked:
            source_id = f"youtube:{result.video_id}"
            if result.video_id in seen_videos:
                continue
            if exclude_reserved and source_id in reserved:
                continue
            if exclude_history and source_id in historical:
                continue
            candidate = _mood_item(result)
            artist = _artist_key(candidate["artist"])
            if artist_counts.get(artist, 0) >= max_per_artist:
                continue
            selected.append(candidate)
            seen_videos.add(result.video_id)
            artist_counts[artist] = artist_counts.get(artist, 0) + 1
            if len(selected) >= wanted:
                return

    take(exclude_history=True, exclude_reserved=True, max_per_artist=1)
    if len(selected) < wanted:
        take(exclude_history=True, exclude_reserved=True, max_per_artist=2)
    if len(selected) < wanted:
        take(exclude_history=False, exclude_reserved=True, max_per_artist=2)
    if len(selected) < wanted:
        # Cross-mood uniqueness is a quality preference. Reusing an otherwise
        # valid song is better than exposing a permanently empty menu option.
        take(exclude_history=False, exclude_reserved=False, max_per_artist=2)
    if len(selected) < wanted:
        # Likewise, provider pages occasionally contain many releases from one
        # official channel. Keep unique videos as the hard invariant while
        # relaxing artist diversity only as the final availability fallback.
        take(
            exclude_history=False,
            exclude_reserved=False,
            max_per_artist=wanted,
        )
    if len(selected) != wanted:
        raise ValueError(f"only {len(selected)} of {wanted} {slug} tracks resolved")
    return selected


def next_campaign_slot(campaign_key: str, now: int | float) -> int:
    """Return this hour's slot or the next fixed Tashkent weekly slot."""
    if campaign_key not in CAMPAIGN_SLOTS:
        raise ValueError(f"unsupported proactive campaign: {campaign_key!r}")
    timestamp = int(now)
    weekday, hour = CAMPAIGN_SLOTS[campaign_key]
    local_now = datetime.fromtimestamp(timestamp, TASHKENT_TZ)
    start_today = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if (
        local_now.weekday() == weekday
        and 0 <= (local_now - start_today).total_seconds()
        < CAMPAIGN_SLOT_WINDOW_SECONDS
    ):
        return timestamp
    days = (weekday - local_now.weekday()) % 7
    candidate = start_today + timedelta(days=days)
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return int(candidate.timestamp())


def _in_campaign_slot(campaign_key: str, now: int) -> bool:
    return next_campaign_slot(campaign_key, now) == int(now)


def _campaign_descriptor(campaign_key: str, now: int) -> dict[str, Any]:
    eligible_at = next_campaign_slot(campaign_key, now)
    weekday, hour = CAMPAIGN_SLOTS[campaign_key]
    local_now = datetime.fromtimestamp(int(now), TASHKENT_TZ)
    if _in_campaign_slot(campaign_key, now):
        canonical_slot = local_now.replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
    else:
        canonical_slot = datetime.fromtimestamp(eligible_at, TASHKENT_TZ)
    # The weekday assertion catches accidental drift if slot definitions are
    # edited without updating the scheduler helper.
    if canonical_slot.weekday() != weekday:  # pragma: no cover - invariant
        raise RuntimeError("campaign slot calculation drifted")
    slot_timestamp = int(canonical_slot.timestamp())
    return {
        "dedupe_key": f"{campaign_key}:{slot_timestamp}",
        "kind": campaign_key,
        "header_key": CAMPAIGN_HEADER_KEYS[campaign_key],
        "eligible_at": eligible_at,
        "expires_at": eligible_at + CAMPAIGN_EXPIRY_SECONDS,
        "priority": CAMPAIGN_PRIORITIES[campaign_key],
    }


def _snapshot_items(snapshot: Any) -> list[dict[str, Any]]:
    if isinstance(snapshot, Mapping):
        raw = snapshot.get("items")
    else:
        raw = snapshot
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _recent_source_ids(state: Mapping[str, Any]) -> list[str]:
    provider_state = state.get("provider_state")
    raw = provider_state.get("recent_source_ids") if isinstance(provider_state, Mapping) else []
    if not isinstance(raw, list):
        return []
    return [_text(value) for value in raw if _text(value)]


def _next_provider_state(
    state: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *, now: int,
) -> dict[str, Any]:
    previous = _recent_source_ids(state)
    current = [_text(item.get("source_id")) for item in items]
    recent: list[str] = []
    for source_id in (*previous, *current):
        if source_id:
            if source_id in recent:
                recent.remove(source_id)
            recent.append(source_id)
    return {
        "recent_source_ids": recent[-100:],
        "source_refreshed_at": int(now),
    }


def _next_resolutions(
    state: Mapping[str, Any], items: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    raw = state.get("resolutions")
    resolutions = dict(raw) if isinstance(raw, Mapping) else {}
    for item in items:
        source_id = _text(item.get("source_id"))
        video_id = _text(item.get("video_id"))
        if not source_id or _YOUTUBE_VIDEO_ID.fullmatch(video_id) is None:
            continue
        resolutions.pop(source_id, None)
        resolutions[source_id] = {
            "video_id": video_id,
            "duration": item.get("duration"),
            "uploader": _text(item.get("uploader")),
        }
    while len(resolutions) > 500:
        resolutions.pop(next(iter(resolutions)))
    return resolutions


async def _publish_apple_collection(
    db: Any,
    *,
    campaign_key: str,
    state: Mapping[str, Any],
    claim_token: str,
    charts: Mapping[str, Sequence[Mapping[str, Any]]],
    baseline_uz: Sequence[Mapping[str, Any]],
    now: int,
    searcher: Callable[[str, int], Awaitable[list[SearchItem]]],
) -> dict[str, Any]:
    stored_provider_state = state.get("provider_state")
    has_rising_bootstrap = bool(
        campaign_key == CAMPAIGN_RISING
        and isinstance(stored_provider_state, Mapping)
        and stored_provider_state.get("rising_bootstrap")
    )
    # Provisional ranks 11+ can become tomorrow's real movers. Do not let the
    # bootstrap's own repeat-avoidance list hide them from the first measured
    # six-day comparison.
    recent = [] if has_rising_bootstrap else _recent_source_ids(state)
    source_day = datetime.fromtimestamp(now, timezone.utc).date()
    bootstrap = False
    if campaign_key == CAMPAIGN_NEW_MUSIC:
        candidates = select_new_music_candidates(
            charts,
            today=source_day,
            candidate_limit=20,
            recently_used=recent,
        )
    elif campaign_key == CAMPAIGN_RISING:
        has_complete_snapshot = (
            len(state.get("items") or []) == EDITORIAL_CAMPAIGN_SIZE
        )
        meaningful: list[dict[str, Any]] = []
        if baseline_uz:
            meaningful = select_rising_candidates(
                charts["uz"], baseline_uz, candidate_limit=15,
                recently_used=recent,
            )
        if len(meaningful) >= 3:
            candidates = add_rising_fillers(
                meaningful, charts["uz"], candidate_limit=15,
            )
            bootstrap = False
        elif has_complete_snapshot:
            if not baseline_uz:
                raise LookupError("a six-day Apple chart baseline is not ready")
            raise NoMeaningfulRising(
                f"only {len(meaningful)} meaningful movers are available"
            )
        else:
            # Never leave /rising empty while its six-day movement baseline is
            # being accumulated. The bootstrap is a current-chart snapshot,
            # not a claim that rank movement has already been measured.
            candidates = select_rising_bootstrap_candidates(
                charts["uz"], candidate_limit=15, recently_used=recent,
            )
            bootstrap = True
    elif campaign_key == CAMPAIGN_DISCOVERIES:
        candidates = select_discovery_candidates(
            charts,
            baseline_uz=baseline_uz,
            today=source_day,
            candidate_limit=20,
            recently_used=recent,
        )
    else:  # pragma: no cover - guarded by the caller
        raise ValueError(f"unsupported Apple collection: {campaign_key}")

    items = await resolve_apple_candidates(
        candidates,
        count=EDITORIAL_CAMPAIGN_SIZE,
        prior_resolutions=(
            state.get("resolutions")
            if isinstance(state.get("resolutions"), Mapping)
            else {}
        ),
        searcher=searcher,
    )
    source_in_slot = _in_campaign_slot(campaign_key, now)
    in_slot = source_in_slot and not bootstrap
    # Initial snapshots are materialized immediately for command speed, but a
    # deploy outside the editorial slot must not blast three messages at once.
    campaign = _campaign_descriptor(campaign_key, now) if in_slot else None
    next_slot = next_campaign_slot(
        campaign_key,
        now + CAMPAIGN_SLOT_WINDOW_SECONDS if bootstrap and source_in_slot else now,
    )
    refresh_seconds = WEEK_SECONDS if in_slot else max(60, next_slot - now)
    next_provider_state = _next_provider_state(state, items, now=now)
    if campaign_key == CAMPAIGN_RISING:
        next_provider_state["rising_bootstrap"] = bootstrap
    result = await db.publish_music_collection(
        campaign_key,
        items,
        now=now,
        claim_token=claim_token,
        refresh_seconds=refresh_seconds,
        resolutions=_next_resolutions(state, items),
        provider_state=next_provider_state,
        campaign=campaign,
    )
    if in_slot:
        # A silently materialized first snapshot may be unchanged at the first
        # real slot. Publishing only enqueues changed snapshots, so explicitly
        # and idempotently ensure the scheduled row exists for this slot.
        descriptor = _campaign_descriptor(campaign_key, now)
        campaign_id = await db.enqueue_music_campaign(
            dedupe_key=descriptor["dedupe_key"],
            collection_key=campaign_key,
            generation=int(result["generation"]),
            kind=descriptor["kind"],
            header_key=descriptor["header_key"],
            items=items,
            eligible_at=int(descriptor["eligible_at"]),
            expires_at=int(descriptor["expires_at"]),
            priority=int(descriptor["priority"]),
            now=now,
        )
        result = {**dict(result), "campaign_id": campaign_id}
    return {**dict(result), "bootstrap": bootstrap}


async def refresh_apple_collections_once(
    db: Any,
    *,
    now: int | None = None,
    fetcher: Callable[[], Awaitable[dict[str, list[dict[str, Any]]]]] = (
        fetch_apple_market_charts
    ),
    searcher: Callable[[str, int], Awaitable[list[SearchItem]]] = search_tracks,
    retry_base_seconds: int = 30 * 60,
    force_empty: bool = False,
) -> dict[str, Any]:
    """Capture chart history and independently refresh due editorial lists."""
    timestamp = int(time.time()) if now is None else int(now)
    keys = (CAMPAIGN_NEW_MUSIC, CAMPAIGN_RISING, CAMPAIGN_DISCOVERIES)
    states: dict[str, dict[str, Any]] = {}
    for key in keys:
        await db.ensure_music_collection(key, EDITORIAL_CAMPAIGN_SIZE)
        states[key] = await db.get_music_collection_state(key)

    latest_snapshot = await db.get_latest_music_chart_snapshot(
        APPLE_CHART_HISTORY_KEY
    )
    latest_captured = (
        int(latest_snapshot.get("captured_at") or 0)
        if isinstance(latest_snapshot, Mapping)
        else 0
    )
    any_empty_due = any(
        len(state.get("items") or []) != EDITORIAL_CAMPAIGN_SIZE
        and (
            force_empty
            or int(state.get("next_refresh_at") or 0) <= timestamp
        )
        for state in states.values()
    )
    any_slot_due = any(
        int(state.get("next_refresh_at") or 0) <= timestamp
        and _in_campaign_slot(key, timestamp)
        for key, state in states.items()
    )
    history_due = latest_captured <= timestamp - 24 * 60 * 60
    if not (any_empty_due or any_slot_due or history_due):
        return {"fetched": False, "collections": {}}

    charts = await fetcher()
    if "uz" not in charts or len(charts["uz"]) < 10:
        raise ValueError("Apple source did not return a complete UZ chart")
    await db.record_music_chart_snapshot(
        APPLE_CHART_HISTORY_KEY,
        list(charts["uz"]),
        captured_at=timestamp,
        keep=32,
    )
    baseline_snapshot = await db.get_music_chart_snapshot_before(
        APPLE_CHART_HISTORY_KEY,
        timestamp - APPLE_RISING_BASELINE_SECONDS,
    )
    baseline_uz = _snapshot_items(baseline_snapshot)

    outcomes: dict[str, Any] = {}
    for key in keys:
        state = await db.get_music_collection_state(key)
        empty = len(state.get("items") or []) != EDITORIAL_CAMPAIGN_SIZE
        # Once materialized, collections refresh only in their fixed weekly
        # slot. Empty snapshots are allowed to initialize silently at boot.
        if not empty and not _in_campaign_slot(key, timestamp):
            outcomes[key] = {"status": "waiting_for_slot"}
            continue
        claim_token = await db.claim_music_collection_refresh(
            key,
            timestamp,
            lease_seconds=REFRESH_LEASE_SECONDS,
            force=force_empty and empty,
        )
        if claim_token is None:
            outcomes[key] = {"status": "not_due"}
            if force_empty and empty:
                outcomes[key]["repair_pending"] = True
            continue
        try:
            result = await _publish_apple_collection(
                db,
                campaign_key=key,
                state=state,
                claim_token=claim_token,
                charts=charts,
                baseline_uz=baseline_uz,
                now=timestamp,
                searcher=searcher,
            )
            outcomes[key] = {"status": "published", **dict(result)}
        except asyncio.CancelledError:
            await db.release_music_collection_refresh(key, claim_token)
            raise
        except (LookupError, NoMeaningfulRising) as exc:
            if isinstance(exc, LookupError):
                next_refresh_at = timestamp + 24 * 60 * 60
                status = "baseline_pending"
            else:
                after_window = timestamp + CAMPAIGN_SLOT_WINDOW_SECONDS
                next_refresh_at = next_campaign_slot(key, after_window)
                status = "no_meaningful_movers"
            await db.defer_music_collection_refresh(
                key,
                claim_token,
                next_refresh_at=next_refresh_at,
            )
            logger.info(
                "Rising refresh deferred status=%s next_refresh_at=%s",
                status,
                next_refresh_at,
            )
            outcomes[key] = {
                "status": status,
                "next_refresh_at": next_refresh_at,
            }
        except Exception as exc:
            retry_at = await db.fail_music_collection_refresh(
                key,
                claim_token,
                now=timestamp,
                base_seconds=retry_base_seconds,
            )
            logger.warning(
                "Music collection refresh failed key=%s error=%s retry_at=%s",
                key,
                type(exc).__name__,
                retry_at,
            )
            outcomes[key] = {
                "status": "failed",
                "error": type(exc).__name__,
                "retry_at": retry_at,
            }
    return {"fetched": True, "collections": outcomes}


async def refresh_mood_collection_once(
    db: Any,
    mood: str,
    *,
    now: int | None = None,
    reserved_source_ids: Iterable[str] = (),
    searcher: Callable[[str, int], Awaitable[list[SearchItem]]] = search_tracks,
    retry_base_seconds: int = 30 * 60,
    force_empty: bool = False,
) -> dict[str, Any]:
    """Refresh one mood atomically; source failures leave its old ten intact."""
    slug = str(mood or "").strip().lower()
    key = mood_campaign_key(slug)
    timestamp = int(time.time()) if now is None else int(now)
    await db.ensure_music_collection(key, MOOD_CAMPAIGN_SIZE)
    state = await db.get_music_collection_state(key)
    empty = len(state.get("items") or []) != MOOD_CAMPAIGN_SIZE
    claim_token = await db.claim_music_collection_refresh(
        key,
        timestamp,
        lease_seconds=REFRESH_LEASE_SECONDS,
        force=force_empty and empty,
    )
    if claim_token is None:
        result = {"status": "not_due"}
        if force_empty and empty:
            result["repair_pending"] = True
        return result
    try:
        items = await build_mood_collection(
            slug,
            count=MOOD_CAMPAIGN_SIZE,
            recently_used=_recent_source_ids(state),
            reserved_source_ids=reserved_source_ids,
            searcher=searcher,
        )
        result = await db.publish_music_collection(
            key,
            items,
            now=timestamp,
            claim_token=claim_token,
            refresh_seconds=WEEK_SECONDS,
            resolutions=_next_resolutions(state, items),
            provider_state=_next_provider_state(state, items, now=timestamp),
            campaign=None,
        )
        return {"status": "published", **dict(result)}
    except asyncio.CancelledError:
        await db.release_music_collection_refresh(key, claim_token)
        raise
    except Exception as exc:
        retry_at = await db.fail_music_collection_refresh(
            key,
            claim_token,
            now=timestamp,
            base_seconds=retry_base_seconds,
        )
        logger.warning(
            "Mood refresh failed mood=%s error=%s retry_at=%s",
            slug,
            type(exc).__name__,
            retry_at,
        )
        return {
            "status": "failed",
            "error": type(exc).__name__,
            "retry_at": retry_at,
        }


async def refresh_mood_collections_once(
    db: Any,
    *,
    now: int | None = None,
    searcher: Callable[[str, int], Awaitable[list[SearchItem]]] = search_tracks,
    retry_base_seconds: int = 30 * 60,
    force_empty: bool = False,
) -> dict[str, dict[str, Any]]:
    """Refresh all five moods sequentially with failure isolation."""
    timestamp = int(time.time()) if now is None else int(now)
    existing: dict[str, set[str]] = {}
    for slug in MOOD_SLUGS:
        key = mood_campaign_key(slug)
        await db.ensure_music_collection(key, MOOD_CAMPAIGN_SIZE)
        state = await db.get_music_collection_state(key)
        existing[slug] = {
            _text(item.get("source_id"))
            for item in state.get("items") or []
            if isinstance(item, Mapping) and _text(item.get("source_id"))
        }

    outcomes: dict[str, dict[str, Any]] = {}
    selected_this_run: dict[str, set[str]] = {}
    for slug in MOOD_SLUGS:
        reserved: set[str] = set()
        for other_slug, source_ids in existing.items():
            if other_slug != slug:
                reserved.update(source_ids)
        for source_ids in selected_this_run.values():
            reserved.update(source_ids)
        result = await refresh_mood_collection_once(
            db,
            slug,
            now=timestamp,
            reserved_source_ids=reserved,
            searcher=searcher,
            retry_base_seconds=retry_base_seconds,
            force_empty=force_empty,
        )
        outcomes[slug] = result
        if result.get("status") == "published":
            state = await db.get_music_collection_state(mood_campaign_key(slug))
            selected_this_run[slug] = {
                _text(item.get("source_id"))
                for item in state.get("items") or []
                if isinstance(item, Mapping) and _text(item.get("source_id"))
            }
    return outcomes


def tashkent_week_key(now: int | float) -> str:
    """Return a stable ISO week key in the campaign scheduling timezone."""
    local = datetime.fromtimestamp(int(now), TASHKENT_TZ)
    iso_year, iso_week, _ = local.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


async def deliver_next_music_campaign(
    bot: Any,
    db: Any,
    config: Any,
    *,
    now: int | None = None,
) -> bool:
    """Claim and durably deliver one locale-neutral scheduled campaign."""
    timestamp = int(time.time()) if now is None else int(now)
    row = await db.claim_next_music_campaign(
        now=timestamp,
        week_key=tashkent_week_key(timestamp),
        max_per_week=3,
        lease_seconds=120,
    )
    if row is None:
        return False

    campaign_id = int(row["campaign_id"])
    runner_token = _text(row.get("runner_token"))
    payload = row.get("payload")
    kind = _text(row.get("kind"))
    expected_header = CAMPAIGN_HEADER_KEYS.get(kind)
    try:
        if (
            not runner_token
            or not isinstance(payload, Mapping)
            or kind not in CAMPAIGN_HEADER_KEYS
            or payload.get("header_key") != expected_header
            or not isinstance(payload.get("items"), list)
            or len(payload["items"]) != EDITORIAL_CAMPAIGN_SIZE
        ):
            await db.fail_music_campaign(campaign_id, runner_token)
            logger.error("Rejected invalid scheduled music campaign id=%s", campaign_id)
            return True

        # Imports stay lazy so source-only tests do not construct aiogram
        # routers, and so the handler can reuse the exact interactive markup.
        from bot.handlers.music_campaigns import campaign_list_keyboard
        from bot.i18n import t
        from bot.services.broadcast import broadcast_active

        items = payload["items"]
        header_key = str(payload["header_key"])
        include_moods = kind in {CAMPAIGN_NEW_MUSIC, CAMPAIGN_DISCOVERIES}

        async def send_one(user_id: int):
            locale = await db.get_locale(user_id) or config.default_locale

            def translate(key: str, **kwargs: Any) -> str:
                return t(key, locale, **kwargs)

            return await bot.send_message(
                chat_id=user_id,
                text=translate(header_key),
                reply_markup=campaign_list_keyboard(
                    items,
                    translate,
                    include_moods=include_moods,
                ),
            )

        async def checkpoint(user_id: int, outcome: str) -> None:
            advanced = await db.checkpoint_music_campaign(
                campaign_id,
                runner_token,
                user_id,
                outcome,
                lease_seconds=120,
            )
            if not advanced:
                raise RuntimeError("scheduled music campaign lease was lost")

        stats = await broadcast_active(
            bot=bot,
            db=db,
            config=config,
            send_one=send_one,
            after_user_id=int(row.get("broadcast_cursor") or 0),
            through_user_id=int(row.get("audience_upper_user_id") or 0),
            exclude_user_id=None,
            on_outcome=checkpoint,
        )
        if not await db.complete_music_campaign(campaign_id, runner_token):
            raise RuntimeError("scheduled music campaign completion lease was lost")
        logger.info(
            "Music campaign completed id=%s kind=%s sent=%s inactive=%s failed=%s",
            campaign_id,
            kind,
            stats.sent,
            stats.inactive,
            stats.failed,
        )
        return True
    except asyncio.CancelledError:
        if runner_token:
            await db.release_music_campaign(campaign_id, runner_token)
        raise
    except Exception:
        if runner_token:
            await db.release_music_campaign(campaign_id, runner_token)
        raise


async def run_music_campaign_scheduler(bot: Any, db: Any, config: Any) -> None:
    """Maintain weekly snapshots and resume at most three fan-outs."""
    logger.info(
        "Music campaign scheduler started timezone=Asia/Tashkent "
        "slots=mon09,wed18,fri18 moods=weekly"
    )
    source_retry_at = 0
    # A new release gets one immediate repair pass for snapshots left empty by
    # older source rules. Terminal success/failure restores persisted backoff;
    # an active stale lease keeps the one-shot pending until it can be claimed.
    repair_empty_editorial = True
    repair_empty_moods = True
    while True:
        try:
            # Delivery wins over provider work, including after a restart.
            if await deliver_next_music_campaign(bot, db, config):
                continue

            timestamp = int(time.time())
            if timestamp >= source_retry_at:
                try:
                    editorial_outcomes = await refresh_apple_collections_once(
                        db,
                        now=timestamp,
                        retry_base_seconds=30 * 60,
                        force_empty=repair_empty_editorial,
                    )
                    source_retry_at = 0
                    repair_empty_editorial = any(
                        bool(result.get("repair_pending"))
                        for result in editorial_outcomes.get(
                            "collections", {}
                        ).values()
                        if isinstance(result, Mapping)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    source_retry_at = timestamp + 30 * 60
                    logger.warning(
                        "Apple campaign source cycle failed error=%s retry_at=%s",
                        type(exc).__name__,
                        source_retry_at,
                    )

            # Each mood owns its lease/backoff and last-good state, so one bad
            # query cannot invalidate the other four collections.
            mood_outcomes = await refresh_mood_collections_once(
                db,
                now=timestamp,
                retry_base_seconds=30 * 60,
                force_empty=repair_empty_moods,
            )
            repair_empty_moods = any(
                bool(result.get("repair_pending"))
                for result in mood_outcomes.values()
                if isinstance(result, Mapping)
            )

            if await deliver_next_music_campaign(bot, db, config):
                continue
            # Fifteen minutes keeps slot delivery prompt without polling Apple
            # or YouTube; their persisted due timestamps prevent provider work.
            await asyncio.sleep(15 * 60)
        except asyncio.CancelledError:
            logger.info("Music campaign scheduler stopped")
            raise
        except Exception as exc:
            logger.error(
                "Music campaign scheduler cycle failed error=%s",
                type(exc).__name__,
            )
            await asyncio.sleep(60)


async def load_music_campaign(db: Any, campaign_key: str) -> list[SearchItem]:
    """Load a complete stored collection without any source/provider work."""
    key = str(campaign_key or "").strip().lower()
    if key not in MUSIC_CAMPAIGN_KEYS:
        raise ValueError(f"unsupported music campaign: {campaign_key!r}")
    state = await db.get_music_collection_state(key)
    items = state.get("items") if isinstance(state, Mapping) else None
    if not isinstance(items, list):
        return []
    expected = MOOD_CAMPAIGN_SIZE if key.startswith("mood:") else EDITORIAL_CAMPAIGN_SIZE
    if len(items) != expected:
        return []
    loaded: list[SearchItem] = []
    for item in items:
        if not isinstance(item, Mapping):
            return []
        video_id = _text(item.get("video_id"))
        title = _text(item.get("title"))
        if _YOUTUBE_VIDEO_ID.fullmatch(video_id) is None or not title:
            return []
        duration = item.get("duration")
        loaded.append(SearchItem(
            video_id=video_id,
            title=title,
            duration=int(duration) if duration is not None else None,
            uploader=_text(item.get("uploader")),
        ))
    return loaded
