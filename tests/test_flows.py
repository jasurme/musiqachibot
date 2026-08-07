"""End-to-end flow tests: real updates through the real dispatcher, network faked."""
import asyncio
import os

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot.handlers import broadcast, results, url_download
from bot import jobs
from bot.services import downloader
from bot.services.downloader import DownloadResult, MediaMeta
from bot.services.recognizer import Track
from bot.services.search import SearchItem
from tests.conftest import (
    callback_update,
    document_update,
    first_callback_data,
    text_update,
    video_update,
    video_note_update,
    voice_update,
)

ADMIN_ID = 7645204689


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


async def test_privacy_command_discloses_retention(dp, bot, cap):
    await dp.feed_update(bot, text_update("/privacy"))
    message = cap.last("SendMessage")
    assert "Privacy" in message.text
    assert "7 days" in message.text and "30 days" in message.text


async def test_delete_my_data_purges_db_and_memory(
    dp, bot, cap, storage,
):
    await storage.set_locale(100, "en")
    await storage.save_session("persisted", {"owner_user_id": 100, "url": "x"})
    results._SESS["result"] = {"owner_user_id": 100}
    url_download._PENDING["download"] = {"owner_user_id": 100}

    await dp.feed_update(bot, text_update("/delete_my_data"))

    assert await storage.get_locale(100) is None
    assert 100 not in await storage.get_active_user_ids()
    assert await storage.get_session("persisted") is None
    assert "result" not in results._SESS
    assert "download" not in url_download._PENDING
    assert "deleted" in cap.last("SendMessage").text.lower()


def _fake_dl(config, counter):
    async def fake(url, out_dir, max_bytes=None):
        assert max_bytes == config.max_file_mb * 1024 * 1024
        counter["n"] += 1
        import os
        p = os.path.join(config.download_dir, "t.mp3")
        with open(p, "wb") as f:
            f.write(b"0" * 20000)
        return DownloadResult(path=p, title="Ummon - Song 0", uploader="ch", duration=200, ext="mp3")
    return fake


# ── administrator broadcast ─────────────────────────────
@pytest.mark.parametrize(
    "make_update",
    [
        pytest.param(
            lambda: text_update(
                "Service announcement", user_id=ADMIN_ID, chat_id=ADMIN_ID
            ),
            id="text",
        ),
        pytest.param(
            lambda: document_update(user_id=ADMIN_ID, chat_id=ADMIN_ID),
            id="document",
        ),
    ],
)
async def test_admin_private_content_is_copied_to_every_active_user(
    dp, bot, cap, storage, monkeypatch, make_update,
):
    for user_id in (101, 202, ADMIN_ID):
        await storage.touch_private_user(user_id)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(broadcast.asyncio, "sleep", no_wait)
    update = make_update()
    await dp.feed_update(bot, update)

    copies = cap.by("CopyMessage")
    assert [method.chat_id for method in copies] == [101, 202]
    assert all(method.from_chat_id == ADMIN_ID for method in copies)
    assert all(method.message_id == update.message.message_id for method in copies)
    assert cap.by("ForwardMessage") == []
    summary = cap.last("SendMessage")
    assert "Sent: 2" in summary.text and "Failed: 0" in summary.text


def _broadcast_action(markup, action: str) -> str:
    suffix = f":{action}"
    return next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.endswith(suffix)
    )


def _captured_message_id(cap, method) -> int:
    """The fake Telegram session assigns the one-based request sequence ID."""
    return next(
        index for index, captured in enumerate(cap.methods, start=1)
        if captured is method
    )


async def _stage_admin_video(dp, bot, cap, *, circle_source: bool = False):
    update = (
        video_note_update(user_id=ADMIN_ID, chat_id=ADMIN_ID)
        if circle_source
        else video_update(user_id=ADMIN_ID, chat_id=ADMIN_ID)
    )
    await dp.feed_update(bot, update)
    prompt = cap.last("SendMessage")
    assert prompt is not None and "How should" in prompt.text
    assert cap.by("CopyMessage") == []
    return prompt, _captured_message_id(cap, prompt)


