"""End-to-end flow tests: real updates through the real dispatcher, network faked."""
import pytest

from bot.handlers import results, url_download
from bot.services import downloader
from bot.services.downloader import DownloadResult, MediaMeta
from bot.services.recognizer import Track
from bot.services.search import SearchItem
from tests.conftest import (
    callback_update,
    first_callback_data,
    text_update,
    video_update,
    voice_update,
)


def _fake_note(config):
    async def fake(src, out_dir, **kw):
        import os
        p = os.path.join(out_dir, "note.mp4")
        with open(p, "wb") as f:
            f.write(b"0" * 20000)
        return p
    return fake


def _items(n):
    return [SearchItem(video_id=f"v{i}", title=f"Ummon - Song {i}", duration=200 + i, uploader="ch")
            for i in range(n)]


def _fake_dl(config, counter):
    async def fake(url, out_dir):
        counter["n"] += 1
        import os
        p = os.path.join(config.download_dir, "t.mp3")
        with open(p, "wb") as f:
            f.write(b"0" * 20000)
        return DownloadResult(path=p, title="Ummon - Song 0", uploader="ch", duration=200, ext="mp3")
    return fake


# ── /start & language ────────────────────────────────────
async def test_start_welcome_and_language_buttons(dp, bot, cap):
    await dp.feed_update(bot, text_update("/start"))
    sm = cap.last("SendMessage")
    assert sm is not None and "👋" in sm.text
    datas = [b.callback_data for row in sm.reply_markup.inline_keyboard for b in row]
    assert {"setlang:uz", "setlang:ru", "setlang:en"} <= set(datas)


async def test_first_time_user_defaults_to_uzbek(dp, bot, cap):
    # brand-new user whose Telegram client language is English → still Uzbek
    await dp.feed_update(bot, text_update("/start", user_id=777, lang="en"))
    sm = cap.last("SendMessage")
    assert "Salom" in sm.text  # Uzbek welcome, not English "Hi!"


async def test_stored_choice_overrides_default(dp, bot, cap, storage):
    await storage.set_locale(888, "en")
    await dp.feed_update(bot, text_update("/start", user_id=888, lang="uz"))
    sm = cap.last("SendMessage")
    assert "Hi!" in sm.text  # explicit English choice wins over uz default


async def test_language_switch_persists(dp, bot, cap, storage):
    await dp.feed_update(bot, callback_update("setlang:ru", user_id=100))
    assert await storage.get_locale(100) == "ru"
    # welcome after switch is Russian
    sm = cap.last("SendMessage")
    assert "Привет" in sm.text


# ── Feature A: search → list → pick → cache ─────────────
async def test_search_shows_numbered_list(dp, bot, cap, monkeypatch):
    async def fake_search(q, limit=30):
        return _items(30)
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)

    await dp.feed_update(bot, text_update("ummon"))
    em = cap.last("EditMessageText")
    assert em is not None
    assert "<b>1.</b>" in em.text and "<b>10.</b>" in em.text
    assert first_callback_data(em.reply_markup, "pick:") is not None
    assert first_callback_data(em.reply_markup, "page:") is not None  # 30 items → paging


async def test_search_no_results(dp, bot, cap, monkeypatch):
    async def fake_search(q, limit=30):
        return []
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)
    await dp.feed_update(bot, text_update("zzzxxx"))
    em = cap.last("EditMessageText")
    assert "😔" in em.text


