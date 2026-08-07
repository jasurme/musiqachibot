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
            " locale TEXT,"
            " is_active INTEGER NOT NULL DEFAULT 1,"
            " deactivated_at TEXT)"
        )
        # Safe forward migration for the original two-column users table.
        async with self._db.execute("PRAGMA table_info(users)") as cur:
            user_columns = {row[1] for row in await cur.fetchall()}
        if "is_active" not in user_columns:
            await self._db.execute(
                "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
            )
        if "deactivated_at" not in user_columns:
            await self._db.execute(
                "ALTER TABLE users ADD COLUMN deactivated_at TEXT"
            )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_broadcast "
            "ON users(is_active, user_id)"
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
        # Earlier releases only recorded users who selected a language. Recover
        # every positive private owner ID still present in recognition/session
        # rows so the first deployment does not start with an empty audience.
        await self._db.execute(
            "INSERT OR IGNORE INTO users (user_id) "
            "SELECT DISTINCT owner_user_id FROM recognition_cache "
            "WHERE owner_user_id IS NOT NULL AND owner_user_id > 0"
        )
        async with self._db.execute("SELECT data FROM sessions") as cur:
            session_rows = await cur.fetchall()
        recovered_user_ids: set[int] = set()
        for (raw_data,) in session_rows:
            try:
                data = json.loads(raw_data)
                owner_user_id = data.get("owner_user_id")
                owner_user_id = int(owner_user_id)
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if owner_user_id > 0:
                recovered_user_ids.add(owner_user_id)
        if recovered_user_ids:
            await self._db.executemany(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                ((user_id,) for user_id in sorted(recovered_user_ids)),
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
            "ON CONFLICT(user_id) DO UPDATE SET "
            "locale = excluded.locale, is_active = 1, deactivated_at = NULL",
            (user_id, locale),
        )
        await self._db.commit()

    async def touch_private_user(self, user_id: int) -> str | None:
        """Register a private user once and reactivate them when they return.

        Active repeat users take a read-only path, avoiding a volume commit on
        every update. Telegram does not expose a historical bot-user list, so
        tracking private interactions is the authoritative broadcast audience.
        """
        async with self._db.execute(
            "SELECT locale, is_active FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await self._db.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
            )
            await self._db.commit()
            return None
        locale, is_active = row
        if not is_active:
            await self._db.execute(
                "UPDATE users SET is_active = 1, deactivated_at = NULL "
                "WHERE user_id = ?",
                (user_id,),
            )
            await self._db.commit()
        return locale

    async def get_active_user_ids(
        self, *, after_user_id: int = 0, limit: int = 500,
        exclude_user_id: int | None = None,
    ) -> list[int]:
        """Return one keyset-paged broadcast batch in stable user-ID order."""
        limit = max(1, min(int(limit), 1000))
        if exclude_user_id is None:
            query = (
                "SELECT user_id FROM users "
                "WHERE is_active = 1 AND user_id > ? "
                "ORDER BY user_id LIMIT ?"
            )
            params = (after_user_id, limit)
        else:
            query = (
                "SELECT user_id FROM users "
                "WHERE is_active = 1 AND user_id > ? AND user_id != ? "
                "ORDER BY user_id LIMIT ?"
            )
            params = (after_user_id, exclude_user_id, limit)
        async with self._db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [int(row[0]) for row in rows]

    async def mark_users_inactive(self, user_ids: list[int]) -> None:
        """Stop future broadcasts to blocked/deactivated Telegram accounts."""
        unique_ids = sorted({int(user_id) for user_id in user_ids if user_id > 0})
        if not unique_ids:
            return
        await self._db.executemany(
            "UPDATE users SET is_active = 0, deactivated_at = datetime('now') "
            "WHERE user_id = ?",
            ((user_id,) for user_id in unique_ids),
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
