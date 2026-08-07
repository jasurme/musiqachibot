"""Dispatcher-level UI tests for stored music campaigns and preferences."""

from __future__ import annotations

from types import MethodType

import pytest

from bot.db.storage import (
    NOTIFY_ALL_MUSIC,
    NOTIFY_DISCOVERIES,
    NOTIFY_NEW_MUSIC,
    NOTIFY_RISING_MUSIC,
)
from bot.handlers.music_campaigns import (
    campaign_list_keyboard,
    mood_menu_keyboard,
    notification_keyboard,
)
from bot.i18n import t
from bot.main import _set_commands
from tests.conftest import callback_update, text_update


def _items(count: int, prefix: str = "track") -> list[dict]:
    return [
        {
            "source_id": f"source-{prefix}-{index}",
            "artist": f"Artist {index}",
            "name": f"Song {index}",
            "video_id": f"{prefix}{index:02d}",
            "title": f"Artist {index} — Song {index}",
            "apple_url": "https://hidden.invalid/source",
            "duration": 180 + index,
            "uploader": f"Channel {index}",
        }
        for index in range(count)
    ]


def _install_collections(storage, values: dict[str, list[dict]]) -> None:
    async def get_music_collection_state(self, key: str):
        return {"collection_key": key, "items": values.get(key, [])}

    storage.get_music_collection_state = MethodType(
        get_music_collection_state, storage
    )


@pytest.mark.parametrize(
    ("command", "key", "header", "include_moods"),
    [
        ("/new_music", "new_music", "<b>🔥 New Music</b>", True),
        ("/rising", "rising", "<b>🚀 Rising Now</b>", False),
        (
            "/discoveries", "discoveries",
            "<b>💎 Weekly Discoveries</b>", True,
        ),
    ],
)
async def test_editorial_commands_are_single_snapshot_only_responses(
    dp, bot, cap, storage, command, key, header, include_moods,
):
    _install_collections(storage, {key: _items(5, key.replace("_", ""))})

    await dp.feed_update(bot, text_update(command))

    messages = cap.by("SendMessage")
    assert len(messages) == 1
    response = messages[0]
    assert response.text == header
    rows = response.reply_markup.inline_keyboard
    assert len(rows[:5]) == 5
    assert all(len(row) == 1 for row in rows[:5])
    assert all(row[0].callback_data.startswith("topdl:") for row in rows[:5])
    assert any(
        button.callback_data == "moods:open"
        for row in rows for button in row
    ) is include_moods
    assert all(
        hidden not in response.text.lower()
        for hidden in ("uzbekistan", "oʻzbekiston", "source", "manba", "http")
    )
    assert cap.by("CopyMessage") == []


async def test_incomplete_editorial_snapshot_has_one_final_error(
    dp, bot, cap, storage,
):
    _install_collections(storage, {"new_music": _items(4)})
    await dp.feed_update(bot, text_update("/new_music"))

    assert len(cap.by("SendMessage")) == 1
    assert "not ready" in cap.last("SendMessage").text


async def test_campaign_song_button_directly_uses_existing_audio_delivery(
    dp, bot, cap, storage,
):
    _install_collections(storage, {"new_music": _items(5, "newmusic")})
    await storage.set_cached_audio(
        f"bot:{bot.id}:ytaudio:newmusic00", "READY_FILE_ID", "Ready song"
    )

    await dp.feed_update(bot, text_update("/new_music", uid=10))
    button = cap.last("SendMessage").reply_markup.inline_keyboard[0][0]
    await dp.feed_update(bot, callback_update(button.callback_data, uid=11))

    audio = cap.last("SendAudio")
    assert audio is not None and audio.audio == "READY_FILE_ID"
    assert cap.last("AnswerCallbackQuery").text is None


async def test_mood_menu_and_ten_song_view_have_no_progress_message(
    dp, bot, cap, storage,
):
    _install_collections(storage, {"mood:night": _items(10, "night")})

    await dp.feed_update(bot, text_update("/moods"))
    menu = cap.last("SendMessage")
    assert menu.text == "<b>🎭 Choose a mood</b>"
    assert [row[0].callback_data for row in menu.reply_markup.inline_keyboard] == [
        "mood:night", "mood:road", "mood:workout", "mood:calm",
        "mood:weekend",
    ]

    before_messages = len(cap.by("SendMessage"))
    await dp.feed_update(bot, callback_update("mood:night", uid=20))

    assert len(cap.by("SendMessage")) == before_messages
    edited = cap.last("EditMessageText")
    assert edited.text == "<b>🌙 Night Vibes</b>"
    rows = edited.reply_markup.inline_keyboard
    assert len(rows[:10]) == 10
    assert all(len(row) == 1 for row in rows[:10])
    assert [row[0].callback_data for row in rows[:10]] == [
        f"topdl:night{index:02d}" for index in range(10)
    ]
    assert rows[-1][0].callback_data == "mood:menu"
    assert cap.last("AnswerCallbackQuery").text is None