async def test_pick_downloads_signs_and_caches(dp, bot, cap, config, monkeypatch, storage):
    counter = {"n": 0}
    monkeypatch.setattr(downloader, "download_audio", _fake_dl(config, counter))

    async def fake_search(q, limit=30):
        return _items(12)
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)

    # 1) search to create a session + get a real pick token
    await dp.feed_update(bot, text_update("ummon"))
    token_data = first_callback_data(cap.last("EditMessageText").reply_markup, "pick:")
    assert token_data

    # 2) first pick → downloads, sends with signature, caches file_id
    await dp.feed_update(bot, callback_update(token_data, uid=2))
    sa = cap.last("SendAudio")
    assert sa is not None and "👉 @testbot" in sa.caption
    assert counter["n"] == 1
    assert await storage.get_cached_audio("ytaudio:v0") is not None

    # 3) second identical pick → NO new download, reuses cached file_id
    cap.methods.clear()
    await dp.feed_update(bot, callback_update(token_data, uid=3))
    sa2 = cap.last("SendAudio")
    assert counter["n"] == 1, "cache miss — should not re-download"
    assert isinstance(sa2.audio, str) and sa2.audio.startswith("AUDIO_")


async def test_pagination_next_page(dp, bot, cap, monkeypatch):
    async def fake_search(q, limit=30):
        return _items(30)
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)
    await dp.feed_update(bot, text_update("ummon"))
    page_data = first_callback_data(cap.last("EditMessageText").reply_markup, "page:")
    cap.methods.clear()
    await dp.feed_update(bot, callback_update(page_data, uid=4))
    em = cap.last("EditMessageText")
    assert "<b>11.</b>" in em.text and "<b>20.</b>" in em.text


# ── Feature C: recognition ───────────────────────────────
class _FakeRec:
    def __init__(self, track):
        self._track = track

    async def recognize(self, path):
        return self._track


async def test_recognition_shows_header_art_and_extras(dp, bot, cap, monkeypatch):
    async def fake_download(media, destination=None, **kw):
        with open(destination, "wb") as f:
            f.write(b"x")
    monkeypatch.setattr(bot, "download", fake_download)
    monkeypatch.setattr("bot.services.audio.make_sample",
                        lambda *a, **k: _aret("s.mp3"))
    monkeypatch.setattr("bot.handlers.media_recognize.get_recognizer",
                        lambda cfg: _FakeRec(Track(title="Believer", artist="Imagine Dragons",
                                                   cover="http://cover.jpg")))

    async def fake_search(q, limit=5):
        return _items(5)
    monkeypatch.setattr("bot.handlers.media_recognize.search_tracks", fake_search)

    await dp.feed_update(bot, voice_update())
    ph = cap.last("SendPhoto")
    assert ph is not None
    assert "Believer" in ph.caption and "Imagine Dragons" in ph.caption
    datas = [b.callback_data for row in ph.reply_markup.inline_keyboard for b in row]
    assert any(d.startswith("lyr:") for d in datas)
    assert any(d.startswith("vid:") for d in datas)
    assert any(d.startswith("pick:") for d in datas)


async def test_recognition_failure(dp, bot, cap, monkeypatch):
    async def fake_download(media, destination=None, **kw):
        with open(destination, "wb") as f:
            f.write(b"x")
    monkeypatch.setattr(bot, "download", fake_download)
    monkeypatch.setattr("bot.services.audio.make_sample", lambda *a, **k: _aret("s.mp3"))
    monkeypatch.setattr("bot.handlers.media_recognize.get_recognizer",
                        lambda cfg: _FakeRec(None))
    await dp.feed_update(bot, voice_update())
    em = cap.last("EditMessageText")
    assert "😔" in em.text


# ── Feature B: lyrics button ─────────────────────────────
async def test_lyrics_button_found(dp, bot, cap, monkeypatch):
    results._SESS["tok"] = {"header": "h", "items": _items(3), "per_page": 5,
                            "extras": True, "artist": "Imagine Dragons", "title": "Believer"}

    async def fake_lyrics(artist, title):
        return "First they came for the...\nPain! You made me a believer"
    monkeypatch.setattr("bot.handlers.results.fetch_lyrics", fake_lyrics)

    await dp.feed_update(bot, callback_update("lyr:tok"))
    sm = cap.last("SendMessage")
    assert "believer" in sm.text.lower()