async def test_admin_video_requires_format_and_confirmation_before_same_type_copy(
    dp, bot, cap, storage, monkeypatch,
):
    for user_id in (101, 202, ADMIN_ID):
        await storage.touch_private_user(user_id)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(broadcast.asyncio, "sleep", no_wait)
    prompt, control_message_id = await _stage_admin_video(dp, bot, cap)
    normal = _broadcast_action(prompt.reply_markup, "normal")

    await dp.feed_update(
        bot,
        callback_update(
            normal, uid=2, user_id=ADMIN_ID, chat_id=ADMIN_ID,
            message_id=control_message_id,
        ),
    )
    confirmation = cap.last("EditMessageText")
    assert "normal video" in confirmation.text
    confirm = normal.rsplit(":", 1)[0] + ":confirm"

    await dp.feed_update(
        bot,
        callback_update(
            confirm, uid=3, user_id=ADMIN_ID, chat_id=ADMIN_ID,
            message_id=control_message_id,
        ),
    )

    copies = cap.by("CopyMessage")
    assert [method.chat_id for method in copies] == [101, 202]
    assert all(method.from_chat_id == ADMIN_ID for method in copies)
    assert all(method.message_id == 1 for method in copies)
    for method in copies:
        buttons = [
            button for row in method.reply_markup.inline_keyboard for button in row
        ]
        assert len(buttons) == 1
        assert buttons[0].callback_data == "top_music"

    # A replayed Confirm is no longer in awaiting_confirmation and must not
    # create a second campaign.
    await dp.feed_update(
        bot,
        callback_update(
            confirm, uid=4, user_id=ADMIN_ID, chat_id=ADMIN_ID,
            message_id=control_message_id,
        ),
    )
    assert len(cap.by("CopyMessage")) == 2


async def test_admin_video_cross_conversion_uploads_preview_once_then_reuses_file_id(
    dp, bot, cap, storage, config, monkeypatch,
):
    for user_id in (101, 202, ADMIN_ID):
        await storage.touch_private_user(user_id)
    calls = {"download": 0, "convert": 0}

    async def fake_download(file, destination, **kwargs):
        calls["download"] += 1
        with open(destination, "wb") as output:
            output.write(b"source-video")

    async def fake_note(src_path, out_dir, **kwargs):
        calls["convert"] += 1
        path = os.path.join(out_dir, "prepared.note.mp4")
        with open(path, "wb") as output:
            output.write(b"prepared-circle")
        return path

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(bot, "download", fake_download)
    monkeypatch.setattr("bot.services.video.make_video_note", fake_note)
    monkeypatch.setattr(broadcast.asyncio, "sleep", no_wait)

    prompt, control_message_id = await _stage_admin_video(dp, bot, cap)
    circle = _broadcast_action(prompt.reply_markup, "circle")
    await dp.feed_update(
        bot,
        callback_update(
            circle, uid=10, user_id=ADMIN_ID, chat_id=ADMIN_ID,
            message_id=control_message_id,
        ),
    )

    previews = [
        method for method in cap.by("SendVideoNote")
        if method.chat_id == ADMIN_ID
    ]
    assert len(previews) == 1
    preview = previews[0]
    preview_message_id = _captured_message_id(cap, preview)
    assert _broadcast_action(preview.reply_markup, "confirm")
    assert calls == {"download": 1, "convert": 1}

    confirm = circle.rsplit(":", 1)[0] + ":confirm"
    await dp.feed_update(
        bot,
        callback_update(
            confirm, uid=11, user_id=ADMIN_ID, chat_id=ADMIN_ID,
            message_id=preview_message_id,
        ),
    )

    deliveries = [
        method for method in cap.by("SendVideoNote")
        if method.chat_id in {101, 202}
    ]
    assert [method.chat_id for method in deliveries] == [101, 202]
    assert len({method.video_note for method in deliveries}) == 1
    assert all(isinstance(method.video_note, str) for method in deliveries)
    assert all(
        method.reply_markup.inline_keyboard[0][0].callback_data == "top_music"
        for method in deliveries
    )
    assert calls == {"download": 1, "convert": 1}


