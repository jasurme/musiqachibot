"""Music recognition — swappable provider interface (Feature C).

Default provider is Shazamio (free, unofficial). AudD can be dropped in later
by implementing the same `Recognizer` protocol; handlers won't change.
"""
from dataclasses import dataclass
from typing import Protocol


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
        out = await shazam.recognize(audio_path)
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


def get_recognizer(config) -> Recognizer:
    # Only Shazamio for now; branch on config.audd_token here when AudD is added.
    return ShazamioRecognizer()
