"""Unit tests for pure helpers — no bot, no network."""
import os
import subprocess

import pytest

from bot.db.storage import Storage
from bot.i18n import SUPPORTED, TRANSLATIONS, t
from bot.services import downloader
from bot.services.search import SearchItem, _clean_title
from bot.services.video import make_video_note


# ── i18n ─────────────────────────────────────────────────
def test_all_locales_have_same_keys():
    base = set(TRANSLATIONS["en"])
    for loc in SUPPORTED:
        assert set(TRANSLATIONS[loc]) == base, f"{loc} key mismatch"


def test_t_falls_back_to_default_locale():
    assert t("welcome", "zz").startswith("👋")  # unknown locale → uz default


def test_t_formats_placeholders():
    out = t("rec_header", "en", title="Believer", artist="Imagine Dragons")
    assert "Believer" in out and "Imagine Dragons" in out


def test_t_missing_kwarg_does_not_crash():
    # rec_header needs title+artist; omitting them must not raise
    assert isinstance(t("rec_header", "en"), str)


# ── URL support detection ────────────────────────────────
@pytest.mark.parametrize("url", [
    "https://www.tiktok.com/@x/video/1",
    "https://youtu.be/abc",
    "https://www.youtube.com/watch?v=abc",
    "https://instagram.com/reel/x/",
    "https://vm.tiktok.com/xyz",
])
def test_supported_urls(url):
    assert downloader.is_supported_url(url)


@pytest.mark.parametrize("url", [
    "https://example.com/x",
    "https://notyoutube.com/watch",
    "ftp://youtube.com",
    "not a url",
])
def test_unsupported_urls(url):
    assert not downloader.is_supported_url(url)


# ── title cleaning ───────────────────────────────────────
@pytest.mark.parametrize("raw,clean", [
    ("Ummon - Xiyonat | Уммон - Хиёнат (AUDIO)", "Ummon - Xiyonat"),
    ("Ummon - Qanday unutding | Уммон", "Ummon - Qanday unutding"),
    ("Artist - Song (Official Music Video)", "Artist - Song"),
    ("Artist - Song [HD]", "Artist - Song"),
    ("Plain Title", "Plain Title"),
    ("Song (Remix)", "Song (Remix)"),  # remix kept — not a junk tag
])
def test_clean_title(raw, clean):
    assert _clean_title(raw) == clean


def test_search_item_url():
    it = SearchItem(video_id="abc123", title="t", duration=100, uploader="u")
    assert it.url == "https://www.youtube.com/watch?v=abc123"


def test_cookies_content_materialized(monkeypatch):
    from bot.main import _materialize_cookies
    from bot.services.downloader import _net_opts
    monkeypatch.delenv("YTDLP_COOKIES_FILE", raising=False)
    monkeypatch.setenv("YTDLP_COOKIES_CONTENT", "# Netscape HTTP Cookie File\n")
    try:
        _materialize_cookies()
        path = os.environ.get("YTDLP_COOKIES_FILE")
        assert path and os.path.exists(path)
        assert _net_opts().get("cookiefile") == path  # picked up by yt-dlp opts
    finally:
        leaked = os.environ.pop("YTDLP_COOKIES_FILE", None)
        if leaked and os.path.exists(leaked):
            os.remove(leaked)


# ── round video-note conversion (real ffmpeg, no network) ─
async def test_make_video_note_is_square(tmp_path):
    src = str(tmp_path / "src.mp4")
    # synthesize a 2s NON-square clip locally with ffmpeg
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=15:duration=2",
         "-pix_fmt", "yuv420p", src],
        check=True, capture_output=True,
    )
    note = await make_video_note(src, str(tmp_path), size=480)
    assert os.path.exists(note) and os.path.getsize(note) > 1000
    dims = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", note],
        capture_output=True, text=True,
    ).stdout.strip()
    assert dims.replace(" ", "") == "480,480"  # cropped to a centred square


# ── storage ──────────────────────────────────────────────
async def test_storage_locale_roundtrip():
    s = Storage(":memory:")
    await s.init()
    assert await s.get_locale(1) is None
    await s.set_locale(1, "ru")
    assert await s.get_locale(1) == "ru"
    await s.set_locale(1, "en")  # upsert
    assert await s.get_locale(1) == "en"
    await s.close()


async def test_storage_audio_cache_roundtrip():
    s = Storage(":memory:")
    await s.init()
    assert await s.get_cached_audio("k") is None
    await s.set_cached_audio("k", "FILEID", "Some Title")
    assert await s.get_cached_audio("k") == "FILEID"
    await s.set_cached_audio("k", "FILEID2")  # replace
    assert await s.get_cached_audio("k") == "FILEID2"
    await s.close()


# ── quality keyboard (Feature D) ─────────────────────────
_ = lambda k, **kw: k  # noqa: E731 — stub translator for keyboard tests


def test_quality_keyboard_tiers_audio_and_find_music():
    from bot.handlers.url_download import _quality_keyboard
    kb = _quality_keyboard("tok", [144, 360, 480, 720, 1080], _)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "dl:tok:360" in datas
    assert "dl:tok:720" in datas
    assert "dl:tok:1080" in datas
    assert "dl:tok:audio" in datas
    assert "dl:tok:music" in datas  # the "Find music" button
    assert "dl:tok:round" in datas  # the "Yumaloq video" button


def test_quality_keyboard_caps_to_available_height():
    from bot.handlers.url_download import _quality_keyboard
    kb = _quality_keyboard("tok", [240, 360], _)  # max 360
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "dl:tok:360" in datas
    assert "dl:tok:720" not in datas  # not offered above source resolution
    assert "dl:tok:audio" in datas and "dl:tok:music" in datas


def test_quality_keyboard_no_heights_shows_all():
    from bot.handlers.url_download import _quality_keyboard
    kb = _quality_keyboard("tok", [], _)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "dl:tok:360" in datas and "dl:tok:audio" in datas and "dl:tok:music" in datas


# ── results list rendering (shared A/C) ──────────────────
def _items(n):
    return [SearchItem(video_id=f"v{i}", title=f"Song {i}", duration=180 + i, uploader="ch")
            for i in range(n)]


def test_results_list_text_numbers_and_durations():
    from bot.handlers.results import _list_text
    sess = {"header": "<b>q</b>", "items": _items(3), "per_page": 10}
    text = _list_text(sess, 0)
    assert "<b>1.</b>" in text and "<b>3.</b>" in text
    assert "3:00" in text  # 180s


def test_results_keyboard_pick_and_paging():
    from bot.handlers.results import _kb
    sess = {"header": "h", "items": _items(30), "per_page": 10}
    kb = _kb("tok", sess, 0, lambda k, **kw: k)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "pick:tok:0" in datas and "pick:tok:9" in datas
    assert "page:tok:1" in datas  # next page exists
    assert "page:tok:-1" not in datas  # no prev on page 0


def test_results_keyboard_extras_lyrics_video():
    from bot.handlers.results import _kb
    sess = {"header": "h", "items": _items(5), "per_page": 5, "extras": True}
    kb = _kb("tok", sess, 0, lambda k, **kw: k)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "lyr:tok" in datas and "vid:tok" in datas
    assert "pick:tok:0" in datas
    assert not any(d.startswith("page:") for d in datas)  # only 5 items, no paging


def test_results_page2_has_back_button():
    from bot.handlers.results import _kb
    sess = {"header": "h", "items": _items(30), "per_page": 10}
    kb = _kb("tok", sess, 1, lambda k, **kw: k)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "page:tok:0" in datas  # back
    assert "pick:tok:10" in datas and "pick:tok:19" in datas
