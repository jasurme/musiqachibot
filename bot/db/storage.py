"""SQLite storage for locale, bounded sessions/recognition, and file_id cache.

The file_id cache is the money-saver: once a song has been sent once, Telegram
gives us a `file_id` we can re-send instantly, for free, with no re-download.
"""
import json
import logging
import secrets
import time
from hashlib import sha256

import aiosqlite

logger = logging.getLogger(__name__)

_DIRECT_BROADCAST_HISTORY_SECONDS = 24 * 60 * 60


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
        # Persistent short-lived drafts make admin media confirmation fail
        # closed across restarts and make double-clicks atomic.
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS broadcast_drafts ("
            " token TEXT PRIMARY KEY,"
            " owner_user_id INTEGER NOT NULL,"
            " control_chat_id INTEGER NOT NULL,"
            " control_message_id INTEGER NOT NULL,"
            " payload TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'choosing',"
            " expires_at REAL NOT NULL,"
            " created_at REAL NOT NULL,"
            " audience_upper_user_id INTEGER NOT NULL DEFAULT 0,"
            " broadcast_cursor INTEGER NOT NULL DEFAULT 0,"
            " broadcast_sent INTEGER NOT NULL DEFAULT 0,"
            " broadcast_inactive INTEGER NOT NULL DEFAULT 0,"
            " broadcast_failed INTEGER NOT NULL DEFAULT 0)"
        )
        # Safe forward migration for drafts created before resumable manual
        # campaigns were introduced. A confirmed campaign always writes its
        # frozen audience boundary before entering ``sending``.
        async with self._db.execute(
            "PRAGMA table_info(broadcast_drafts)"
        ) as cur:
            draft_columns = {row[1] for row in await cur.fetchall()}
        draft_migrations = {
            "audience_upper_user_id": (
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "broadcast_cursor": "INTEGER NOT NULL DEFAULT 0",
            "broadcast_sent": "INTEGER NOT NULL DEFAULT 0",
            "broadcast_inactive": "INTEGER NOT NULL DEFAULT 0",
            "broadcast_failed": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in draft_migrations.items():
            if column not in draft_columns:
                await self._db.execute(
                    f"ALTER TABLE broadcast_drafts ADD COLUMN {column} "
                    f"{declaration}"
                )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_broadcast_drafts_owner "
            "ON broadcast_drafts(owner_user_id, expires_at)"
        )
        # One atomically replaced Top 10 snapshot plus its refresh/broadcast
        # cursor.  Keeping the ten small records in one JSON value means a
        # reader can only ever observe the complete old chart or the complete
        # new chart -- never a half-written ranking during a refresh.
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS top_music_state ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " generation INTEGER NOT NULL DEFAULT 0,"
            " items_json TEXT NOT NULL DEFAULT '[]',"
            " resolutions_json TEXT NOT NULL DEFAULT '{}',"
            " fingerprint TEXT,"
            " refreshed_at INTEGER,"
            " next_refresh_at INTEGER NOT NULL DEFAULT 0,"
            " failure_count INTEGER NOT NULL DEFAULT 0,"
            " refresh_lease_token TEXT,"
            " refresh_lease_until INTEGER,"
            " pending_generation INTEGER,"
            " broadcast_cursor INTEGER NOT NULL DEFAULT 0,"
            " broadcast_sent INTEGER NOT NULL DEFAULT 0,"
            " broadcast_inactive INTEGER NOT NULL DEFAULT 0,"
            " broadcast_failed INTEGER NOT NULL DEFAULT 0,"
            " broadcast_upper_user_id INTEGER)"
        )
        await self._db.execute(
            "INSERT OR IGNORE INTO top_music_state (id) VALUES (1)"
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
        through_user_id: int | None = None,
    ) -> list[int]:
        """Return one keyset-paged broadcast batch in stable user-ID order."""
        limit = max(1, min(int(limit), 1000))
        clauses = ["is_active = 1", "user_id > ?"]
        params: list[int] = [int(after_user_id)]
        if exclude_user_id is not None:
            clauses.append("user_id != ?")
            params.append(int(exclude_user_id))
        if through_user_id is not None:
            clauses.append("user_id <= ?")
            params.append(int(through_user_id))
        query = (
            "SELECT user_id FROM users WHERE " + " AND ".join(clauses)
            + " ORDER BY user_id LIMIT ?"
        )
        params.append(limit)
        async with self._db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [int(row[0]) for row in rows]

    async def get_active_user_upper_bound(
        self, *, exclude_user_id: int | None = None,
    ) -> int:
        """Freeze the upper edge of a new keyset-paged broadcast audience."""
        if exclude_user_id is None:
            query = "SELECT COALESCE(MAX(user_id), 0) FROM users WHERE is_active = 1"
            params = ()
        else:
            query = (
                "SELECT COALESCE(MAX(user_id), 0) FROM users "
                "WHERE is_active = 1 AND user_id != ?"
            )
            params = (int(exclude_user_id),)
        async with self._db.execute(query, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

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

    # ── confirmed administrator media drafts ─────────────────
    async def create_broadcast_draft(
        self,
        token: str,
        owner_user_id: int,
        control_chat_id: int,
        control_message_id: int,
        payload: dict,
        expires_at: float,
    ) -> None:
        """Persist a bounded, short-lived draft in its initial state."""
        if not token or not isinstance(payload, dict):
            raise ValueError("invalid broadcast draft")
        now = time.time()
        if float(expires_at) <= now:
            raise ValueError("broadcast draft expiry must be in the future")
        raw_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )
        await self._db.execute(
            "INSERT INTO broadcast_drafts "
            "(token, owner_user_id, control_chat_id, control_message_id, "
            "payload, status, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'choosing', ?, ?)",
            (
                token,
                int(owner_user_id),
                int(control_chat_id),
                int(control_message_id),
                raw_payload,
                float(expires_at),
                now,
            ),
        )
        await self._db.execute(
            "DELETE FROM broadcast_drafts WHERE expires_at <= ? "
            "AND status != 'sending'",
            (now,),
        )
        await self._db.execute(
            "DELETE FROM broadcast_drafts WHERE status != 'sending' "
            "AND token NOT IN "
            "(SELECT token FROM broadcast_drafts "
            "WHERE status != 'sending' ORDER BY created_at DESC LIMIT 100)"
        )
        await self._db.commit()

    async def create_direct_broadcast(
        self,
        token: str,
        owner_user_id: int,
        source_chat_id: int,
        source_message_id: int,
        content_type: str,
    ) -> None:
        """Persist an immediately confirmed copy campaign before delivery.

        Direct administrator announcements have no confirmation control. The
        source message doubles as the durable campaign binding, while
        ``delivery_kind`` keeps restart delivery distinct from media campaigns
        that intentionally add the Top Music button. The INSERT's scalar
        subquery freezes the active audience edge in the same SQLite statement.
        """
        if not token or not isinstance(content_type, str) or not content_type:
            raise ValueError("invalid direct broadcast")
        now = time.time()
        payload = {
            "delivery_kind": "copy",
            "source_chat_id": int(source_chat_id),
            "source_message_id": int(source_message_id),
            "content_type": content_type,
        }
        await self._db.execute(
            "INSERT INTO broadcast_drafts "
            "(token, owner_user_id, control_chat_id, control_message_id, "
            "payload, status, expires_at, created_at, "
            "audience_upper_user_id) "
            "VALUES (?, ?, ?, ?, ?, 'sending', ?, ?, "
            "(SELECT COALESCE(MAX(user_id), 0) FROM users "
            "WHERE is_active = 1 AND user_id != ?))",
            (
                token,
                int(owner_user_id),
                int(source_chat_id),
                int(source_message_id),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                now + _DIRECT_BROADCAST_HISTORY_SECONDS,
                now,
                int(owner_user_id),
            ),
        )
        await self._db.execute(
            "DELETE FROM broadcast_drafts WHERE expires_at <= ? "
            "AND status != 'sending'",
            (now,),
        )
        await self._db.execute(
            "DELETE FROM broadcast_drafts WHERE status != 'sending' "
            "AND token NOT IN "
            "(SELECT token FROM broadcast_drafts "
            "WHERE status != 'sending' ORDER BY created_at DESC LIMIT 100)"
        )
        await self._db.commit()

    async def get_broadcast_draft(self, token: str) -> dict | None:
        async with self._db.execute(
            "SELECT token, owner_user_id, control_chat_id, "
            "control_message_id, payload, status, expires_at, created_at, "
            "audience_upper_user_id, broadcast_cursor, broadcast_sent, "
            "broadcast_inactive, broadcast_failed "
            "FROM broadcast_drafts WHERE token = ?",
            (token,),
        ) as cur:
            row = await cur.fetchone()
        return self._decode_broadcast_draft_row(row, token=token)

    @staticmethod
    def _decode_broadcast_draft_row(
        row, *, token: str | None = None,
    ) -> dict | None:
        if not row:
            return None
        try:
            payload = json.loads(row[4])
        except (TypeError, json.JSONDecodeError):
            logger.warning(
                "Discarding invalid broadcast draft token=%s", token or row[0]
            )
            return None
        if not isinstance(payload, dict):
            return None
        return {
            "token": row[0],
            "owner_user_id": int(row[1]),
            "control_chat_id": int(row[2]),
            "control_message_id": int(row[3]),
            "payload": payload,
            "status": row[5],
            "expires_at": float(row[6]),
            "created_at": float(row[7]),
            "audience_upper_user_id": int(row[8]),
            "broadcast_cursor": int(row[9]),
            "broadcast_sent": int(row[10]),
            "broadcast_inactive": int(row[11]),
            "broadcast_failed": int(row[12]),
        }

    async def list_sending_broadcast_drafts(
        self, owner_user_id: int,
    ) -> list[dict]:
        """Return confirmed manual campaigns that need delivery or resumption."""
        async with self._db.execute(
            "SELECT token, owner_user_id, control_chat_id, "
            "control_message_id, payload, status, expires_at, created_at, "
            "audience_upper_user_id, broadcast_cursor, broadcast_sent, "
            "broadcast_inactive, broadcast_failed FROM broadcast_drafts "
            "WHERE owner_user_id = ? AND status = 'sending' "
            "ORDER BY created_at, token",
            (int(owner_user_id),),
        ) as cur:
            rows = await cur.fetchall()
        drafts = []
        for row in rows:
            draft = self._decode_broadcast_draft_row(row)
            if draft is not None:
                drafts.append(draft)
        return drafts

    async def begin_broadcast_draft(
        self,
        token: str,
        owner_user_id: int,
        control_message_id: int,
        audience_upper_user_id: int,
    ) -> bool:
        """Atomically confirm a draft and persist its frozen audience edge."""
        cursor = await self._db.execute(
            "UPDATE broadcast_drafts SET status = 'sending', "
            "audience_upper_user_id = ?, broadcast_cursor = 0, "
            "broadcast_sent = 0, broadcast_inactive = 0, "
            "broadcast_failed = 0 WHERE token = ? AND owner_user_id = ? "
            "AND control_message_id = ? AND status = 'awaiting_confirmation' "
            "AND expires_at > ?",
            (
                max(0, int(audience_upper_user_id)),
                token,
                int(owner_user_id),
                int(control_message_id),
                time.time(),
            ),
        )
        await self._db.commit()
        return cursor.rowcount == 1

    async def rebind_broadcast_draft_control(
        self,
        token: str,
        owner_user_id: int,
        old_control_message_id: int,
        new_control_message_id: int,
    ) -> bool:
        """Atomically bind a live confirmation to a replacement message."""
        cursor = await self._db.execute(
            "UPDATE broadcast_drafts SET control_message_id = ? "
            "WHERE token = ? AND owner_user_id = ? "
            "AND control_message_id = ? "
            "AND status = 'awaiting_confirmation' AND expires_at > ?",
            (
                int(new_control_message_id),
                token,
                int(owner_user_id),
                int(old_control_message_id),
                time.time(),
            ),
        )
        await self._db.commit()
        return cursor.rowcount == 1

    async def checkpoint_broadcast_draft(
        self,
        token: str,
        owner_user_id: int,
        user_id: int,
        outcome: str,
    ) -> bool:
        """Durably advance one confirmed campaign after a terminal outcome.

        The cursor predicate makes a repeated checkpoint idempotent. Delivery
        itself cannot be transactionally coupled to Telegram, so a process
        crash after Telegram accepts a send but before this commit can repeat
        at most that uncheckpointed recipient on the next startup.
        """
        columns = {
            "sent": "broadcast_sent",
            "inactive": "broadcast_inactive",
            "failed": "broadcast_failed",
        }
        column = columns.get(outcome)
        if column is None:
            raise ValueError(f"unknown broadcast outcome: {outcome!r}")
        recipient = int(user_id)
        cursor = await self._db.execute(
            f"UPDATE broadcast_drafts SET broadcast_cursor = ?, "
            f"{column} = {column} + 1 WHERE token = ? "
            "AND owner_user_id = ? AND status = 'sending' "
            "AND broadcast_cursor < ? AND audience_upper_user_id >= ?",
            (
                recipient,
                token,
                int(owner_user_id),
                recipient,
                recipient,
            ),
        )
        await self._db.commit()
        return cursor.rowcount == 1

    async def transition_broadcast_draft(
        self,
        token: str,
        owner_user_id: int,
        control_message_id: int,
        from_status: str,
        to_status: str,
        *,
        payload: dict | None = None,
        new_control_message_id: int | None = None,
    ) -> bool:
        """Atomically move one live, message-bound draft to its next state."""
        allowed = {
            "choosing", "preparing", "awaiting_confirmation", "sending",
            "completed", "cancelled", "failed",
        }
        if from_status not in allowed or to_status not in allowed:
            raise ValueError("invalid broadcast draft status")
        assignments = ["status = ?"]
        params: list[object] = [to_status]
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValueError("broadcast draft payload must be a dictionary")
            assignments.append("payload = ?")
            params.append(json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ))
        if new_control_message_id is not None:
            assignments.append("control_message_id = ?")
            params.append(int(new_control_message_id))
        params.extend((
            token,
            int(owner_user_id),
            int(control_message_id),
            from_status,
        ))
        expiry_clause = ""
        if from_status != "sending":
            expiry_clause = " AND expires_at > ?"
            params.append(time.time())
        cursor = await self._db.execute(
            "UPDATE broadcast_drafts SET " + ", ".join(assignments)
            + " WHERE token = ? AND owner_user_id = ? "
            "AND control_message_id = ? AND status = ?" + expiry_clause,
            params,
        )
        await self._db.commit()
        return cursor.rowcount == 1

    # ── persisted Top 10 chart + scheduled fan-out ──────────────
    @staticmethod
    def _normalize_top_music_items(items: list[dict]) -> list[dict]:
        """Validate and reduce a candidate chart to its durable public fields."""
        if not isinstance(items, list) or len(items) != 10:
            raise ValueError("top music chart must contain exactly 10 items")
        normalized: list[dict] = []
        source_ids: set[str] = set()
        video_ids: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("top music items must be dictionaries")
            required = {}
            for key in ("source_id", "artist", "name", "video_id", "title"):
                value = item.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"top music item has invalid {key}")
                required[key] = value.strip()
            if required["source_id"] in source_ids:
                raise ValueError("top music source IDs must be unique")
            if required["video_id"] in video_ids:
                raise ValueError("top music video IDs must be unique")
            source_ids.add(required["source_id"])
            video_ids.add(required["video_id"])
            duration = item.get("duration")
            if duration is not None:
                if isinstance(duration, bool):
                    raise ValueError("top music duration must be an integer")
                try:
                    duration = int(duration)
                except (TypeError, ValueError) as exc:
                    raise ValueError("top music duration must be an integer") from exc
                if duration < 0:
                    raise ValueError("top music duration cannot be negative")
            apple_url = item.get("apple_url") or ""
            uploader = item.get("uploader") or ""
            if not isinstance(apple_url, str) or not isinstance(uploader, str):
                raise ValueError("top music optional text fields must be strings")
            normalized.append(
                {
                    **required,
                    "apple_url": apple_url.strip(),
                    "duration": duration,
                    "uploader": uploader.strip(),
                }
            )
        return normalized

    @staticmethod
    def _top_music_fingerprint(items: list[dict]) -> str:
        ordered_ids = "\n".join(item["source_id"] for item in items)
        return sha256(ordered_ids.encode("utf-8")).hexdigest()

    async def get_top_music_state(self) -> dict:
        """Return the complete chart/schedule state, tolerating DB corruption."""
        async with self._db.execute(
            "SELECT generation, items_json, resolutions_json, fingerprint, "
            "refreshed_at, next_refresh_at, failure_count, refresh_lease_token, "
            "refresh_lease_until, pending_generation, broadcast_cursor, "
            "broadcast_sent, broadcast_inactive, broadcast_failed, "
            "broadcast_upper_user_id FROM top_music_state WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return {
                "generation": 0, "items": [], "resolutions": {},
                "fingerprint": None, "refreshed_at": None,
                "next_refresh_at": 0, "failure_count": 0,
                "refresh_lease_token": None, "refresh_lease_until": None,
                "pending_generation": None, "broadcast_cursor": 0,
                "broadcast_sent": 0, "broadcast_inactive": 0,
                "broadcast_failed": 0, "broadcast_upper_user_id": 0,
            }
        try:
            raw_items = json.loads(row[1])
            items = self._normalize_top_music_items(raw_items) if raw_items else []
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.error("Discarding invalid persisted Top 10 chart")
            items = []
        try:
            resolutions = json.loads(row[2])
            if not isinstance(resolutions, dict):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.error("Discarding invalid persisted Top 10 resolution cache")
            resolutions = {}
        return {
            "generation": int(row[0]),
            "items": items,
            "resolutions": resolutions,
            "fingerprint": row[3],
            "refreshed_at": int(row[4]) if row[4] is not None else None,
            "next_refresh_at": int(row[5]),
            "failure_count": int(row[6]),
            "refresh_lease_token": row[7],
            "refresh_lease_until": (
                int(row[8]) if row[8] is not None else None
            ),
            "pending_generation": (
                int(row[9]) if row[9] is not None else None
            ),
            "broadcast_cursor": int(row[10]),
            "broadcast_sent": int(row[11]),
            "broadcast_inactive": int(row[12]),
            "broadcast_failed": int(row[13]),
            "broadcast_upper_user_id": (
                int(row[14]) if row[14] is not None else 0
            ),
        }

    async def claim_top_music_refresh(
        self, now: int, *, lease_seconds: int = 900, force: bool = False,
    ) -> str | None:
        """Atomically claim one due chart refresh across possible workers."""
        token = secrets.token_urlsafe(18)
        due_clause = "1 = 1" if force else "next_refresh_at <= ?"
        params: list[object] = [token, int(now) + max(30, int(lease_seconds))]
        if not force:
            params.append(int(now))
        params.extend((int(now),))
        cursor = await self._db.execute(
            "UPDATE top_music_state SET refresh_lease_token = ?, "
            "refresh_lease_until = ? WHERE id = 1 AND pending_generation IS NULL "
            f"AND {due_clause} AND (refresh_lease_until IS NULL "
            "OR refresh_lease_until <= ?)",
            params,
        )
        await self._db.commit()
        return token if cursor.rowcount == 1 else None

    async def release_top_music_refresh(self, claim_token: str) -> None:
        await self._db.execute(
            "UPDATE top_music_state SET refresh_lease_token = NULL, "
            "refresh_lease_until = NULL WHERE id = 1 AND refresh_lease_token = ?",
            (claim_token,),
        )
        await self._db.commit()

    async def publish_top_music(
        self, items: list[dict], *, now: int, claim_token: str,
        audience_upper_bound: int, refresh_seconds: int = 172800,
    ) -> bool:
        """Atomically publish a validated chart and queue it when it changed."""
        normalized = self._normalize_top_music_items(items)
        fingerprint = self._top_music_fingerprint(normalized)
        state = await self.get_top_music_state()
        changed = state["fingerprint"] != fingerprint

        # Persist old resolutions as a bounded insertion-ordered cache. This
        # lets a song leave and later re-enter the Top 10 without another
        # YouTube search. Current items are moved to the most-recent end.
        resolutions = dict(state.get("resolutions") or {})
        for item in normalized:
            source_id = item["source_id"]
            resolutions.pop(source_id, None)
            resolutions[source_id] = {
                "video_id": item["video_id"],
                "duration": item["duration"],
                "uploader": item["uploader"],
            }
        while len(resolutions) > 200:
            resolutions.pop(next(iter(resolutions)))

        common = (
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
            json.dumps(resolutions, ensure_ascii=False, separators=(",", ":")),
            fingerprint,
            int(now),
            int(now) + max(60, int(refresh_seconds)),
            claim_token,
        )
        if changed:
            cursor = await self._db.execute(
                "UPDATE top_music_state SET generation = generation + 1, "
                "items_json = ?, resolutions_json = ?, fingerprint = ?, "
                "refreshed_at = ?, next_refresh_at = ?, failure_count = 0, "
                "refresh_lease_token = NULL, refresh_lease_until = NULL, "
                "pending_generation = generation + 1, broadcast_cursor = 0, "
                "broadcast_sent = 0, broadcast_inactive = 0, "
                "broadcast_failed = 0, broadcast_upper_user_id = ? "
                "WHERE id = 1 AND refresh_lease_token = ?",
                (*common[:-1], max(0, int(audience_upper_bound)), common[-1]),
            )
        else:
            cursor = await self._db.execute(
                "UPDATE top_music_state SET items_json = ?, resolutions_json = ?, "
                "fingerprint = ?, refreshed_at = ?, next_refresh_at = ?, "
                "failure_count = 0, refresh_lease_token = NULL, "
                "refresh_lease_until = NULL WHERE id = 1 "
                "AND refresh_lease_token = ?",
                common,
            )
        await self._db.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("Top 10 refresh lease was lost before publication")
        return changed

    async def fail_top_music_refresh(
        self, claim_token: str, *, now: int, base_seconds: int = 300,
        max_seconds: int = 21600,
    ) -> int:
        """Release a failed refresh and return its persisted retry timestamp."""
        state = await self.get_top_music_state()
        failure_count = max(0, int(state["failure_count"])) + 1
        base = max(1, int(base_seconds))
        cap = max(60, base, int(max_seconds))
        delay = min(
            cap,
            base * (2 ** min(failure_count - 1, 8)),
        )
        retry_at = int(now) + delay
        cursor = await self._db.execute(
            "UPDATE top_music_state SET failure_count = ?, next_refresh_at = ?, "
            "refresh_lease_token = NULL, refresh_lease_until = NULL "
            "WHERE id = 1 AND refresh_lease_token = ?",
            (failure_count, retry_at, claim_token),
        )
        await self._db.commit()
        return retry_at if cursor.rowcount == 1 else int(state["next_refresh_at"])

    async def checkpoint_top_music_broadcast(
        self, generation: int, user_id: int, outcome: str,
    ) -> None:
        """Persist one terminal delivery outcome before advancing further."""
        columns = {
            "sent": "broadcast_sent",
            "inactive": "broadcast_inactive",
            "failed": "broadcast_failed",
        }
        column = columns.get(outcome)
        if column is None:
            raise ValueError(f"unknown broadcast outcome: {outcome!r}")
        await self._db.execute(
            f"UPDATE top_music_state SET broadcast_cursor = MAX(broadcast_cursor, ?), "
            f"{column} = {column} + 1 WHERE id = 1 AND pending_generation = ?",
            (int(user_id), int(generation)),
        )
        await self._db.commit()

    async def complete_top_music_broadcast(self, generation: int) -> bool:
        cursor = await self._db.execute(
            "UPDATE top_music_state SET pending_generation = NULL "
            "WHERE id = 1 AND pending_generation = ?",
            (int(generation),),
        )
        await self._db.commit()
        return cursor.rowcount == 1

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
        draft_cursor = await self._db.execute(
            "DELETE FROM broadcast_drafts WHERE owner_user_id = ?", (user_id,)
        )
        await self._db.commit()
        return (
            len(owned_tokens)
            + max(0, user_cursor.rowcount)
            + max(0, recognition_cursor.rowcount)
            + max(0, draft_cursor.rowcount)
        )