async def test_open_moods_from_campaign_sends_only_the_final_menu(
    dp, bot, cap,
):
    await dp.feed_update(bot, callback_update("moods:open", uid=30))

    assert cap.last("AnswerCallbackQuery").text is None
    message = cap.last("SendMessage")
    assert message.text == "<b>🎭 Choose a mood</b>"
    assert len(message.reply_markup.inline_keyboard) == 5


async def test_unavailable_mood_uses_callback_alert_without_chat_noise(
    dp, bot, cap, storage,
):
    _install_collections(storage, {"mood:night": _items(9, "night")})
    await dp.feed_update(bot, callback_update("mood:night", uid=40))

    answer = cap.last("AnswerCallbackQuery")
    assert "not ready" in answer.text
    assert answer.show_alert is True
    assert cap.by("SendMessage") == []
    assert cap.by("EditMessageText") == []


async def test_notification_preferences_have_three_categories_and_persist(
    dp, bot, cap, storage,
):
    await dp.feed_update(bot, text_update("/notifications", user_id=77))
    settings = cap.last("SendMessage")
    rows = settings.reply_markup.inline_keyboard
    assert [row[0].callback_data for row in rows[:3]] == [
        "notify:toggle:new",
        "notify:toggle:rising",
        "notify:toggle:discoveries",
    ]
    assert all(row[0].text.startswith("✅") for row in rows[:3])
    assert [button.callback_data for button in rows[-1]] == [
        "notify:all:on", "notify:all:off",
    ]
    assert await storage.get_notification_mask(77) == NOTIFY_ALL_MUSIC

    await dp.feed_update(
        bot, callback_update("notify:toggle:new", uid=51, user_id=77)
    )
    assert await storage.get_notification_mask(77) == (
        NOTIFY_RISING_MUSIC | NOTIFY_DISCOVERIES
    )
    toggled = cap.last("EditMessageText").reply_markup.inline_keyboard
    assert toggled[0][0].text.startswith("❌")

    await dp.feed_update(
        bot, callback_update("notify:all:off", uid=52, user_id=77)
    )
    assert await storage.get_notification_mask(77) == 0
    await dp.feed_update(
        bot, callback_update("notify:all:on", uid=53, user_id=77)
    )
    assert await storage.get_notification_mask(77) == NOTIFY_ALL_MUSIC


async def test_campaign_opt_out_is_idempotent_and_sends_no_extra_message(
    dp, bot, cap, storage,
):
    await storage.touch_private_user(88)
    for uid in (60, 61):
        await dp.feed_update(
            bot, callback_update("notify:off:rising", uid=uid, user_id=88)
        )

    assert await storage.get_notification_mask(88) == (
        NOTIFY_NEW_MUSIC | NOTIFY_DISCOVERIES
    )
    assert cap.by("SendMessage") == []
    assert cap.by("EditMessageText") == []
    assert len(cap.by("AnswerCallbackQuery")) == 2


def test_campaign_keyboards_are_bounded_and_localized_in_all_languages():
    items = _items(10, "durable")
    for locale in ("uz", "ru", "en"):
        translate = lambda key, _locale=locale, **kw: t(
            key, _locale, **kw
        )
        songs = campaign_list_keyboard(items, translate)
        callbacks = [row[0].callback_data for row in songs.inline_keyboard]
        assert len(callbacks) == 10
        assert all(value.startswith("topdl:") for value in callbacks)
        assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)
        assert all(len(row[0].text) <= 100 for row in songs.inline_keyboard)
        assert len(mood_menu_keyboard(translate).inline_keyboard) == 5
        preferences = notification_keyboard(NOTIFY_ALL_MUSIC, translate)
        assert len(preferences.inline_keyboard) == 4
        assert all(
            key not in preferences.model_dump_json()
            for key in ("notification_top", "notification_moods")
        )


def test_all_campaign_locale_keys_have_real_parity():
    keys = {
        "new_music_header", "rising_header", "discoveries_header",
        "moods_header", "mood_night", "mood_road", "mood_workout",
        "mood_calm", "mood_weekend", "mood_night_header",
        "mood_road_header", "mood_workout_header", "mood_calm_header",
        "mood_weekend_header", "music_campaign_unavailable", "btn_back",
        "btn_open_moods", "btn_notifications_off",
        "btn_notification_settings", "notifications_header",
        "notification_new_music", "notification_rising",
        "notification_discoveries", "btn_notifications_all_on",
        "btn_notifications_all_off", "notifications_saved",
        "notifications_category_disabled", "notifications_all_enabled",
        "notifications_all_disabled",
    }
    for locale in ("uz", "ru", "en"):
        assert all(t(key, locale) != key for key in keys)


async def test_bot_command_menu_exposes_every_campaign_in_all_languages():
    calls = []

    class FakeBot:
        async def set_my_commands(self, commands, **kwargs):
            calls.append((commands, kwargs.get("language_code")))

    await _set_commands(FakeBot(), "uz")

    assert [language for _commands, language in calls] == [None, "uz", "ru", "en"]
    required = {
        "new_music", "rising", "discoveries", "moods", "notifications",
    }
    for commands, _language in calls:
        assert required <= {command.command for command in commands}
