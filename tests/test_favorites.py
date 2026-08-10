"""End-to-end favorite playlist and audio-heart behavior."""

from pathlib import Path

from aiogram.exceptions import TelegramBadRequest

from bot.handlers.favorites import (
    favorite_unsaved_markup,
    favorites_reply_keyboard,
)
from bot.i18n import t
from bot.services import downloader
from bot.services.search import SearchItem
from tests.conftest import callback_update, first_callback_data, text_update


def _session(video_id: str = "FavVideo001") -> dict:
    return {
        "header": "Results",
        "per_page": 5,
        "extras": False,
        "artist": None,
        "title": None,
        "listen_url": None,
        "owner_user_id": 100,
        "items": [{
            "video_id": video_id,
            "title": "Favorite Artist - Favorite Song",
            "duration": 201,
            "uploader": "Favorite Artist",
        }],
    }


def test_heart_callback_accepts_full_youtube_id_alphabet():
    markup = favorite_unsaved_markup(
        "yt:_Leading001", lambda key, **kwargs: t(key, "en", **kwargs)
    )
    data = markup.inline_keyboard[0][0].callback_data
    assert data == "fav:a:yt:_Leading001"
    assert len(data.encode("utf-8")) <= 64


def test_persistent_favorites_keyboard_is_exactly_localized():
    for locale, label in (
        ("uz", "❤️ Sevimlilar"),
        ("ru", "❤️ Избранное"),
        ("en", "❤️ Favorites"),
    ):
        markup = favorites_reply_keyboard(locale)
        assert markup.is_persistent is True
        assert markup.resize_keyboard is True
        assert len(markup.keyboard) == 1
        assert len(markup.keyboard[0]) == 1
        assert markup.keyboard[0][0].text == label


async def _deliver_cached_favorite(dp, bot, cap, storage) -> str:
    await storage.save_session("favsess", _session())
    cache_key = downloader.telegram_media_cache_key(
        bot.id,
        "https://www.youtube.com/watch?v=FavVideo001",
        "audio",
        media_id="FavVideo001",
    )
    await storage.set_cached_audio(cache_key, "CACHED_FAVORITE", "Favorite Song")
    await dp.feed_update(bot, callback_update("pick:favsess:0", uid=10))

    audio = cap.last("SendAudio")
    assert audio.audio == "CACHED_FAVORITE"
    callback_data = first_callback_data(audio.reply_markup, "fav:a:")
    assert callback_data == "fav:a:yt:FavVideo001"
    assert len(callback_data.encode("utf-8")) <= 64
    return callback_data


async def test_audio_heart_adds_idempotently_and_playlist_replays_instantly(
    dp, bot, cap, storage,
):
    add_data = await _deliver_cached_favorite(dp, bot, cap, storage)
    cap.methods.clear()

    await dp.feed_update(bot, callback_update(add_data, uid=11))

    answer = cap.last("AnswerCallbackQuery")
    assert "Added" in answer.text
    edit = cap.last("EditMessageReplyMarkup")
    assert first_callback_data(edit.reply_markup, "fav:h:") == (
        "fav:h:yt:FavVideo001"
    )
    assert await storage.count_favorites(100) == 1

    # A retried/duplicate add never toggles the favorite back off.
    cap.methods.clear()
    await dp.feed_update(bot, callback_update(add_data, uid=12))
    assert "already" in cap.last("AnswerCallbackQuery").text
    assert await storage.count_favorites(100) == 1

    cap.methods.clear()
    await dp.feed_update(bot, text_update("/favorites", uid=13))
    playlist = cap.last("SendMessage")
    assert "Favorites" in playlist.text
    play = first_callback_data(playlist.reply_markup, "fav:p:")
    delete = first_callback_data(playlist.reply_markup, "fav:d:")
    assert play == "fav:p:yt:FavVideo001"
    assert delete == "fav:d:yt:FavVideo001:0"

    cap.methods.clear()
    await dp.feed_update(bot, callback_update(play, uid=14))
    replay = cap.last("SendAudio")
    assert replay.audio == "CACHED_FAVORITE"
    assert first_callback_data(replay.reply_markup, "fav:h:")

    cap.methods.clear()
    await dp.feed_update(bot, callback_update(delete, uid=15))
    assert "Removed" in cap.last("AnswerCallbackQuery").text
    assert "empty" in cap.last("EditMessageText").text
    assert await storage.count_favorites(100) == 0

    # The red heart on an older audio remains a safe save action after removal.
    cap.methods.clear()
    await dp.feed_update(bot, callback_update("fav:h:yt:FavVideo001", uid=16))
    assert "Added" in cap.last("AnswerCallbackQuery").text
    assert await storage.count_favorites(100) == 1