async def test_admin_circle_can_be_reuploaded_once_as_normal_video(
    dp, bot, cap, storage, monkeypatch,
):
    for user_id in (101, ADMIN_ID):
        await storage.touch_private_user(user_id)
    downloads = 0

    async def fake_download(file, destination, **kwargs):
        nonlocal downloads
        downloads += 1
        with open(destination, "wb") as output:
            output.write(b"square-mp4")

    async def forbidden_conversion(*args, **kwargs):
        raise AssertionError("circle-to-normal must not run ffmpeg")

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(bot, "download", fake_download)
    monkeypatch.setattr("bot.services.video.make_video_note", forbidden_conversion)
    monkeypatch.setattr(broadcast.asyncio, "sleep", no_wait)

    prompt, control_message_id = await _stage_admin_video(
        dp, bot, cap, circle_source=True,
    )
    normal = _broadcast_action(prompt.reply_markup, "normal")
    await dp.feed_update(
        bot,
        callback_update(
            normal, uid=15, user_id=ADMIN_ID, chat_id=ADMIN_ID,
            message_id=control_message_id,
        ),
    )

    previews = [
        method for method in cap.by("SendVideo")
        if method.chat_id == ADMIN_ID
    ]
    assert len(previews) == 1
    preview = previews[0]
    preview_message_id = _captured_message_id(cap, preview)
    assert _broadcast_action(preview.reply_markup, "confirm")
    assert downloads == 1

    confirm = normal.rsplit(":", 1)[0] + ":confirm"
    await dp.feed_update(
        bot,
        callback_update(
            confirm, uid=16, user_id=ADMIN_ID, chat_id=ADMIN_ID,
            message_id=preview_message_id,
        ),
    )

    deliveries = [
        method for method in cap.by("SendVideo") if method.chat_id == 101
    ]
    assert len(deliveries) == 1
    assert isinstance(deliveries[0].video, str)
    assert deliveries[0].reply_markup.inline_keyboard[0][0].callback_data == "top_music"
    assert downloads == 1


async def test_admin_media_draft_can_be_cancelled_without_delivery(
    dp, bot, cap, storage,
):
    await storage.touch_private_user(101)
    prompt, control_message_id = await _stage_admin_video(
        dp, bot, cap, circle_source=True,
    )
    cancel = _broadcast_action(prompt.reply_markup, "cancel")

    await dp.feed_update(
        bot,
        callback_update(
            cancel, uid=20, user_id=ADMIN_ID, chat_id=ADMIN_ID,
            message_id=control_message_id,
        ),
    )

    assert cap.by("CopyMessage") == []
    assert len(cap.by("DeleteMessage")) == 1
    assert "cancelled" in cap.last("SendMessage").text.lower()


async def test_two_admin_confirm_callbacks_have_one_atomic_winner(
    dp, bot, cap, storage, monkeypatch,
):
    await storage.touch_private_user(101)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(broadcast.asyncio, "sleep", no_wait)
    prompt, control_message_id = await _stage_admin_video(dp, bot, cap)
    normal = _broadcast_action(prompt.reply_markup, "normal")
    await dp.feed_update(
        bot,
        callback_update(
            normal, uid=30, user_id=ADMIN_ID, chat_id=ADMIN_ID,
            message_id=control_message_id,
        ),
    )
    confirm = normal.rsplit(":", 1)[0] + ":confirm"

    await asyncio.gather(
        dp.feed_update(
            bot,
            callback_update(
                confirm, uid=31, user_id=ADMIN_ID, chat_id=ADMIN_ID,
                message_id=control_message_id,
            ),
        ),
        dp.feed_update(
            bot,
            callback_update(
                confirm, uid=32, user_id=ADMIN_ID, chat_id=ADMIN_ID,
                message_id=control_message_id,
            ),
        ),
    )

    assert [method.chat_id for method in cap.by("CopyMessage")] == [101]


async def test_admin_broadcast_preempts_normal_link_and_search_handlers(
    dp, bot, cap, storage, monkeypatch,
):
    await storage.touch_private_user(101)

    async def forbidden_provider_call(*args, **kwargs):
        raise AssertionError("admin announcement reached a provider handler")

    monkeypatch.setattr(downloader, "extract_meta", forbidden_provider_call)
    monkeypatch.setattr(
        "bot.handlers.text_search.search_tracks", forbidden_provider_call
    )
    await dp.feed_update(
        bot,
        text_update(
            "https://www.instagram.com/reel/example/",
            user_id=ADMIN_ID,
            chat_id=ADMIN_ID,
        ),
    )

    assert len(cap.by("CopyMessage")) == 1
    assert url_download._PENDING == {}


async def test_private_user_is_registered_even_for_unhandled_document(
    dp, bot, cap, storage,
):
    await dp.feed_update(bot, document_update(user_id=909, chat_id=909))
    assert await storage.get_active_user_ids() == [909]
    assert cap.methods == []