async def test_lyrics_button_not_found(dp, bot, cap, monkeypatch):
    results._SESS["tok"] = {"header": "h", "items": _items(3), "per_page": 5,
                            "extras": True, "artist": "Ummon", "title": "Xiyonat"}
    monkeypatch.setattr("bot.handlers.results.fetch_lyrics",
                        lambda a, t: _aret(None))
    await dp.feed_update(bot, callback_update("lyr:tok"))
    sm = cap.last("SendMessage")
    assert "😔" in sm.text


# ── Feature D: link quality picker + video button ────────
async def test_url_link_shows_quality_picker(dp, bot, cap, monkeypatch):
    async def fake_meta(url):
        return MediaMeta(url=url, title="Some Video", uploader="ch", duration=100,
                         thumbnail=None, heights=[360, 480, 720])
    monkeypatch.setattr(downloader, "extract_meta", fake_meta)

    await dp.feed_update(bot, text_update("https://youtu.be/abc"))
    sm = cap.last("SendMessage")
    datas = [b.callback_data for row in sm.reply_markup.inline_keyboard for b in row]
    assert any(d.startswith("dl:") and d.endswith(":360") for d in datas)
    assert any(d.endswith(":audio") for d in datas)


async def test_video_button_reuses_quality_picker(dp, bot, cap, monkeypatch):
    results._SESS["tok"] = {"header": "h", "items": _items(2), "per_page": 5,
                            "extras": True, "artist": "A", "title": "T"}

    async def fake_meta(url):
        return MediaMeta(url=url, title="V", uploader="ch", duration=1,
                         thumbnail=None, heights=[360, 720])
    monkeypatch.setattr(downloader, "extract_meta", fake_meta)

    await dp.feed_update(bot, callback_update("vid:tok"))
    sm = cap.last("SendMessage")
    datas = [b.callback_data for row in sm.reply_markup.inline_keyboard for b in row]
    assert any(d.startswith("dl:") for d in datas)


async def test_quality_audio_download(dp, bot, cap, config, monkeypatch, storage):
    url_download._PENDING["tok"] = {"url": "https://youtu.be/abc", "title": "Song"}
    counter = {"n": 0}
    monkeypatch.setattr(downloader, "download_audio", _fake_dl(config, counter))

    await dp.feed_update(bot, callback_update("dl:tok:audio"))
    sa = cap.last("SendAudio")
    assert sa is not None and "👉 @testbot" in sa.caption
    assert counter["n"] == 1
    # cached under the dl: key
    assert await storage.get_cached_audio("dl:https://youtu.be/abc:audio") is not None


async def test_quality_video_download(dp, bot, cap, config, monkeypatch):
    url_download._PENDING["tok"] = {"url": "https://youtu.be/abc", "title": "Song"}

    async def fake_vq(url, out_dir, height):
        import os
        p = os.path.join(config.download_dir, "v.mp4")
        with open(p, "wb") as f:
            f.write(b"0" * 20000)
        return DownloadResult(path=p, title="Song", uploader="ch", duration=100, ext="mp4")
    monkeypatch.setattr(downloader, "download_video_quality", fake_vq)

    await dp.feed_update(bot, callback_update("dl:tok:720"))
    sv = cap.last("SendVideo")
    assert sv is not None and "👉 @testbot" in sv.caption


