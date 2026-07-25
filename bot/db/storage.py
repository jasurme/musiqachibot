"""SQLite storage for locale, bounded sessions/recognition, and file_id cache.

The file_id cache is the money-saver: once a song has been sent once, Telegram
gives us a `file_id` we can re-send instantly, for free, with no re-download.
"""
import json
import logging

import aiosqlite

logger = logging.getLogger(__name__)


class Storage:
    def __init__(
        self, path: str = "musiqa.db", session_ttl_days: int = 7,
        recognition_ttl_days: int = 30,
    ):
        self.path = path
        self.session_ttl_days = max(1, session_ttl_days)
        self.recognition_ttl_days = max(1, recognition_ttl_days)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            await self._db.execute("PRAGMA journal_mode = WAL")
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
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS recognition_cache ("
            " key TEXT PRIMARY KEY,"
            " title TEXT NOT NULL,"
            " artist TEXT NOT NULL,"
            " url TEXT,"
            " cover TEXT,"
            " owner_user_id INTEGER,"
            " created_at TEXT DEFAULT (datetime('now')))"
        )
        # Safe forward migration for databases created by older releases.
        async with self._db.execute("PRAGMA table_info(recognition_cache)") as cur:
            recognition_columns = {row[1] for row in await cur.fetchall()}
        if "owner_user_id" not in recognition_columns:
            await self._db.execute(
                "ALTER TABLE recognition_cache ADD COLUMN owner_user_id INTEGER"
            )
        # button sessions (results lists, pending links) — persisted so inline
        # buttons keep working across bot restarts instead of "expiring".
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            " token TEXT PRIMARY KEY,"
            " data TEXT NOT NULL,"
            " created_at TEXT DEFAULT (datetime('now')))"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_created_at "
            "ON sessions(created_at)"
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

    async def delete_cached_audio(self, key: str) -> None:
        await self._db.execute("DELETE FROM audio_cache WHERE key = ?", (key,))
        await self._db.commit()

    # ── recognition cache ──────────────────────────────────
    async def get_recognition(
        self, key: str, owner_user_id: int | None = None,
    ) -> dict | None:
        async with self._db.execute(
            "SELECT title, artist, url, cover FROM recognition_cache "
            "WHERE key = ? AND created_at >= datetime('now', ?)",
            (key, f"-{self.recognition_ttl_days} days"),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        if owner_user_id is not None:
            await self._db.execute(
                "UPDATE recognition_cache SET owner_user_id = ? WHERE key = ?",
                (owner_user_id, key),
            )
            await self._db.commit()
        return {"title": row[0], "artist": row[1], "url": row[2], "cover": row[3]}

    async def set_recognition(
        self, key: str, title: str, artist: str,
        url: str | None = None, cover: str | None = None,
        owner_user_id: int | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO recognition_cache "
            "(key, title, artist, url, cover, owner_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, title, artist, url, cover, owner_user_id),
        )
        await self._db.execute(
            "DELETE FROM recognition_cache WHERE created_at < datetime('now', ?)",
            (f"-{self.recognition_ttl_days} days",),
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
            "DELETE FROM sessions WHERE created_at < datetime('now', ?)",
            (f"-{self.session_ttl_days} days",),
        )
        await self._db.execute(
            "DELETE FROM sessions WHERE token NOT IN "
            "(SELECT token FROM sessions ORDER BY created_at DESC LIMIT 5000)"
        )
        await self._db.commit()

    async def get_session(self, token: str) -> dict | None:
        async with self._db.execute(
            "SELECT data FROM sessions "
            "WHERE token = ? AND created_at >= datetime('now', ?)",
            (token, f"-{self.session_ttl_days} days"),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            logger.warning("Discarding invalid JSON session token=%s", token)
            return None
        return data if isinstance(data, dict) else None

    async def delete_user_data(self, user_id: int) -> int:
        """Delete directly-associated preference, recognition, and session data."""
        async with self._db.execute("SELECT token, data FROM sessions") as cur:
            rows = await cur.fetchall()
        owned_tokens = []
        for token, raw_data in rows:
            try:
                data = json.loads(raw_data)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("owner_user_id") == user_id:
                owned_tokens.append((token,))
        if owned_tokens:
            await self._db.executemany(
                "DELETE FROM sessions WHERE token = ?", owned_tokens
            )
        user_cursor = await self._db.execute(
            "DELETE FROM users WHERE user_id = ?", (user_id,)
        )
        recognition_cursor = await self._db.execute(
            "DELETE FROM recognition_cache WHERE owner_user_id = ?", (user_id,)
        )
        await self._db.commit()
        return (
            len(owned_tokens)
            + max(0, user_cursor.rowcount)
            + max(0, recognition_cursor.rowcount)
        )
