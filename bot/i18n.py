"""Tiny dict-based translation lookup for uz / ru / en."""
from locales import en, ru, uz

TRANSLATIONS = {"uz": uz.STRINGS, "ru": ru.STRINGS, "en": en.STRINGS}
SUPPORTED = ("uz", "ru", "en")
DEFAULT = "uz"


def t(key: str, locale: str, **kwargs) -> str:
    table = TRANSLATIONS.get(locale, TRANSLATIONS[DEFAULT])
    text = table.get(key)
    if text is None:
        text = TRANSLATIONS[DEFAULT].get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text