# ── Feature D "Find music": recognize the song inside a link ─
async def test_link_find_music_button(dp, bot, cap, config, monkeypatch):
    url_download._PENDING["tok"] = {"url": "https://www.tiktok.com/@x/video/1", "title": "V"}

    async def fake_dl_audio(url, out_dir):
        import os
        p = os.path.join(config.download_dir, "a.mp3")
        with open(p, "wb") as f:
            f.write(b"0" * 20000)
        return DownloadResult(path=p, title="V", uploader="ch", duration=100, ext="mp3")
    monkeypatch.setattr(downloader, "download_audio", fake_dl_audio)
    monkeypatch.setattr("bot.services.audio.make_sample", lambda *a, **k: _aret("s.mp3"))

    class _Rec:
        async def recognize(self, path):
            return Track(title="Faded", artist="Alan Walker", cover="http://cover.jpg")
    monkeypatch.setattr("bot.handlers.media_recognize.get_recognizer", lambda cfg: _Rec())

    async def fake_search(q, limit=5):
        return _items(5)
    monkeypatch.setattr("bot.handlers.media_recognize.search_tracks", fake_search)

    await dp.feed_update(bot, callback_update("dl:tok:music"))
    ph = cap.last("SendPhoto")
    assert ph is not None
    assert "Faded" in ph.caption and "Alan Walker" in ph.caption
    datas = [b.callback_data for row in ph.reply_markup.inline_keyboard for b in row]
    assert any(d.startswith("pick:") for d in datas)
    assert any(d.startswith("lyr:") for d in datas)


async def test_link_find_music_not_recognized(dp, bot, cap, config, monkeypatch):
    url_download._PENDING["tok"] = {"url": "https://youtu.be/abc", "title": "V"}

    async def fake_dl_audio(url, out_dir):
        import os
        p = os.path.join(config.download_dir, "a.mp3")
        with open(p, "wb") as f:
            f.write(b"0" * 20000)
        return DownloadResult(path=p, title="V", uploader="ch", duration=100, ext="mp3")
    monkeypatch.setattr(downloader, "download_audio", fake_dl_audio)
    monkeypatch.setattr("bot.services.audio.make_sample", lambda *a, **k: _aret("s.mp3"))

    class _Rec:
        async def recognize(self, path):
            return None
    monkeypatch.setattr("bot.handlers.media_recognize.get_recognizer", lambda cfg: _Rec())

    await dp.feed_update(bot, callback_update("dl:tok:music"))
    assert "😔" in cap.last("EditMessageText").text


# ── group behaviour (tag to search, auto-handle links) ───
async def test_group_ignores_untagged_text(dp, bot, cap, monkeypatch):
    called = {"n": 0}

    async def fake_search(q, limit=30):
        called["n"] += 1
        return _items(5)
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)

    await dp.feed_update(bot, text_update("believer", chat_type="supergroup", chat_id=-100))
    assert cap.methods == [], "bot replied to an untagged group message"
    assert called["n"] == 0


async def test_group_tagged_searches(dp, bot, cap, monkeypatch):
    seen = {}

    async def fake_search(q, limit=30):
        seen["q"] = q
        return _items(5)
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)

    await dp.feed_update(bot, text_update("@testbot believer", chat_type="supergroup", chat_id=-100))
    assert seen.get("q") == "believer"  # bot mention stripped from the query
    assert cap.last("EditMessageText") is not None


async def test_group_link_is_handled(dp, bot, cap, monkeypatch):
    async def fake_meta(url):
        return MediaMeta(url=url, title="V", uploader="ch", duration=1,
                         thumbnail=None, heights=[360, 720])
    monkeypatch.setattr(downloader, "extract_meta", fake_meta)

    await dp.feed_update(bot, text_update("https://youtu.be/abc",
                                          chat_type="supergroup", chat_id=-100))
    sm = cap.last("SendMessage")
    datas = [b.callback_data for row in sm.reply_markup.inline_keyboard for b in row]
    assert any(d.startswith("dl:") for d in datas)


async def test_group_ignores_media(dp, bot, cap, monkeypatch):
    await dp.feed_update(bot, voice_update(chat_type="supergroup", chat_id=-100))
    assert cap.methods == [], "bot tried to recognize a group voice message"


