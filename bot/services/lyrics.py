"""Feature B — lyrics lookup via lyrics.ovh (free, no API key).

Coverage is mostly Western/English; returns None when not found (e.g. many
Uzbek tracks). Swap in Genius here later for wider coverage if needed.
"""
import logging
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)


async def fetch_lyrics(artist: str, title: str) -> str | None:
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist or not title:
        return None
    url = f"https://api.lyrics.ovh/v1/{quote(artist, safe='')}/{quote(title, safe='')}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("Lyrics lookup failed: %s", type(exc).__name__)
        return None
    lyrics = (data or {}).get("lyrics")
    return lyrics.strip() if lyrics and lyrics.strip() else None