async def test_group_user_and_group_admin_are_not_broadcast_registered(
    dp, bot, cap, storage,
):
    await dp.feed_update(
        bot,
        text_update(
            "announcement", user_id=ADMIN_ID, chat_id=-100,
            chat_type="supergroup",
        ),
    )
    assert cap.by("CopyMessage") == []
    assert await storage.get_active_user_ids() == []


async def test_broadcast_deactivates_blocked_user_and_continues(
    dp, bot, cap, storage, monkeypatch,
):
    for user_id in (101, 202):
        await storage.touch_private_user(user_id)
    attempted: list[int] = []
    original_request = bot.session.make_request

    async def request_with_block(bot_, method, timeout=None):
        if type(method).__name__ == "CopyMessage":
            attempted.append(method.chat_id)
            if method.chat_id == 101:
                raise TelegramForbiddenError(
                    method, "Forbidden: bot was blocked by the user"
                )
        return await original_request(bot_, method, timeout=timeout)

    async def no_wait(_seconds):
        return None

    bot.session.make_request = request_with_block
    monkeypatch.setattr(broadcast.asyncio, "sleep", no_wait)
    await dp.feed_update(
        bot,
        text_update("Notice", user_id=ADMIN_ID, chat_id=ADMIN_ID),
    )

    assert attempted == [101, 202]
    assert await storage.get_active_user_ids() == [202, ADMIN_ID]

    attempted.clear()
    await dp.feed_update(
        bot,
        text_update("Second", uid=2, user_id=ADMIN_ID, chat_id=ADMIN_ID),
    )
    assert attempted == [202]


async def test_broadcast_retries_same_user_after_flood_wait(
    dp, bot, cap, storage, monkeypatch,
):
    await storage.touch_private_user(101)
    attempts = 0
    sleeps: list[float] = []
    original_request = bot.session.make_request

    async def request_with_retry(bot_, method, timeout=None):
        nonlocal attempts
        if type(method).__name__ == "CopyMessage":
            attempts += 1
            if attempts == 1:
                raise TelegramRetryAfter(method, "Too Many Requests", retry_after=2)
        return await original_request(bot_, method, timeout=timeout)

    async def record_sleep(seconds):
        sleeps.append(seconds)

    bot.session.make_request = request_with_retry
    monkeypatch.setattr(broadcast.asyncio, "sleep", record_sleep)
    await dp.feed_update(
        bot,
        text_update("Notice", user_id=ADMIN_ID, chat_id=ADMIN_ID),
    )

    assert attempts == 2
    assert any(seconds >= 2 for seconds in sleeps)


async def test_broadcast_voice_privacy_error_does_not_deactivate_user(
    dp, bot, storage, monkeypatch,
):
    await storage.touch_private_user(101)
    original_request = bot.session.make_request

    async def request_with_voice_privacy(bot_, method, timeout=None):
        if type(method).__name__ == "CopyMessage":
            raise TelegramForbiddenError(
                method, "Forbidden: VOICE_MESSAGES_FORBIDDEN"
            )
        return await original_request(bot_, method, timeout=timeout)

    async def no_wait(_seconds):
        return None

    bot.session.make_request = request_with_voice_privacy
    monkeypatch.setattr(broadcast.asyncio, "sleep", no_wait)
    await dp.feed_update(
        bot,
        voice_update(user_id=ADMIN_ID, chat_id=ADMIN_ID),
    )

    assert await storage.get_active_user_ids() == [101, ADMIN_ID]


async def test_admin_unsupported_message_type_stops_before_audience(
    dp, bot, cap, storage,
):
    await storage.touch_private_user(101)
    update = text_update("placeholder", user_id=ADMIN_ID, chat_id=ADMIN_ID)
    update = update.model_copy(
        update={"message": update.message.model_copy(update={"text": None})}
    )

    await dp.feed_update(bot, update)

    assert cap.by("CopyMessage") == []
    assert "cannot copy" in cap.last("SendMessage").text


# ── /start & language ────────────────────────────────────
async def test_start_welcome_and_language_buttons(dp, bot, cap):
    await dp.feed_update(bot, text_update("/start"))
    sm = cap.last("SendMessage")
    assert sm is not None and "👋" in sm.text
    datas = [b.callback_data for row in sm.reply_markup.inline_keyboard for b in row]
    assert {"setlang:uz", "setlang:ru", "setlang:en"} <= set(datas)


