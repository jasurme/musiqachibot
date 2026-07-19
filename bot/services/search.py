"""Feature A — find songs by name / artist via YouTube search (yt-dlp).

Flat search returns titles + durations + video ids fast (no per-video calls).
The video id is kept so a pick downloads that *exact* track — no re-search.
Feature B (lyrics) will resolve a lyric snippet to a query, then reuse this.
"""
import asyncio
import re
from dataclasses import dataclass

import yt_dlp

from bot.services.downloader import _net_opts

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


async def search_tracks(query: str, limit: int = 30) -> list[SearchItem]:
    return await asyncio.to_thread(_search_sync, query, limit)


def _search_sync(query: str, limit: int) -> list[SearchItem]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL({**opts, **_net_opts()}) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    items: list[SearchItem] = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        vid = entry.get("id")
        if not vid:
            continue
        dur = entry.get("duration")
        items.append(
            SearchItem(
                video_id=vid,
                title=_clean_title(entry.get("title")),
                duration=int(dur) if dur else None,
                uploader=entry.get("channel") or entry.get("uploader") or "",
            )
        )
    return items
