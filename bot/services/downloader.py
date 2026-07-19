"""yt-dlp wrapper — Feature D (IG / TikTok / YouTube) with a quality selector.

Flow: `extract_meta()` first (no download) to build the preview + buttons, then
`download_video_quality()` / `download_audio()` when the user picks a tier.
`download_audio_by_query()` (ytsearch) is used by Feature A/B later.
Blocking yt-dlp calls run in a thread so the async event loop stays responsive.
"""
import asyncio
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import yt_dlp

SUPPORTED_HOSTS = (
    "instagram.com", "instagr.am", "cdninstagram.com",
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "youtube.com", "youtu.be", "m.youtube.com",
    "facebook.com", "fb.watch",
    "twitter.com", "x.com",
)


@dataclass
class DownloadResult:
    path: str
    title: str
    uploader: str
    duration: float | None
    ext: str


@dataclass
class MediaMeta:
    url: str
    title: str
    uploader: str
    duration: float | None
    thumbnail: str | None
    heights: list[int]


def _net_opts() -> dict:
    """Optional yt-dlp network options from env — essential on cloud/datacenter
    hosts (Railway etc.) where YouTube blocks bare requests.
      YTDLP_COOKIES_FILE : path to a Netscape cookies.txt (export from a browser)
      YTDLP_PROXY        : proxy URL (residential recommended)
    """
    opts: dict = {}
    cookies = os.getenv("YTDLP_COOKIES_FILE")
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    proxy = os.getenv("YTDLP_PROXY")
    if proxy:
        opts["proxy"] = proxy
    return opts


def is_supported_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in SUPPORTED_HOSTS)


def _first_entry(info: dict) -> dict:
    if info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if entries:
            return entries[0]
    return info


def _final_path(info: dict, ydl: "yt_dlp.YoutubeDL") -> str:
    """Path of the file actually written after any post-processing/merge."""
    downloads = info.get("requested_downloads")
    if downloads and downloads[0].get("filepath"):
        return downloads[0]["filepath"]
    return ydl.prepare_filename(info)


# ── metadata only (no download) ──────────────────────────
async def extract_meta(url: str) -> MediaMeta:
    return await asyncio.to_thread(_extract_meta_sync, url)


def _extract_meta_sync(url: str) -> MediaMeta:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    with yt_dlp.YoutubeDL({**opts, **_net_opts()}) as ydl:
        info = _first_entry(ydl.extract_info(url, download=False))
    heights = sorted(
        {int(f["height"]) for f in (info.get("formats") or []) if f.get("height")}
    )
    return MediaMeta(
        url=info.get("webpage_url") or url,
        title=info.get("title") or "",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        heights=heights,
    )


# ── download a specific video quality ────────────────────
async def download_video_quality(url: str, out_dir: str, max_height: int) -> DownloadResult:
    return await asyncio.to_thread(_download_quality_sync, url, out_dir, max_height)


def _download_quality_sync(url: str, out_dir: str, max_height: int) -> DownloadResult:
    os.makedirs(out_dir, exist_ok=True)
    opts = {
        "outtmpl": os.path.join(out_dir, f"%(id)s_{max_height}p.%(ext)s"),
        "format": (
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"best[height<={max_height}]/best"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    with yt_dlp.YoutubeDL({**opts, **_net_opts()}) as ydl:
        info = _first_entry(ydl.extract_info(url, download=True))
        path = _final_path(info, ydl)
    return DownloadResult(
        path=path,
        title=info.get("title") or "",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=info.get("duration"),
        ext=os.path.splitext(path)[1].lstrip("."),
    )


# ── download audio (from a URL, or via YouTube search) ───
async def download_audio(url: str, out_dir: str) -> DownloadResult:
    return await asyncio.to_thread(_download_audio_sync, url, out_dir)


async def download_audio_by_query(query: str, out_dir: str) -> DownloadResult:
    """Search YouTube for `query`, download best audio as mp3. Feature A/B."""
    return await asyncio.to_thread(_download_audio_sync, f"ytsearch1:{query}", out_dir)


def _download_audio_sync(target: str, out_dir: str) -> DownloadResult:
    os.makedirs(out_dir, exist_ok=True)
    opts = {
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    with yt_dlp.YoutubeDL({**opts, **_net_opts()}) as ydl:
        info = _first_entry(ydl.extract_info(target, download=True))
        path = _final_path(info, ydl)
    if not path.endswith(".mp3"):
        path = os.path.splitext(path)[0] + ".mp3"
    return DownloadResult(
        path=path,
        title=info.get("title") or "",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=info.get("duration"),
        ext="mp3",
    )