async def test_fresh_track_upload_gets_heart_and_updates_reusable_file_id(
    dp, bot, cap, storage, config, monkeypatch,
):
    await storage.save_session("freshfav", _session("FreshFav001"))
    output = Path(config.download_dir) / "fresh-favorite.m4a"
    output.write_bytes(b"audio")

    class Result:
        path = str(output)
        title = "Fresh Artist - Fresh Song"
        uploader = "Fresh Artist"
        duration = 190
        ext = "m4a"

    async def download_audio(*args, **kwargs):
        return Result()

    monkeypatch.setattr(downloader, "download_audio", download_audio)
    await dp.feed_update(bot, callback_update("pick:freshfav:0", uid=20))

    sent = cap.last("SendAudio")
    assert first_callback_data(sent.reply_markup, "fav:a:") == (
        "fav:a:yt:FreshFav001"
    )
    catalog = await storage.get_favorite_track("yt:FreshFav001")
    assert catalog["file_id"].startswith("AUDIO_")
    assert catalog["title"] == "Fresh Artist - Fresh Song"
    assert not output.exists()


async def test_cached_social_audio_gets_restart_safe_heart(
    dp, bot, cap, storage,
):
    item = {
        "url": "https://www.instagram.com/reel/example/",
        "title": "Creator - Reel Song",
        "owner_user_id": 100,
        "media_id": "reel-123",
        "uploader": "Creator",
        "duration": 25,
    }
    await storage.save_session("socialfav", item)
    cache_key = downloader.telegram_media_cache_key(
        bot.id, item["url"], "audio", media_id=item["media_id"]
    )
    await storage.set_cached_audio(cache_key, "SOCIAL_AUDIO", item["title"])

    await dp.feed_update(bot, callback_update("dl:socialfav:audio", uid=30))

    sent = cap.last("SendAudio")
    add_data = first_callback_data(sent.reply_markup, "fav:a:")
    assert sent.audio == "SOCIAL_AUDIO"
    assert add_data and add_data.startswith("fav:a:m:")
    key = add_data.removeprefix("fav:a:")
    catalog = await storage.get_favorite_track(key)
    assert catalog["source_url"] == item["url"]
    assert catalog["file_id"] == "SOCIAL_AUDIO"


async def test_fresh_social_audio_gets_heart_and_catalog_file_id(
    dp, bot, cap, storage, config, monkeypatch,
):
    item = {
        "url": "https://www.instagram.com/reel/fresh-example/",
        "title": "Creator - Fresh Reel Song",
        "owner_user_id": 100,
        "media_id": "fresh-reel-456",
        "uploader": "Creator",
        "duration": 31,
    }
    await storage.save_session("freshsocial", item)
    output = Path(config.download_dir) / "fresh-social.m4a"
    output.write_bytes(b"audio")

    class Result:
        path = str(output)
        title = item["title"]
        uploader = item["uploader"]
        duration = item["duration"]
        ext = "m4a"

    async def download_audio(*args, **kwargs):
        return Result()

    monkeypatch.setattr(downloader, "download_audio", download_audio)
    await dp.feed_update(bot, callback_update("dl:freshsocial:audio", uid=35))

    sent = cap.last("SendAudio")
    add_data = first_callback_data(sent.reply_markup, "fav:a:")
    assert add_data and add_data.startswith("fav:a:m:")
    catalog = await storage.get_favorite_track(
        add_data.removeprefix("fav:a:")
    )
    assert catalog["file_id"].startswith("AUDIO_")
    assert not output.exists()


async def test_favorite_callbacks_are_private_and_user_scoped(
    dp, bot, cap, storage,
):
    add_data = await _deliver_cached_favorite(dp, bot, cap, storage)
    await dp.feed_update(bot, callback_update(add_data, uid=40))
    cap.methods.clear()

    await dp.feed_update(
        bot,
        callback_update(
            "fav:p:yt:FavVideo001", uid=41, user_id=200, chat_id=200
        ),
    )
    assert cap.last("SendAudio") is None
    assert cap.last("AnswerCallbackQuery").show_alert is True

    cap.methods.clear()
    await dp.feed_update(
        bot,
        callback_update(
            "fav:d:yt:FavVideo001:0", uid=42, user_id=200, chat_id=200
        ),
    )
    assert await storage.count_favorites(100) == 1
    assert await storage.count_favorites(200) == 0


