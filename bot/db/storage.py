"""SQLite storage: per-user locale + a song file_id cache.

The file_id cache is the money-saver: once a song has been sent once, Telegram
gives us a `file_id` we can re-send instantly, for free, with no re-download.
"""
import json

import aiosqlite


class Storage:
    def __init__(self, path: str = "musiqa.db"):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            " user_id INTEGER PRIMARY KEY,"
            " locale TEXT)"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS audio_cache ("
            " key TEXT PRIMARY KEY,"
            " file_id TEXT NOT NULL,"
            " title TEXT,"
            " created_at TEXT DEFAULT (datetime('now')))"
        )
        # button sessions (results lists, pending links) — persisted so inline
        # buttons keep working across bot restarts instead of "expiring".
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            " token TEXT PRIMARY KEY,"
            " data TEXT NOT NULL,"
            " created_at TEXT DEFAULT (datetime('now')))"
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()

    # ── locale ──────────────────────────────────────────
    async def get_locale(self, user_id: int) -> str | None:
        async with self._db.execute(
            "SELECT locale FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_locale(self, user_id: int, locale: str) -> None:
        await self._db.execute(
            "INSERT INTO users (user_id, locale) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET locale = excluded.locale",
            (user_id, locale),
        )
        await self._db.commit()

    # ── audio cache (used from Feature A onward) ─────────
    async def get_cached_audio(self, key: str) -> str | None:
        async with self._db.execute(
            "SELECT file_id FROM audio_cache WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_cached_audio(self, key: str, file_id: str, title: str = "") -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO audio_cache (key, file_id, title) VALUES (?, ?, ?)",
            (key, file_id, title),
        )
        await self._db.commit()

    # ── button sessions (survive restarts) ──────────────
    async def save_session(self, token: str, data: dict) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO sessions (token, data) VALUES (?, ?)",
            (token, json.dumps(data)),
        )
        # keep the table bounded (most-recent 5000 tokens)
        await self._db.execute(
            "DELETE FROM sessions WHERE token NOT IN "
            "(SELECT token FROM sessions ORDER BY created_at DESC LIMIT 5000)"
        )
        await self._db.commit()

    async def get_session(self, token: str) -> dict | None:
        async with self._db.execute(
            "SELECT data FROM sessions WHERE token = ?", (token,)
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else None
