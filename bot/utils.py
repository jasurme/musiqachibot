"""Chat-type / mention helpers for group vs private behaviour."""
import re

GROUP_TYPES = ("group", "supergroup")


def is_group(message) -> bool:
    return message.chat.type in GROUP_TYPES


def is_mentioned(message, bot_username: str | None) -> bool:
    if not bot_username:
        return False
    text = message.text or message.caption or ""
    return f"@{bot_username}".lower() in text.lower()


def strip_mention(text: str | None, bot_username: str | None) -> str:
    text = text or ""
    if bot_username:
        text = re.sub(re.escape(f"@{bot_username}"), "", text, flags=re.IGNORECASE)
    return text.strip()