async def test_group_track_delivery_does_not_show_personal_heart(
    dp, bot, cap, storage,
):
    await storage.save_session("groupfav", _session())
    cache_key = downloader.telegram_media_cache_key(
        bot.id,
        "https://www.youtube.com/watch?v=FavVideo001",
        "audio",
        media_id="FavVideo001",
    )
    await storage.set_cached_audio(cache_key, "GROUP_AUDIO", "Song")

    await dp.feed_update(
        bot,
        callback_update(
            "pick:groupfav:0",
            uid=45,
            user_id=100,
            chat_id=-100123,
            chat_type="supergroup",
        ),
    )

    assert cap.last("SendAudio").reply_markup is None
    assert await storage.get_favorite_track("yt:FavVideo001") is None


async def test_invalid_saved_file_id_falls_back_without_losing_favorite(
    dp, bot, cap, storage, config, monkeypatch,
):
    key = await storage.upsert_favorite_track(
        {
            "video_id": "Fallback001",
            "title": "Fallback Artist - Fallback Song",
            "duration": 180,
            "uploader": "Fallback Artist",
        },
        file_id="BROKEN_AUDIO",
    )
    await storage.add_favorite(100, key)
    output = Path(config.download_dir) / "favorite-fallback.m4a"
    output.write_bytes(b"audio")

    class Result:
        path = str(output)
        title = "Fallback Artist - Fallback Song"
        uploader = "Fallback Artist"
        duration = 180
        ext = "m4a"

    async def download_audio(*args, **kwargs):
        return Result()

    monkeypatch.setattr(downloader, "download_audio", download_audio)
    original_request = bot.session.make_request

    async def reject_old_file(bot_, method, timeout=None):
        if type(method).__name__ == "SendAudio" and method.audio == "BROKEN_AUDIO":
            raise TelegramBadRequest(
                method=method, message="Bad Request: wrong file identifier"
            )
        return await original_request(bot_, method, timeout=timeout)

    bot.session.make_request = reject_old_file
    await dp.feed_update(bot, callback_update(f"fav:p:{key}", uid=46))

    sent = cap.last("SendAudio")
    assert sent is not None and sent.audio != "BROKEN_AUDIO"
    assert await storage.is_favorite(100, key) is True
    assert (await storage.get_favorite(100, key))["file_id"].startswith(
        "AUDIO_"
    )


async def test_favorites_pagination_is_bounded_and_clamps_after_removal(
    dp, bot, cap, storage,
):
    for index in range(11):
        key = await storage.upsert_favorite_track({
            "video_id": f"PageFav{index:04d}",
            "title": f"Artist {index} - Song {index}",
        })
        await storage.add_favorite(100, key)

    await dp.feed_update(bot, text_update("/sevimlilar", uid=50))
    first = cap.last("SendMessage")
    assert len(first.reply_markup.inline_keyboard) == 11
    next_page = first_callback_data(first.reply_markup, "fav:g:1")
    assert next_page == "fav:g:1"

    cap.methods.clear()
    await dp.feed_update(bot, callback_update(next_page, uid=51))
    second = cap.last("EditMessageText")
    plays = [
        button.callback_data
        for row in second.reply_markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("fav:p:")
    ]
    assert len(plays) == 1
    delete = first_callback_data(second.reply_markup, "fav:d:")

    cap.methods.clear()
    await dp.feed_update(bot, callback_update(delete, uid=52))
    clamped = cap.last("EditMessageText")
    assert "10" in clamped.text
    assert await storage.count_favorites(100) == 10


async def test_malformed_favorite_callback_fails_closed(dp, bot, cap):
    await dp.feed_update(bot, callback_update("fav:a:bad key", uid=60))
    answer = cap.last("AnswerCallbackQuery")
    assert answer.show_alert is True
    assert cap.last("SendAudio") is None


async def test_localized_persistent_button_opens_playlist_and_never_broadcasts(
    dp, bot, cap, storage,
):
    labels = (
        (101, "uz", "❤️ Sevimlilar"),
        (102, "ru", "❤️ Избранное"),
        (103, "en", "❤️ Favorites"),
        (7645204689, "uz", "❤️ Sevimlilar"),
        (7645204689, "uz", "❤️ Избранное"),
        (7645204689, "uz", "❤️ Favorites"),
    )
    for uid, locale, label in labels:
        await storage.set_locale(uid, locale)
        cap.methods.clear()
        await dp.feed_update(
            bot, text_update(label, uid=70 + uid % 10, user_id=uid, chat_id=uid)
        )
        assert cap.last("SendMessage") is not None
        assert cap.last("CopyMessage") is None
        assert cap.last("SendAudio") is None