async def test_first_time_user_uses_configured_default(dp, bot, cap):
    # The fixture config sets English; Telegram client language does not override it.
    await dp.feed_update(bot, text_update("/start", user_id=777, lang="en"))
    sm = cap.last("SendMessage")
    assert "Hi!" in sm.text


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
    seen = {}

    async def fake_search(q, limit=30):
        seen["limit"] = limit
        return _items(limit)
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)

    await dp.feed_update(bot, text_update("ummon"))
    sm = cap.last("SendMessage")
    assert sm is not None
    assert seen["limit"] == 5
    assert "<b>1.</b>" in sm.text and "<b>5.</b>" in sm.text
    assert "<b>6.</b>" not in sm.text
    assert first_callback_data(sm.reply_markup, "pick:") is not None
    assert first_callback_data(sm.reply_markup, "page:") is None
    assert len(cap.by("SendMessage")) == 1
    assert cap.by("EditMessageText") == []


async def test_search_no_results(dp, bot, cap, monkeypatch):
    async def fake_search(q, limit=30):
        return []
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)
    await dp.feed_update(bot, text_update("zzzxxx"))
    sm = cap.last("SendMessage")
    assert "😔" in sm.text
    assert len(cap.by("SendMessage")) == 1
    assert cap.by("EditMessageText") == []


async def test_search_failure_is_a_single_direct_error(dp, bot, cap, monkeypatch):
    async def fake_search(q, limit=5):
        raise downloader.ProviderBusy("media provider queue is busy")

    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)
    await dp.feed_update(bot, text_update("ummon"))

    messages = cap.by("SendMessage")
    assert len(messages) == 1 and "busy" in messages[0].text.lower()
    assert cap.by("EditMessageText") == []
    assert cap.by("DeleteMessage") == []


async def test_unsupported_url_is_not_sent_to_music_search(dp, bot, cap, monkeypatch):
    called = {"n": 0}

    async def fake_search(q, limit=30):
        called["n"] += 1
        return []

    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)
    await dp.feed_update(bot, text_update("https://example.com/video"))
    assert called["n"] == 0
    assert "isn't supported" in cap.last("SendMessage").text


async def test_long_search_is_rejected_before_provider_call(dp, bot, cap, monkeypatch):
    called = {"n": 0}

    async def fake_search(q, limit=30):
        called["n"] += 1
        return []

    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)
    await dp.feed_update(bot, text_update("x" * 201))
    assert called["n"] == 0
    assert "too long" in cap.last("SendMessage").text


async def test_pick_downloads_signs_and_caches(dp, bot, cap, config, monkeypatch, storage):
    counter = {"n": 0}
    monkeypatch.setattr(downloader, "download_audio", _fake_dl(config, counter))

    async def fake_search(q, limit=30):
        return _items(12)
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)

    # 1) search to create a session + get a real pick token
    await dp.feed_update(bot, text_update("ummon"))
    token_data = first_callback_data(cap.last("SendMessage").reply_markup, "pick:")
    assert token_data

    # 2) first pick → downloads, sends with signature, caches file_id
    cap.methods.clear()
    await dp.feed_update(bot, callback_update(token_data, uid=2))
    sa = cap.last("SendAudio")
    assert sa is not None and "👉 @testbot" in sa.caption
    assert counter["n"] == 1
    assert await storage.get_cached_audio("bot:123456:ytaudio:v0") is not None
    ack = cap.last("AnswerCallbackQuery")
    assert ack is not None and ack.text is None
    assert cap.names().index("AnswerCallbackQuery") < cap.names().index("SendAudio")
    assert cap.by("SendMessage") == []
    assert cap.by("DeleteMessage") == []

    # 3) second identical pick → NO new download, reuses cached file_id
    cap.methods.clear()
    await dp.feed_update(bot, callback_update(token_data, uid=3))
    sa2 = cap.last("SendAudio")
    assert counter["n"] == 1, "cache miss — should not re-download"
    assert isinstance(sa2.audio, str) and sa2.audio.startswith("AUDIO_")


async def test_cached_track_bypasses_busy_heavy_job_limit(
    dp, bot, cap, storage,
):
    results._SESS["cached"] = {
        "header": "h", "items": _items(1), "per_page": 5,
        "extras": False, "owner_user_id": None,
    }
    await storage.set_cached_audio(
        "bot:123456:ytaudio:v0", "CACHED_AUDIO", "Song"
    )
    jobs.configure(1)
    assert jobs.claim(999)

    await dp.feed_update(bot, callback_update("pick:cached:0"))

    assert cap.last("SendAudio").audio == "CACHED_AUDIO"


