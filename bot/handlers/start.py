from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.handlers.favorites import favorites_reply_keyboard
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


def _is_private_user_chat(message: Message, user_id: int) -> bool:
    return message.chat.type == "private" and message.chat.id == user_id


@router.message(CommandStart())
async def cmd_start(message: Message, _, locale: str, **kwargs):
    await message.answer(_("welcome"), reply_markup=_lang_keyboard())
    if message.from_user and _is_private_user_chat(
        message, message.from_user.id
    ):
        await message.answer(
            _("favorites_intro"),
            reply_markup=favorites_reply_keyboard(locale),
        )


@router.message(Command("lang"))
async def cmd_lang(message: Message, _, **kwargs):
    await message.answer(_("choose_language"), reply_markup=_lang_keyboard())


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
    reply_markup = None
    if (
        isinstance(callback.message, Message)
        and _is_private_user_chat(callback.message, callback.from_user.id)
    ):
        reply_markup = favorites_reply_keyboard(requested_locale)
    await callback.message.answer(
        t("welcome", requested_locale), reply_markup=reply_markup
    )
