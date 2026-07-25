"""Resolve the user's locale once per update and inject `_` + `locale`."""
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from bot.i18n import DEFAULT, SUPPORTED, t


class I18nMiddleware(BaseMiddleware):
    def __init__(self, db, default_locale: str = DEFAULT):
        self.db = db
        self.default_locale = default_locale if default_locale in SUPPORTED else DEFAULT

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        locale = self.default_locale
        if user is not None:
            stored = await self.db.get_locale(user.id)
            if stored in SUPPORTED:
                locale = stored  # ...unless they've explicitly chosen a language

        data["locale"] = locale
        data["_"] = lambda key, **kw: t(key, locale, **kw)
        return await handler(event, data)