async def test_simultaneous_track_picks_share_one_download(
    dp, bot, cap, config, monkeypatch,
):
    results._SESS["shared"] = {
        "header": "h", "items": _items(1), "per_page": 5,
        "extras": False, "owner_user_id": None,
    }
    calls = 0

    async def slow_download(url, out_dir, max_bytes=None):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        path = os.path.join(config.download_dir, "shared.m4a")
        with open(path, "wb") as stream:
            stream.write(b"audio")
        return DownloadResult(
            path=path, title="Song", uploader="Artist", duration=10, ext="m4a"
        )

    monkeypatch.setattr(downloader, "download_audio", slow_download)
    await asyncio.gather(
        dp.feed_update(
            bot, callback_update("pick:shared:0", uid=101, user_id=101)
        ),
        dp.feed_update(
            bot, callback_update("pick:shared:0", uid=102, user_id=102)
        ),
    )

    assert calls == 1
    assert len(cap.by("SendAudio")) == 2


async def test_repeated_tap_does_not_start_overlapping_job(dp, bot, cap, config, monkeypatch):
    url_download._PENDING["tok"] = {"url": "https://youtu.be/abc", "title": "Song"}
    counter = {"n": 0}
    monkeypatch.setattr(downloader, "download_audio", _fake_dl(config, counter))
    assert jobs.claim(100)

    await dp.feed_update(bot, callback_update("dl:tok:audio"))

    assert counter["n"] == 0
    answer = cap.last("AnswerCallbackQuery")
    assert answer.show_alert is True and "still processing" in answer.text


async def test_pagination_handles_unexpected_extra_results(dp, bot, cap, monkeypatch):
    # Keep the shared component defensive if a provider returns over its limit.
    async def fake_search(q, limit=30):
        return _items(30)
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)
    await dp.feed_update(bot, text_update("ummon"))
    page_data = first_callback_data(cap.last("SendMessage").reply_markup, "page:")
    cap.methods.clear()
    await dp.feed_update(bot, callback_update(page_data, uid=4))
    em = cap.last("EditMessageText")
    assert "<b>6.</b>" in em.text and "<b>10.</b>" in em.text


# ── Feature C: recognition ───────────────────────────────
class _FakeRec:
    def __init__(self, track):
        self._track = track

    async def recognize(self, path):
        return self._track


async def test_recognition_shows_header_art_and_extras(dp, bot, cap, monkeypatch):
    async def fake_download(media, destination=None, **kw):
        assert kw == {"timeout": 300}
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


async def test_cached_recognition_bypasses_busy_heavy_job_limit(
    dp, bot, cap, monkeypatch, storage,
):
    await storage.set_recognition("vu1", "Believer", "Imagine Dragons")

    async def fake_search(q, limit=5):
        return _items(5)

    async def should_not_download(*args, **kwargs):
        raise AssertionError("cached recognition should not download media")

    monkeypatch.setattr("bot.handlers.media_recognize.search_tracks", fake_search)
    monkeypatch.setattr(bot, "download", should_not_download)
    jobs.configure(1)
    assert jobs.claim(999)

    await dp.feed_update(bot, voice_update())

    assert "Believer" in cap.last("SendMessage").text


async def test_recognition_failure(dp, bot, cap, monkeypatch):
    async def fake_download(media, destination=None, **kw):
        assert kw == {"timeout": 300}
        with open(destination, "wb") as f:
            f.write(b"x")
    monkeypatch.setattr(bot, "download", fake_download)
    monkeypatch.setattr("bot.services.audio.make_sample", lambda *a, **k: _aret("s.mp3"))
    monkeypatch.setattr("bot.handlers.media_recognize.get_recognizer",
                        lambda cfg: _FakeRec(None))
    await dp.feed_update(bot, voice_update())
    sm = cap.last("SendMessage")
    assert "😔" in sm.text
    assert len(cap.by("SendMessage")) == 1
    assert cap.by("EditMessageText") == []


