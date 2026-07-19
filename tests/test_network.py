"""Real-internet smoke tests. Run with:  pytest -m network
Skipped by default in the fast suite via `pytest -m 'not network'`.
"""
import os

import pytest

from bot.services import audio as audio_svc
from bot.services import downloader
from bot.services.lyrics import fetch_lyrics
from bot.services.recognizer import ShazamioRecognizer
from bot.services.search import search_tracks

pytestmark = pytest.mark.network


async def test_search_real_returns_tracks():
    items = await search_tracks("imagine dragons believer", limit=10)
    assert len(items) >= 5
    assert all(it.video_id and it.title for it in items)
    assert any(it.duration for it in items)


async def test_extract_meta_real_has_heights():
    meta = await downloader.extract_meta("https://www.youtube.com/watch?v=5bEChjylcEQ")
    assert meta.title
    assert any(h >= 360 for h in meta.heights)


async def test_download_audio_real(tmp_path):
    items = await search_tracks("ummon xiyonat", limit=5)
    last_err = None
    for it in items[:4]:  # try a few; YouTube may 403 a specific video
        try:
            res = await downloader.download_audio(it.url, str(tmp_path))
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        assert res.path.endswith(".mp3")
        size_mb = os.path.getsize(res.path) / (1024 * 1024)
        assert 0.1 < size_mb < 50
        return
    pytest.skip(f"YouTube rate-limited/blocked all candidates: {last_err}")


async def test_recognize_real(tmp_path):
    res = await downloader.download_audio_by_query("PSY Gangnam Style official", str(tmp_path))
    sample = await audio_svc.make_sample(res.path, str(tmp_path), seconds=12)
    track = await ShazamioRecognizer().recognize(sample)
    assert track is not None
    assert "gangnam" in track.title.lower()


async def test_lyrics_real():
    text = await fetch_lyrics("Imagine Dragons", "Believer")
    assert text and "believer" in text.lower()