# ── round video-notes (yumaloq video) ────────────────────
async def test_round_command_then_video(dp, bot, cap, config, monkeypatch):
    async def fake_download(media, destination=None, **kw):
        with open(destination, "wb") as f:
            f.write(b"x")
    monkeypatch.setattr(bot, "download", fake_download)
    monkeypatch.setattr("bot.services.video.make_video_note", _fake_note(config))

    # /round → prompt + FSM state
    await dp.feed_update(bot, text_update("/round"))
    assert "⭕" in cap.last("SendMessage").text

    # next video → round note
    cap.methods.clear()
    await dp.feed_update(bot, video_update(uid=2))
    assert cap.last("SendVideoNote") is not None


async def test_round_link_button(dp, bot, cap, config, monkeypatch):
    url_download._PENDING["tok"] = {"url": "https://youtu.be/abc", "title": "V"}

    async def fake_vq(url, out_dir, height):
        import os
        p = os.path.join(out_dir, "v.mp4")
        with open(p, "wb") as f:
            f.write(b"0" * 20000)
        return DownloadResult(path=p, title="V", uploader="ch", duration=10, ext="mp4")
    monkeypatch.setattr(downloader, "download_video_quality", fake_vq)
    monkeypatch.setattr("bot.services.video.make_video_note", _fake_note(config))

    await dp.feed_update(bot, callback_update("dl:tok:round"))
    assert cap.last("SendVideoNote") is not None


async def test_video_normally_recognizes_not_rounds(dp, bot, cap, config, monkeypatch):
    # without /round first, a video goes to recognition, NOT round conversion
    async def fake_download(media, destination=None, **kw):
        with open(destination, "wb") as f:
            f.write(b"x")
    monkeypatch.setattr(bot, "download", fake_download)
    monkeypatch.setattr("bot.services.audio.make_sample", lambda *a, **k: _aret("s.mp3"))
    monkeypatch.setattr("bot.handlers.media_recognize.get_recognizer",
                        lambda cfg: _FakeRec(None))
    await dp.feed_update(bot, video_update())
    assert cap.by("SendVideoNote") == []  # not rounded
    assert "😔" in cap.last("EditMessageText").text  # recognition path ran


# ── the "havola eskirdi" fix: sessions survive a restart ─
async def test_results_button_survives_restart(dp, bot, cap, config, monkeypatch, storage):
    counter = {"n": 0}
    monkeypatch.setattr(downloader, "download_audio", _fake_dl(config, counter))

    async def fake_search(q, limit=30):
        return _items(12)
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)

    await dp.feed_update(bot, text_update("ummon"))
    token_data = first_callback_data(cap.last("EditMessageText").reply_markup, "pick:")

    # simulate a bot restart: in-memory session cache is wiped, only DB remains
    results._SESS.clear()
    url_download._PENDING.clear()
    cap.methods.clear()

    await dp.feed_update(bot, callback_update(token_data, uid=9))
    assert cap.last("SendAudio") is not None, "button 'expired' after restart — DB fallback failed"
    assert counter["n"] == 1


async def test_link_button_survives_restart(dp, bot, cap, config, monkeypatch):
    async def fake_meta(url):
        return MediaMeta(url=url, title="V", uploader="ch", duration=1,
                         thumbnail=None, heights=[360, 720])
    monkeypatch.setattr(downloader, "extract_meta", fake_meta)

    await dp.feed_update(bot, text_update("https://youtu.be/abc"))
    dl_data = first_callback_data(cap.last("SendMessage").reply_markup, "dl:")

    results._SESS.clear()
    url_download._PENDING.clear()
    cap.methods.clear()

    async def fake_vq(url, out_dir, height):
        import os
        p = os.path.join(config.download_dir, "v.mp4")
        with open(p, "wb") as f:
            f.write(b"0" * 20000)
        return DownloadResult(path=p, title="V", uploader="ch", duration=1, ext="mp4")
    monkeypatch.setattr(downloader, "download_video_quality", fake_vq)

    await dp.feed_update(bot, callback_update(dl_data, uid=10))
    assert cap.last("SendVideo") is not None, "link button 'expired' after restart"


# ── tiny helper: wrap a value in an awaitable ────────────
async def _aret(v):
    return v