async def test_recognition_survives_enrichment_search_outage(
    dp, bot, cap, monkeypatch,
):
    async def fake_download(media, destination=None, **kw):
        with open(destination, "wb") as stream:
            stream.write(b"x")

    async def unavailable(*args, **kwargs):
        raise RuntimeError("YouTube unavailable")

    monkeypatch.setattr(bot, "download", fake_download)
    monkeypatch.setattr(
        "bot.services.audio.make_sample", lambda *a, **k: _aret("s.mp3")
    )
    monkeypatch.setattr(
        "bot.handlers.media_recognize.get_recognizer",
        lambda cfg: _FakeRec(
            Track(
                title="Believer", artist="Imagine Dragons",
                url="https://example.com/listen", cover="http://cover.jpg",
            )
        ),
    )
    monkeypatch.setattr(
        "bot.handlers.media_recognize.search_tracks", unavailable
    )

    await dp.feed_update(bot, voice_update())
    photo = cap.last("SendPhoto")
    assert photo is not None and "Believer" in photo.caption
    buttons = [
        button
        for row in photo.reply_markup.inline_keyboard
        for button in row
    ]
    assert any(button.url == "https://example.com/listen" for button in buttons)
    assert not any((button.callback_data or "").startswith("vid:") for button in buttons)


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
    ack = cap.last("AnswerCallbackQuery")
    assert ack is not None and ack.text is None
    assert cap.names().index("AnswerCallbackQuery") < cap.names().index("SendMessage")


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
    assert len(cap.by("SendMessage")) == 1
    assert cap.by("DeleteMessage") == []


async def test_url_metadata_failure_is_a_single_direct_error(
    dp, bot, cap, monkeypatch,
):
    async def fake_meta(url):
        raise downloader.yt_dlp.utils.DownloadError("Private video")

    monkeypatch.setattr(downloader, "extract_meta", fake_meta)
    await dp.feed_update(bot, text_update("https://youtu.be/private"))

    messages = cap.by("SendMessage")
    assert len(messages) == 1 and "private" in messages[0].text.lower()
    assert cap.by("EditMessageText") == []
    assert cap.by("DeleteMessage") == []


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


async def test_result_callback_is_bound_to_requesting_user(dp, bot, cap):
    results._SESS["owned"] = {
        "header": "h", "items": _items(1), "per_page": 5,
        "extras": False, "owner_user_id": 7,
    }
    await dp.feed_update(
        bot, callback_update("pick:owned:0", user_id=8)
    )
    answer = cap.last("AnswerCallbackQuery")
    assert answer.show_alert is True
    assert "invalid" in answer.text.lower()


async def test_download_callback_is_bound_to_requesting_user(dp, bot, cap):
    url_download._PENDING["owned"] = {
        "url": "https://youtu.be/abc", "title": "Song", "owner_user_id": 7,
    }
    await dp.feed_update(
        bot, callback_update("dl:owned:audio", user_id=8)
    )
    answer = cap.last("AnswerCallbackQuery")
    assert answer.show_alert is True
    assert "invalid" in answer.text.lower()


async def test_quality_audio_download(dp, bot, cap, config, monkeypatch, storage):
    url_download._PENDING["tok"] = {"url": "https://youtu.be/abc", "title": "Song"}
    counter = {"n": 0}
    monkeypatch.setattr(downloader, "download_audio", _fake_dl(config, counter))

    await dp.feed_update(bot, callback_update("dl:tok:audio"))
    sa = cap.last("SendAudio")
    assert sa is not None and "👉 @testbot" in sa.caption
    assert counter["n"] == 1
    ack = cap.last("AnswerCallbackQuery")
    assert ack is not None and ack.text is None
    assert cap.names().index("AnswerCallbackQuery") < cap.names().index("SendAudio")
    assert cap.by("SendMessage") == []
    assert cap.by("DeleteMessage") == []
    # Direct YouTube links share the same canonical cache as text search.
    assert await storage.get_cached_audio(
        "bot:123456:ytaudio:abc"
    ) is not None


async def test_cached_media_bypasses_provider_circuit(
    dp, bot, cap, monkeypatch, storage,
):
    url = "https://youtu.be/abc"
    url_download._PENDING["tok"] = {"url": url, "title": "Song"}
    await storage.set_cached_audio(
        f"bot:{bot.id}:ytaudio:abc", "CACHED_AUDIO", "Song"
    )
    downloader.record_provider_failure(
        url, downloader.yt_dlp.utils.DownloadError("HTTP Error 403: Forbidden")
    )

    async def should_not_download(*args, **kwargs):
        raise AssertionError("cached file_id should bypass yt-dlp")

    monkeypatch.setattr(downloader, "download_audio", should_not_download)
    await dp.feed_update(bot, callback_update("dl:tok:audio"))
    sent = cap.last("SendAudio")
    assert sent is not None and sent.audio == "CACHED_AUDIO"
    ack = cap.last("AnswerCallbackQuery")
    assert ack is not None and ack.text is None
    assert cap.names().index("AnswerCallbackQuery") < cap.names().index("SendAudio")


