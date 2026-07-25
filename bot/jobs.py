"""Small in-process guard for expensive media jobs.

Railway currently runs one polling process, so an event-loop-local claim is
enough to stop repeated button taps and a multi-user burst from launching
unbounded yt-dlp/ffmpeg/fingerprinting work. A Redis-backed claim can replace
this module if the bot is scaled out.
"""

_ACTIVE_USERS: set[int] = set()
_MAX_ACTIVE = 3


def configure(max_active: int) -> None:
    """Set this process's heavy-job ceiling after startup validation."""
    global _MAX_ACTIVE
    if max_active <= 0:
        raise ValueError("max_active must be positive")
    _MAX_ACTIVE = max_active


def try_claim(user_id: int) -> str | None:
    """Claim a slot, returning a localized error key when it is unavailable."""
    if user_id in _ACTIVE_USERS:
        return "already_processing"
    if len(_ACTIVE_USERS) >= _MAX_ACTIVE:
        return "service_busy"
    _ACTIVE_USERS.add(user_id)
    return None


def claim(user_id: int) -> bool:
    """Compatibility wrapper; new handlers should use :func:`try_claim`."""
    return try_claim(user_id) is None


def release(user_id: int) -> None:
    _ACTIVE_USERS.discard(user_id)


def is_active(user_id: int) -> bool:
    return user_id in _ACTIVE_USERS


def clear() -> None:
    """Test/reset hook; production code should release individual claims."""
    _ACTIVE_USERS.clear()
