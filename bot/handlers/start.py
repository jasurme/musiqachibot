from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import jobs
from bot.config import Config
from bot.i18n import SUPPORTED, t

router = Router(name="start")


def _lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🇺🇿 Oʻzbek", callback_data="setlang:uz"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="setlang:ru"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="setlang:en"),
        ]]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, _, **kwargs):
    await message.answer(_("welcome"), reply_markup=_lang_keyboard())


@router.message(Command("lang"))
async def cmd_lang(message: Message, _, **kwargs):
    await message.answer(_("choose_language"), reply_markup=_lang_keyboard())


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, _, config: Config, **kwargs):
    markup = None
    policy_url = config.privacy_policy_url
    if policy_url and urlparse(policy_url).scheme in {"http", "https"}:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=_("btn_privacy_policy"), url=policy_url)
        ]])
    await message.answer(_("privacy_notice"), reply_markup=markup)


@router.message(Command("delete_my_data"))
async def cmd_delete_my_data(message: Message, _, db, **kwargs):
    user_id = message.from_user.id
    if jobs.is_active(user_id):
        await message.answer(_("already_processing"))
        return
    await db.delete_user_data(user_id)
    # Remove persisted and process-local button/search state in the same turn.
    from bot.handlers import results, url_download
    from bot.services.search import clear_search_cache

    results.purge_owner(user_id)
    url_download.purge_owner(user_id)
    clear_search_cache()
    await message.answer(_("data_deleted"))


@router.callback_query(F.data.startswith("setlang:"))
async def set_language(callback: CallbackQuery, db, locale: str, **kwargs):
    requested_locale = callback.data.split(":", 1)[1]
    if requested_locale not in SUPPORTED:
        await callback.answer(t("invalid_action", locale), show_alert=True)
        return
    await db.set_locale(callback.from_user.id, requested_locale)
    await callback.answer(t("language_set", requested_locale))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(t("welcome", requested_locale))