async def test_quality_failure_acks_then_sends_direct_error(
    dp, bot, cap, monkeypatch,
):
    url_download._PENDING["tok"] = {
        "url": "https://youtu.be/private", "title": "Song",
    }

    async def fail_download(*args, **kwargs):
        raise downloader.yt_dlp.utils.DownloadError("Private video")

    monkeypatch.setattr(downloader, "download_audio", fail_download)
    await dp.feed_update(bot, callback_update("dl:tok:audio"))

    ack = cap.last("AnswerCallbackQuery")
    message = cap.last("SendMessage")
    assert ack is not None and ack.text is None
    assert message is not None and "private" in message.text.lower()
    assert cap.names().index("AnswerCallbackQuery") < cap.names().index("SendMessage")
    assert cap.by("EditMessageText") == []
    assert cap.by("DeleteMessage") == []


async def test_quality_video_download(dp, bot, cap, config, monkeypatch):
    url_download._PENDING["tok"] = {"url": "https://youtu.be/abc", "title": "Song"}

    async def fake_vq(url, out_dir, height, max_bytes=None):
        assert max_bytes == config.max_file_mb * 1024 * 1024
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

    async def fake_dl_audio(url, out_dir, max_bytes=None):
        assert max_bytes == config.max_file_mb * 1024 * 1024
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
    ack = cap.last("AnswerCallbackQuery")
    assert ack is not None and ack.text is None
    assert cap.names().index("AnswerCallbackQuery") < cap.names().index("SendPhoto")
    assert cap.by("SendMessage") == []
    assert cap.by("DeleteMessage") == []


async def test_link_find_music_not_recognized(dp, bot, cap, config, monkeypatch):
    url_download._PENDING["tok"] = {"url": "https://youtu.be/abc", "title": "V"}

    async def fake_dl_audio(url, out_dir, max_bytes=None):
        assert max_bytes == config.max_file_mb * 1024 * 1024
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
    assert "😔" in cap.last("SendMessage").text
    assert cap.by("EditMessageText") == []


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
    assert cap.last("SendMessage") is not None


async def test_group_link_requires_mention(dp, bot, cap, monkeypatch):
    async def fake_meta(url):
        return MediaMeta(url=url, title="V", uploader="ch", duration=1,
                         thumbnail=None, heights=[360, 720])
    monkeypatch.setattr(downloader, "extract_meta", fake_meta)

    await dp.feed_update(bot, text_update("https://youtu.be/abc",
                                          chat_type="supergroup", chat_id=-100))
    assert cap.methods == []

    await dp.feed_update(bot, text_update("@testbot https://youtu.be/abc", uid=2,
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
        assert kw == {"timeout": 300}
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

    async def fake_vq(url, out_dir, height, max_bytes=None):
        assert max_bytes == config.max_file_mb * 1024 * 1024
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
        assert kw == {"timeout": 300}
        with open(destination, "wb") as f:
            f.write(b"x")
    monkeypatch.setattr(bot, "download", fake_download)
    monkeypatch.setattr("bot.services.audio.make_sample", lambda *a, **k: _aret("s.mp3"))
    monkeypatch.setattr("bot.handlers.media_recognize.get_recognizer",
                        lambda cfg: _FakeRec(None))
    await dp.feed_update(bot, video_update())
    assert cap.by("SendVideoNote") == []  # not rounded
    assert "😔" in cap.last("SendMessage").text  # recognition path ran


# ── the "havola eskirdi" fix: sessions survive a restart ─
async def test_results_button_survives_restart(dp, bot, cap, config, monkeypatch, storage):
    counter = {"n": 0}
    monkeypatch.setattr(downloader, "download_audio", _fake_dl(config, counter))

    async def fake_search(q, limit=30):
        return _items(12)
    monkeypatch.setattr("bot.handlers.text_search.search_tracks", fake_search)

    await dp.feed_update(bot, text_update("ummon"))
    token_data = first_callback_data(cap.last("SendMessage").reply_markup, "pick:")

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

    async def fake_vq(url, out_dir, height, max_bytes=None):
        assert max_bytes == config.max_file_mb * 1024 * 1024
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
