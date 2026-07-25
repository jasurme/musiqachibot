"""Music recognition — swappable provider interface (Feature C).

Default provider is Shazamio (free, unofficial). When AUDD_TOKEN is configured,
the official AudD REST API is used as a paid fallback on failure/no-match.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Protocol

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class Track:
    title: str
    artist: str
    url: str | None = None
    cover: str | None = None

    @property
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()


class Recognizer(Protocol):
    async def recognize(self, audio_path: str) -> Track | None:
        ...


class ShazamioRecognizer:
    """Wraps shazamio. Imported lazily so the skeleton runs even before
    the recognition step is wired up."""

    async def recognize(self, audio_path: str) -> Track | None:
        from shazamio import Shazam  # lazy import

        shazam = Shazam()
        out = await asyncio.wait_for(shazam.recognize(audio_path), timeout=25)
        track = (out or {}).get("track")
        if not track:
            return None
        images = track.get("images") or {}
        return Track(
            title=track.get("title", ""),
            artist=track.get("subtitle", ""),
            url=track.get("url"),
            cover=images.get("coverarthq") or images.get("coverart"),
        )


class AudDRecognizer:
    """Official AudD REST fallback, used only when AUDD_TOKEN is configured."""

    endpoint = "https://api.audd.io/"

    def __init__(self, token: str):
        self.token = token

    async def recognize(self, audio_path: str) -> Track | None:
        timeout = aiohttp.ClientTimeout(total=25)
        form = aiohttp.FormData()
        form.add_field("api_token", self.token)
        form.add_field("return", "apple_music,spotify")
        with open(audio_path, "rb") as audio:
            form.add_field(
                "file", audio, filename=os.path.basename(audio_path),
                content_type="audio/mpeg",
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.endpoint, data=form) as response:
                    payload = await response.json(content_type=None)
                    if response.status >= 400:
                        raise RuntimeError(f"AudD HTTP {response.status}")

        if not isinstance(payload, dict):
            raise RuntimeError("AudD returned an invalid response")
        if payload.get("status") != "success":
            error = payload.get("error") or {}
            message = error.get("error_message") if isinstance(error, dict) else None
            raise RuntimeError(message or "AudD recognition failed")
        result = payload.get("result")
        if not result:
            return None

        spotify = result.get("spotify") or {}
        apple = result.get("apple_music") or {}
        spotify_album = spotify.get("album") or {}
        images = spotify_album.get("images") or []
        cover = images[0].get("url") if images and isinstance(images[0], dict) else None
        if not cover:
            artwork = (apple.get("artwork") or {}).get("url")
            if artwork:
                cover = artwork.replace("{w}", "600").replace("{h}", "600")
        listen_url = result.get("song_link")
        if not listen_url:
            listen_url = (spotify.get("external_urls") or {}).get("spotify")
        return Track(
            title=result.get("title") or "",
            artist=result.get("artist") or "",
            url=listen_url,
            cover=cover,
        )


class FallbackRecognizer:
    def __init__(self, providers: list[Recognizer]):
        self.providers = providers

    async def recognize(self, audio_path: str) -> Track | None:
        last_error: Exception | None = None
        had_clean_no_match = False
        for provider in self.providers:
            try:
                track = await provider.recognize(audio_path)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Recognition provider %s failed: %s",
                    type(provider).__name__, type(exc).__name__,
                )
                continue
            if track:
                return track
            had_clean_no_match = True
        if last_error is not None and not had_clean_no_match:
            raise last_error
        return None


def get_recognizer(config) -> Recognizer:
    providers: list[Recognizer] = [ShazamioRecognizer()]
    if config.audd_token:
        # Keep the free provider first; spend an AudD request only on no-match or
        # provider failure, improving production reliability without doubling cost.
        providers.append(AudDRecognizer(config.audd_token))
    return providers[0] if len(providers) == 1 else FallbackRecognizer(providers)
