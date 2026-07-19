from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.i18n import t

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


@router.callback_query(F.data.startswith("setlang:"))
async def set_language(callback: CallbackQuery, db, **kwargs):
    locale = callback.data.split(":", 1)[1]
    await db.set_locale(callback.from_user.id, locale)
    await callback.answer(t("language_set", locale))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(t("welcome", locale))
