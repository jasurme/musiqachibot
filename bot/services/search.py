"""Feature A — find songs by name / artist via YouTube search (yt-dlp).

Flat search returns titles + durations + video ids fast (no per-video calls).
The video id is kept so a pick downloads that *exact* track — no re-search.
Recognized tracks reuse the same search path before offering download buttons.
"""
import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

import yt_dlp

from bot.services.downloader import (
    _base_opts,
    _run_provider_process,
    provider_error_key,
    record_provider_failure,
)

_YOUTUBE_PROVIDER_URL = "https://www.youtube.com/"
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default

# Skip search hits longer than this — full albums / compilations / long live sets
# blow past Telegram's 50 MB upload cap. ~20 min keeps even long songs/remixes.
MAX_TRACK_SECONDS = _env_int("SEARCH_MAX_SECONDS", 1200)
SEARCH_CACHE_SECONDS = _env_int("SEARCH_CACHE_SECONDS", 3600)
_CACHE_MAX = 256
_CACHE: "OrderedDict[tuple[str, int], tuple[float, list[SearchItem]]]" = OrderedDict()
_INFLIGHT: dict[tuple[str, int], asyncio.Task] = {}

# strip a trailing "(Official Video)/(AUDIO)/[HD]..." style tag
_DROP = re.compile(
    r"\s*[\(\[][^)\]]*\b(?:official|audio|video|lyric|lyrics|clip|klip|"
    r"premyera|premiere|hd|4k|mv|karaoke|cover)\b[^)\]]*[\)\]]\s*$",
    re.I,
)


@dataclass
class SearchItem:
    video_id: str
    title: str
    duration: int | None
    uploader: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def _clean_title(title: str | None) -> str:
    t = (title or "").strip()
    if " | " in t:  # drop the duplicated Cyrillic/latin half
        t = t.split(" | ", 1)[0].strip()
    prev = None
    while prev != t:  # peel nested tags like "... (Official) (HD)"
        prev = t
        t = _DROP.sub("", t).strip()
    return t


async def search_tracks(query: str, limit: int = 5) -> list[SearchItem]:
    started = time.perf_counter()
    normalized = " ".join((query or "").split()).casefold()
    limit = max(1, min(int(limit), 30))
    key = (normalized, limit)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and cached[0] > now:
        _CACHE.move_to_end(key)
        items = list(cached[1])
        logger.info(
            "music search cache_hit=true shared_inflight=false results=%s duration_ms=%s",
            len(items), round((time.perf_counter() - started) * 1000),
        )
        return items
    if cached:
        _CACHE.pop(key, None)

    blocked_key = provider_error_key(_YOUTUBE_PROVIDER_URL)
    if blocked_key == "download_blocked":
        raise yt_dlp.utils.DownloadError(
            "YouTube provider is bot-checked; authentication or egress is required"
        )
    if blocked_key == "download_rate_limited":
        raise yt_dlp.utils.DownloadError("YouTube provider is rate limited (HTTP 429)")

    task = _INFLIGHT.get(key)
    shared_inflight = task is not None
    if task is None:
        task = asyncio.create_task(_run_search_worker(query, limit))
        _INFLIGHT[key] = task
        task.add_done_callback(
            lambda done, cache_key=key: (
                _INFLIGHT.pop(cache_key, None)
                if _INFLIGHT.get(cache_key) is done else None
            )
        )
    try:
        items = await asyncio.shield(task)
    finally:
        if task.done() and _INFLIGHT.get(key) is task:
            _INFLIGHT.pop(key, None)

    _CACHE[key] = (time.monotonic() + max(0, SEARCH_CACHE_SECONDS), list(items))
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    logger.info(
        "music search cache_hit=false shared_inflight=%s results=%s duration_ms=%s",
        str(shared_inflight).lower(), len(items),
        round((time.perf_counter() - started) * 1000),
    )
    return list(items)


def clear_search_cache() -> None:
    _CACHE.clear()
    _INFLIGHT.clear()


async def _run_search_worker(query: str, limit: int) -> list[SearchItem]:
    try:
        payload = await _run_provider_process("search", query, limit)
    except Exception as exc:
        record_provider_failure(_YOUTUBE_PROVIDER_URL, exc)
        raise
    if not isinstance(payload, list):
        raise RuntimeError("provider worker returned invalid search results")
    try:
        return [SearchItem(**item) for item in payload if isinstance(item, dict)]
    except TypeError as exc:
        raise RuntimeError("provider worker returned invalid search results") from exc


def _entries_to_items(entries, max_seconds: int = MAX_TRACK_SECONDS) -> list[SearchItem]:
    items: list[SearchItem] = []
    for entry in entries or []:
        if not entry:
            continue
        vid = entry.get("id")
        if not vid:
            continue
        dur = entry.get("duration")
        if dur and max_seconds and dur > max_seconds:
            continue  # album / long compilation → would exceed the 50 MB upload cap
        items.append(
            SearchItem(
                video_id=vid,
                title=_clean_title(entry.get("title")),
                duration=int(dur) if dur else None,
                uploader=entry.get("channel") or entry.get("uploader") or "",
            )
        )
    return items


def _search_sync(query: str, limit: int) -> list[SearchItem]:
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 30))
    opts = {**_base_opts(), "extract_flat": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    return _entries_to_items(info.get("entries"))
