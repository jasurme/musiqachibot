"""SQLite storage for users, favorites, sessions, recognition, and media cache.

The file_id cache is the money-saver: once a song has been sent once, Telegram
gives us a `file_id` we can re-send instantly, for free, with no re-download.
"""
import asyncio
import json
import logging
import re
import secrets
import time
from hashlib import sha256
from urllib.parse import urlsplit

import aiosqlite

logger = logging.getLogger(__name__)

_DIRECT_BROADCAST_HISTORY_SECONDS = 24 * 60 * 60

# Retain a fixed value in the historical outbox column. Railway databases from
# the preference-enabled release have this NOT NULL column without a default,
# so removing it would require a destructive SQLite table rebuild. It is no
# longer decoded or used to select recipients.
_LEGACY_OUTBOX_NOTIFICATION_MASK = 1
_MUSIC_KEY = re.compile(r"[a-z][a-z0-9_:-]{0,63}")
_YOUTUBE_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,32}")
_MUSIC_CAMPAIGN_STATUSES = {
    "queued", "sending", "completed", "superseded", "failed",
}
_FAVORITE_TITLE_MAX = 512
_FAVORITE_UPLOADER_MAX = 512
_TELEGRAM_FILE_ID_MAX = 2048
_FAVORITE_SOURCE_URL_MAX = 4096
_FAVORITE_TRACK_KEY = re.compile(
    r"[a-z][a-z0-9_]{0,11}:[A-Za-z0-9._-]{1,40}"
)
_UNSAVED_FAVORITE_TRACK_TTL = 30 * 24 * 60 * 60
_UNSAVED_FAVORITE_TRACK_LIMIT = 5000
_FAVORITE_PRUNE_INTERVAL = 60 * 60


class Storage:
    def __init__(
        self, path: str = "musiqa.db", session_ttl_days: int = 7,
        recognition_ttl_days: int = 30,
    ):
        self.path = path
        self.session_ttl_days = max(1, session_ttl_days)
        self.recognition_ttl_days = max(1, recognition_ttl_days)
        self._db: aiosqlite.Connection | None = None
        self._music_db: aiosqlite.Connection | None = None
        # A dedicated connection makes SQLite itself isolate long, explicit
        # music/outbox transactions from unrelated locale/cache/admin commits.
        # Shared-cache URI mode preserves normal ``:memory:`` test semantics
        # across the two connections.
        if self.path == ":memory:":
            self._connect_target = (
                f"file:musiqa_{secrets.token_hex(12)}?mode=memory&cache=shared"
            )
            self._connect_uri = True
        else:
            self._connect_target = self.path
            self._connect_uri = False
        # The second connection still needs one in-process transaction owner.
        self._music_write_lock = asyncio.Lock()
        # Serialize the small catalog/membership write transactions so rapid
        # duplicate callbacks cannot observe partially updated favorite state.
        self._favorite_write_lock = asyncio.Lock()
        self._favorite_prune_after = 0

    async def init(self) -> None:
        self._db = await aiosqlite.connect(
            self._connect_target, uri=self._connect_uri
        )
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.execute("PRAGMA foreign_keys = ON")
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
        # Track metadata is a small durable catalog, separate from each user's
        # playlist membership. A heart button can therefore keep a compact,
        # stable track key and still work after its source session/restart.
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS favorite_tracks ("
            " track_key TEXT PRIMARY KEY,"
            " video_id TEXT,"
            " source_url TEXT,"
            " title TEXT NOT NULL,"
            " duration INTEGER,"
            " uploader TEXT NOT NULL DEFAULT '',"
            " audio_file_id TEXT,"
            " updated_at INTEGER NOT NULL)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_favorite_tracks_updated "
            "ON favorite_tracks(updated_at)"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS user_favorites ("
            " user_id INTEGER NOT NULL CHECK (user_id > 0),"
            " track_key TEXT NOT NULL,"
            " saved_at INTEGER NOT NULL,"
            " PRIMARY KEY(user_id, track_key),"
            " FOREIGN KEY(user_id) REFERENCES users(user_id) "
            "ON DELETE CASCADE,"
            " FOREIGN KEY(track_key) REFERENCES favorite_tracks(track_key) "
            "ON DELETE CASCADE)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_favorites_page "
            "ON user_favorites(user_id, saved_at DESC, track_key)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_favorites_track "
            "ON user_favorites(track_key)"
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
        # Generalized atomic music snapshots. Network/provider work writes a
        # complete candidate in one transaction; handlers only read the last
        # complete JSON snapshot and therefore stay provider-free and fast.
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS music_collection_state ("
            " collection_key TEXT PRIMARY KEY,"
            " expected_count INTEGER NOT NULL CHECK (expected_count > 0),"
            " generation INTEGER NOT NULL DEFAULT 0,"
            " items_json TEXT NOT NULL DEFAULT '[]',"
            " resolutions_json TEXT NOT NULL DEFAULT '{}',"
            " provider_state_json TEXT NOT NULL DEFAULT '{}',"
            " fingerprint TEXT,"
            " refreshed_at INTEGER,"
            " next_refresh_at INTEGER NOT NULL DEFAULT 0,"
            " failure_count INTEGER NOT NULL DEFAULT 0,"
            " refresh_lease_token TEXT,"
            " refresh_lease_until INTEGER)"
        )
        # Whole ranked source snapshots are retained for rising/novelty
        # comparisons. JSON keeps one observation atomic and the bounded index
        # makes weekly pruning cheap.
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS music_chart_snapshots ("
            " chart_key TEXT NOT NULL,"
            " captured_at INTEGER NOT NULL,"
            " items_json TEXT NOT NULL,"
            " fingerprint TEXT NOT NULL,"
            " PRIMARY KEY (chart_key, captured_at))"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_music_chart_snapshots_lookup "
            "ON music_chart_snapshots(chart_key, captured_at DESC)"
        )
        # Each queued row freezes its text and exact track buttons. A later
        # collection refresh cannot mutate a partially delivered campaign.
        # week_key/slot_no form the durable global weekly quota reservation.
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS music_campaign_outbox ("
            " campaign_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " dedupe_key TEXT NOT NULL UNIQUE,"
            " collection_key TEXT NOT NULL,"
            " collection_generation INTEGER NOT NULL,"
            " campaign_kind TEXT NOT NULL,"
            " payload_json TEXT NOT NULL,"
            " notification_mask INTEGER NOT NULL,"
            " eligible_at INTEGER NOT NULL,"
            " expires_at INTEGER,"
            " priority INTEGER NOT NULL DEFAULT 0,"
            " status TEXT NOT NULL DEFAULT 'queued' "
            "CHECK (status IN "
            "('queued','sending','completed','superseded','failed')),"
            " week_key TEXT,"
            " slot_no INTEGER,"
            " audience_upper_user_id INTEGER NOT NULL DEFAULT 0,"
            " broadcast_cursor INTEGER NOT NULL DEFAULT 0,"
            " broadcast_sent INTEGER NOT NULL DEFAULT 0,"
            " broadcast_inactive INTEGER NOT NULL DEFAULT 0,"
            " broadcast_failed INTEGER NOT NULL DEFAULT 0,"
            " runner_token TEXT,"
            " runner_lease_until INTEGER,"
            " created_at INTEGER NOT NULL,"
            " started_at INTEGER,"
            " completed_at INTEGER)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_music_campaign_outbox_ready "
            "ON music_campaign_outbox(status, eligible_at, priority DESC, "
            "campaign_id)"
        )
        await self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_music_campaign_week_slot ON music_campaign_outbox"
            "(week_key, slot_no) WHERE week_key IS NOT NULL "
            "AND slot_no IS NOT NULL"
        )
        # Preserve an existing Top Music snapshot for the generic read path.
        # Its old pending fan-out is deliberately not copied: Top Music no
        # longer creates proactive campaigns in the generalized subsystem.
        await self._db.execute(
            "INSERT OR IGNORE INTO music_collection_state ("
            "collection_key, expected_count, generation, items_json, "
            "resolutions_json, provider_state_json, fingerprint, "
            "refreshed_at, next_refresh_at, failure_count, "
            "refresh_lease_token, refresh_lease_until) "
            "SELECT 'top_music', 10, generation, items_json, "
            "resolutions_json, '{}', fingerprint, refreshed_at, "
            "next_refresh_at, failure_count, refresh_lease_token, "
            "refresh_lease_until FROM top_music_state WHERE id = 1"
        )
        # Earlier releases only recorded users who selected a language. Recover
        # every positive private owner ID still present in recognition/session
        # rows so the first deployment does not start with an empty audience.
        await self._db.execute(
            "INSERT OR IGNORE INTO users (user_id) "
            "SELECT DISTINCT owner_user_id FROM recognition_cache "
            "WHERE owner_user_id IS NOT NULL AND owner_user_id > 0"
        )
        await self._db.execute(
            "INSERT OR IGNORE INTO users (user_id) "
            "SELECT DISTINCT user_id FROM user_favorites WHERE user_id > 0"
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
        self._music_db = await aiosqlite.connect(
            self._connect_target, uri=self._connect_uri
        )
        await self._music_db.execute("PRAGMA busy_timeout = 5000")

    async def close(self) -> None:
        if self._music_db is not None:
            await self._music_db.close()
            self._music_db = None
        if self._db is not None:
            await self._db.close()
            self._db = None

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

    async def get_user_counts(
        self, *, exclude_user_id: int | None = None,
    ) -> dict[str, int]:
        """Return total, active, and inactive users in one aggregate query."""
        where = ""
        params: tuple[int, ...] = ()
        if exclude_user_id is not None:
            where = " WHERE user_id != ?"
            params = (int(exclude_user_id),)
        async with self._db.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN is_active = 1 THEN 0 ELSE 1 END), 0) "
            "FROM users" + where,
            params,
        ) as cur:
            row = await cur.fetchone()
        if row is None:  # pragma: no cover - aggregate SELECT always returns
            return {"total": 0, "active": 0, "inactive": 0}
        return {
            "total": int(row[0]),
            "active": int(row[1]),
            "inactive": int(row[2]),
        }

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
        clauses = ["is_active = 1"]
        params: list[int] = []
        if exclude_user_id is not None:
            clauses.append("user_id != ?")
            params.append(int(exclude_user_id))
        query = (
            "SELECT COALESCE(MAX(user_id), 0) FROM users WHERE "
            + " AND ".join(clauses)
        )
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

    # ── generalized editorial music snapshots ─────────────────
    @staticmethod
    def _validate_music_key(key: str, *, label: str = "music key") -> str:
        value = str(key or "").strip()
        if _MUSIC_KEY.fullmatch(value) is None:
            raise ValueError(f"invalid {label}")
        return value

    @staticmethod
    def _json_object(value: dict | None, *, label: str) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be a dictionary")
        try:
            return json.loads(json.dumps(
                value, ensure_ascii=False, separators=(",", ":")
            ))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be JSON serializable") from exc

    @classmethod
    def _normalize_music_collection_items(
        cls, items: list[dict], expected_count: int,
    ) -> list[dict]:
        """Validate one complete downloadable collection without dropping metadata."""
        count = int(expected_count)
        if count <= 0 or count > 100:
            raise ValueError("music collection expected_count is out of range")
        if not isinstance(items, list) or len(items) != count:
            raise ValueError(
                f"music collection must contain exactly {count} items"
            )
        try:
            copied = json.loads(json.dumps(
                items, ensure_ascii=False, separators=(",", ":")
            ))
        except (TypeError, ValueError) as exc:
            raise ValueError("music collection items must be JSON serializable") from exc

        normalized: list[dict] = []
        source_ids: set[str] = set()
        video_ids: set[str] = set()
        for item in copied:
            if not isinstance(item, dict):
                raise ValueError("music collection items must be dictionaries")
            for field in ("source_id", "artist", "name", "video_id", "title"):
                value = item.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"music collection item has invalid {field}")
                item[field] = value.strip()
            if item["source_id"] in source_ids:
                raise ValueError("music collection source IDs must be unique")
            if _YOUTUBE_VIDEO_ID.fullmatch(item["video_id"]) is None:
                raise ValueError("music collection item has invalid video_id")
            if item["video_id"] in video_ids:
                raise ValueError("music collection video IDs must be unique")
            source_ids.add(item["source_id"])
            video_ids.add(item["video_id"])

            duration = item.get("duration")
            if duration is not None:
                if isinstance(duration, bool):
                    raise ValueError("music collection duration must be an integer")
                try:
                    duration = int(duration)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "music collection duration must be an integer"
                    ) from exc
                if duration < 0:
                    raise ValueError("music collection duration cannot be negative")
            item["duration"] = duration
            uploader = item.get("uploader") or ""
            if not isinstance(uploader, str):
                raise ValueError("music collection uploader must be text")
            item["uploader"] = uploader.strip()
            for field in ("apple_url", "source_url", "release_date", "storefront"):
                if field in item and item[field] is not None:
                    if not isinstance(item[field], str):
                        raise ValueError(
                            f"music collection {field} must be text"
                        )
                    item[field] = item[field].strip()
            normalized.append(item)
        return normalized

    @staticmethod
    def _music_collection_fingerprint(items: list[dict]) -> str:
        # Include the exact durable callback target as well as ordered source
        # identity. A repaired YouTube mapping must activate a new snapshot.
        identity = [
            [item["source_id"], item["video_id"], item["title"]]
            for item in items
        ]
        raw = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        return sha256(raw.encode("utf-8")).hexdigest()

    async def ensure_music_collection(
        self, key: str, expected_count: int,
    ) -> None:
        """Create an empty configured collection, rejecting count drift."""
        collection_key = self._validate_music_key(key, label="collection key")
        count = int(expected_count)
        if count <= 0 or count > 100:
            raise ValueError("music collection expected_count is out of range")
        await self._music_db.execute(
            "INSERT OR IGNORE INTO music_collection_state "
            "(collection_key, expected_count) VALUES (?, ?)",
            (collection_key, count),
        )
        async with self._music_db.execute(
            "SELECT expected_count FROM music_collection_state "
            "WHERE collection_key = ?",
            (collection_key,),
        ) as cur:
            row = await cur.fetchone()
        if row is None or int(row[0]) != count:
            await self._music_db.rollback()
            raise ValueError("music collection expected_count cannot change")
        await self._music_db.commit()

    async def get_music_collection_state(self, key: str) -> dict:
        collection_key = self._validate_music_key(key, label="collection key")
        async with self._music_db.execute(
            "SELECT collection_key, expected_count, generation, items_json, "
            "resolutions_json, provider_state_json, fingerprint, refreshed_at, "
            "next_refresh_at, failure_count, refresh_lease_token, "
            "refresh_lease_until FROM music_collection_state "
            "WHERE collection_key = ?",
            (collection_key,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return {
                "key": collection_key, "expected_count": 0,
                "generation": 0, "items": [], "resolutions": {},
                "provider_state": {}, "fingerprint": None,
                "refreshed_at": None, "next_refresh_at": 0,
                "failure_count": 0, "refresh_lease_token": None,
                "refresh_lease_until": None,
            }
        expected_count = int(row[1])
        try:
            raw_items = json.loads(row[3])
            items = (
                self._normalize_music_collection_items(
                    raw_items, expected_count
                )
                if raw_items else []
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.error(
                "Discarding invalid music collection key=%s", collection_key
            )
            items = []

        decoded_objects: list[dict] = []
        for raw, label in (
            (row[4], "resolutions"), (row[5], "provider state"),
        ):
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.error(
                    "Discarding invalid music %s key=%s", label, collection_key
                )
                value = {}
            decoded_objects.append(value)
        return {
            "key": row[0], "expected_count": expected_count,
            "generation": int(row[2]), "items": items,
            "resolutions": decoded_objects[0],
            "provider_state": decoded_objects[1], "fingerprint": row[6],
            "refreshed_at": int(row[7]) if row[7] is not None else None,
            "next_refresh_at": int(row[8]), "failure_count": int(row[9]),
            "refresh_lease_token": row[10],
            "refresh_lease_until": (
                int(row[11]) if row[11] is not None else None
            ),
        }

    async def claim_music_collection_refresh(
        self, key: str, now: int, *, lease_seconds: int = 900,
        force: bool = False,
    ) -> str | None:
        collection_key = self._validate_music_key(key, label="collection key")
        token = secrets.token_urlsafe(18)
        params: list[object] = [
            token, int(now) + max(30, int(lease_seconds)), collection_key,
        ]
        due_clause = "1 = 1" if force else "next_refresh_at <= ?"
        if not force:
            params.append(int(now))
        params.append(int(now))
        cursor = await self._music_db.execute(
            "UPDATE music_collection_state SET refresh_lease_token = ?, "
            "refresh_lease_until = ? WHERE collection_key = ? AND "
            f"{due_clause} AND (refresh_lease_until IS NULL "
            "OR refresh_lease_until <= ?)",
            params,
        )
        await self._music_db.commit()
        return token if cursor.rowcount == 1 else None

    async def release_music_collection_refresh(
        self, key: str, claim_token: str,
    ) -> None:
        collection_key = self._validate_music_key(key, label="collection key")
        await self._music_db.execute(
            "UPDATE music_collection_state SET refresh_lease_token = NULL, "
            "refresh_lease_until = NULL WHERE collection_key = ? "
            "AND refresh_lease_token = ?",
            (collection_key, claim_token),
        )
        await self._music_db.commit()

    async def defer_music_collection_refresh(
        self, key: str, claim_token: str, *, next_refresh_at: int,
    ) -> bool:
        """Finish a valid no-content refresh without counting it as failure."""
        collection_key = self._validate_music_key(key, label="collection key")
        cursor = await self._music_db.execute(
            "UPDATE music_collection_state SET next_refresh_at = ?, "
            "failure_count = 0, refresh_lease_token = NULL, "
            "refresh_lease_until = NULL WHERE collection_key = ? "
            "AND refresh_lease_token = ?",
            (max(0, int(next_refresh_at)), collection_key, claim_token),
        )
        await self._music_db.commit()
        return cursor.rowcount == 1

    @classmethod
    def _validate_campaign_spec(cls, campaign: dict | None) -> dict | None:
        if campaign is None:
            return None
        if not isinstance(campaign, dict):
            raise ValueError("music campaign specification must be a dictionary")
        dedupe_key = str(campaign.get("dedupe_key") or "").strip()
        if not dedupe_key or len(dedupe_key) > 200:
            raise ValueError("music campaign has an invalid dedupe_key")
        kind = cls._validate_music_key(
            campaign.get("kind"), label="campaign kind"
        )
        header_key = str(campaign.get("header_key") or "").strip()
        if _MUSIC_KEY.fullmatch(header_key) is None:
            raise ValueError("music campaign has an invalid header_key")
        eligible_at = int(campaign.get("eligible_at"))
        raw_expires = campaign.get("expires_at")
        expires_at = int(raw_expires) if raw_expires is not None else None
        if expires_at is not None and expires_at <= eligible_at:
            raise ValueError("music campaign must expire after it becomes eligible")
        return {
            "dedupe_key": dedupe_key,
            "kind": kind,
            "header_key": header_key,
            "eligible_at": eligible_at,
            "expires_at": expires_at,
            "priority": int(campaign.get("priority") or 0),
        }

    async def _enqueue_music_campaign_locked(
        self,
        *,
        collection_key: str,
        generation: int,
        items: list[dict],
        campaign: dict,
        created_at: int,
    ) -> tuple[int, bool]:
        """Insert one immutable outbox row inside the caller's transaction."""
        spec = self._validate_campaign_spec(campaign)
        if spec is None:  # pragma: no cover - guarded by callers
            raise ValueError("music campaign specification is required")
        payload = json.dumps(
            {"header_key": spec["header_key"], "items": items},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        async with self._music_db.execute(
            "SELECT campaign_id FROM music_campaign_outbox "
            "WHERE dedupe_key = ?",
            (spec["dedupe_key"],),
        ) as cur:
            existing = await cur.fetchone()
        if existing is not None:
            return int(existing[0]), False
        # Only an unsent older candidate of the same category is superseded.
        # A partially sent campaign always resumes with its frozen payload.
        await self._music_db.execute(
            "UPDATE music_campaign_outbox SET status = 'superseded', "
            "completed_at = ? WHERE collection_key = ? AND campaign_kind = ? "
            "AND status = 'queued' AND dedupe_key != ?",
            (
                int(created_at), collection_key, spec["kind"],
                spec["dedupe_key"],
            ),
        )
        cursor = await self._music_db.execute(
            "INSERT OR IGNORE INTO music_campaign_outbox ("
            "dedupe_key, collection_key, collection_generation, "
            "campaign_kind, payload_json, notification_mask, eligible_at, "
            "expires_at, priority, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
            (
                spec["dedupe_key"], collection_key, int(generation),
                spec["kind"], payload, _LEGACY_OUTBOX_NOTIFICATION_MASK,
                spec["eligible_at"], spec["expires_at"], spec["priority"],
                int(created_at),
            ),
        )
        created = cursor.rowcount == 1
        async with self._music_db.execute(
            "SELECT campaign_id FROM music_campaign_outbox "
            "WHERE dedupe_key = ?",
            (spec["dedupe_key"],),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise RuntimeError("music campaign outbox insert disappeared")
        return int(row[0]), created

    async def _publish_music_collection_locked(
        self,
        key: str,
        items: list[dict],
        *,
        now: int,
        claim_token: str,
        refresh_seconds: int,
        resolutions: dict | None,
        provider_state: dict | None,
        campaign: dict | None,
    ) -> dict:
        """Publish inside an already-open transaction."""
        collection_key = self._validate_music_key(key, label="collection key")
        async with self._music_db.execute(
            "SELECT expected_count, generation, resolutions_json, "
            "provider_state_json, fingerprint, refresh_lease_token "
            "FROM music_collection_state WHERE collection_key = ?",
            (collection_key,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise ValueError("music collection has not been configured")
        if row[5] != claim_token:
            raise RuntimeError("music collection refresh lease was lost")

        expected_count = int(row[0])
        normalized = self._normalize_music_collection_items(
            items, expected_count
        )
        fingerprint = self._music_collection_fingerprint(normalized)
        changed = row[4] != fingerprint
        generation = int(row[1]) + (1 if changed else 0)

        if resolutions is None:
            try:
                normalized_resolutions = json.loads(row[2])
                if not isinstance(normalized_resolutions, dict):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                normalized_resolutions = {}
        else:
            normalized_resolutions = self._json_object(
                resolutions, label="music resolutions"
            )
        if provider_state is None:
            try:
                normalized_provider_state = json.loads(row[3])
                if not isinstance(normalized_provider_state, dict):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                normalized_provider_state = {}
        else:
            normalized_provider_state = self._json_object(
                provider_state, label="music provider state"
            )
        campaign_spec = self._validate_campaign_spec(campaign)

        cursor = await self._music_db.execute(
            "UPDATE music_collection_state SET generation = ?, items_json = ?, "
            "resolutions_json = ?, provider_state_json = ?, fingerprint = ?, "
            "refreshed_at = ?, next_refresh_at = ?, failure_count = 0, "
            "refresh_lease_token = NULL, refresh_lease_until = NULL "
            "WHERE collection_key = ? AND refresh_lease_token = ?",
            (
                generation,
                json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                json.dumps(
                    normalized_resolutions, ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps(
                    normalized_provider_state, ensure_ascii=False,
                    separators=(",", ":"),
                ),
                fingerprint, int(now),
                int(now) + max(60, int(refresh_seconds)), collection_key,
                claim_token,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "music collection refresh lease was lost before publication"
            )

        campaign_id = None
        campaign_created = False
        if changed and campaign_spec is not None:
            campaign_id, campaign_created = await self._enqueue_music_campaign_locked(
                collection_key=collection_key,
                generation=generation,
                items=normalized,
                campaign=campaign_spec,
                created_at=int(now),
            )
        return {
            "changed": changed,
            "generation": generation,
            "campaign_id": campaign_id,
            "campaign_created": campaign_created,
        }

    async def publish_music_collection(
        self,
        key: str,
        items: list[dict],
        *,
        now: int,
        claim_token: str,
        refresh_seconds: int,
        resolutions: dict | None = None,
        provider_state: dict | None = None,
        campaign: dict | None = None,
    ) -> dict:
        """Atomically replace one last-good snapshot and optional campaign."""
        async with self._music_write_lock:
            await self._music_db.execute("BEGIN IMMEDIATE")
            try:
                result = await self._publish_music_collection_locked(
                    key,
                    items,
                    now=now,
                    claim_token=claim_token,
                    refresh_seconds=refresh_seconds,
                    resolutions=resolutions,
                    provider_state=provider_state,
                    campaign=campaign,
                )
                await self._music_db.commit()
            except BaseException:
                await self._music_db.rollback()
                raise
        return result

    async def publish_music_collection_batch(
        self, publications: list[dict],
    ) -> list[dict]:
        """Atomically publish a validated group (the five weekly moods)."""
        if not isinstance(publications, list) or not publications:
            raise ValueError("music publication batch cannot be empty")
        keys = [
            self._validate_music_key(item.get("key"), label="collection key")
            if isinstance(item, dict) else ""
            for item in publications
        ]
        if not all(keys) or len(set(keys)) != len(keys):
            raise ValueError("music publication batch keys must be unique")
        results: list[dict] = []
        async with self._music_write_lock:
            await self._music_db.execute("BEGIN IMMEDIATE")
            try:
                for publication in publications:
                    results.append(await self._publish_music_collection_locked(
                        publication["key"],
                        publication["items"],
                        now=int(publication["now"]),
                        claim_token=publication["claim_token"],
                        refresh_seconds=int(publication["refresh_seconds"]),
                        resolutions=publication.get("resolutions"),
                        provider_state=publication.get("provider_state"),
                        campaign=publication.get("campaign"),
                    ))
                await self._music_db.commit()
            except BaseException:
                await self._music_db.rollback()
                raise
        return results

    async def fail_music_collection_refresh(
        self,
        key: str,
        claim_token: str,
        *,
        now: int,
        base_seconds: int = 1800,
        max_seconds: int = 21600,
    ) -> int:
        """Keep the last-good snapshot and persist bounded exponential backoff."""
        collection_key = self._validate_music_key(key, label="collection key")
        async with self._music_db.execute(
            "SELECT failure_count, next_refresh_at FROM music_collection_state "
            "WHERE collection_key = ?",
            (collection_key,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise ValueError("music collection has not been configured")
        failure_count = max(0, int(row[0])) + 1
        base = max(1, int(base_seconds))
        cap = max(60, base, int(max_seconds))
        delay = min(cap, base * (2 ** min(failure_count - 1, 8)))
        retry_at = int(now) + delay
        cursor = await self._music_db.execute(
            "UPDATE music_collection_state SET failure_count = ?, "
            "next_refresh_at = ?, refresh_lease_token = NULL, "
            "refresh_lease_until = NULL WHERE collection_key = ? "
            "AND refresh_lease_token = ?",
            (failure_count, retry_at, collection_key, claim_token),
        )
        await self._music_db.commit()
        return retry_at if cursor.rowcount == 1 else int(row[1])

    @classmethod
    def _normalize_chart_snapshot_items(cls, items: list[dict]) -> list[dict]:
        if not isinstance(items, list) or not 1 <= len(items) <= 200:
            raise ValueError("music chart snapshot must contain 1 to 200 items")
        try:
            copied = json.loads(json.dumps(
                items, ensure_ascii=False, separators=(",", ":")
            ))
        except (TypeError, ValueError) as exc:
            raise ValueError("music chart snapshot must be JSON serializable") from exc
        source_ids: set[str] = set()
        for index, item in enumerate(copied, start=1):
            if not isinstance(item, dict):
                raise ValueError("music chart entries must be dictionaries")
            source_id = item.get("source_id")
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError("music chart entry has invalid source_id")
            item["source_id"] = source_id.strip()
            if item["source_id"] in source_ids:
                raise ValueError("music chart source IDs must be unique")
            source_ids.add(item["source_id"])
            if "rank" in item:
                if isinstance(item["rank"], bool):
                    raise ValueError("music chart rank must be an integer")
                try:
                    item["rank"] = int(item["rank"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("music chart rank must be an integer") from exc
                if item["rank"] <= 0:
                    raise ValueError("music chart rank must be positive")
            else:
                item["rank"] = index
        return copied

    async def record_music_chart_snapshot(
        self,
        chart_key: str,
        items: list[dict],
        *,
        captured_at: int,
        keep: int = 32,
    ) -> bool:
        """Persist one full ranked observation and prune older history."""
        key = self._validate_music_key(chart_key, label="chart key")
        normalized = self._normalize_chart_snapshot_items(items)
        identity = [item["source_id"] for item in normalized]
        fingerprint = sha256("\n".join(identity).encode("utf-8")).hexdigest()
        retained = max(2, min(int(keep), 365))
        async with self._music_write_lock:
            await self._music_db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._music_db.execute(
                    "INSERT OR IGNORE INTO music_chart_snapshots "
                    "(chart_key, captured_at, items_json, fingerprint) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        key, int(captured_at),
                        json.dumps(
                            normalized, ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        fingerprint,
                    ),
                )
                await self._music_db.execute(
                    "DELETE FROM music_chart_snapshots WHERE chart_key = ? "
                    "AND captured_at NOT IN (SELECT captured_at FROM "
                    "music_chart_snapshots WHERE chart_key = ? "
                    "ORDER BY captured_at DESC LIMIT ?)",
                    (key, key, retained),
                )
                await self._music_db.commit()
            except BaseException:
                await self._music_db.rollback()
                raise
        return cursor.rowcount == 1

    @classmethod
    def _decode_chart_snapshot(cls, row) -> dict | None:
        if row is None:
            return None
        try:
            items = cls._normalize_chart_snapshot_items(json.loads(row[2]))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.error(
                "Discarding invalid chart snapshot chart=%s captured_at=%s",
                row[0], row[1],
            )
            return None
        return {
            "chart_key": row[0], "captured_at": int(row[1]),
            "items": items, "fingerprint": row[3],
        }

    async def get_music_chart_snapshot_before(
        self, chart_key: str, before: int,
    ) -> dict | None:
        key = self._validate_music_key(chart_key, label="chart key")
        async with self._music_db.execute(
            "SELECT chart_key, captured_at, items_json, fingerprint "
            "FROM music_chart_snapshots WHERE chart_key = ? "
            "AND captured_at < ? ORDER BY captured_at DESC LIMIT 1",
            (key, int(before)),
        ) as cur:
            row = await cur.fetchone()
        return self._decode_chart_snapshot(row)

    async def get_latest_music_chart_snapshot(
        self, chart_key: str,
    ) -> dict | None:
        key = self._validate_music_key(chart_key, label="chart key")
        async with self._music_db.execute(
            "SELECT chart_key, captured_at, items_json, fingerprint "
            "FROM music_chart_snapshots WHERE chart_key = ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (key,),
        ) as cur:
            row = await cur.fetchone()
        return self._decode_chart_snapshot(row)

    # ── durable, globally quota-limited music campaign outbox ──────
    @staticmethod
    def _decode_music_campaign_row(row) -> dict | None:
        if row is None:
            return None
        try:
            payload = json.loads(row[5])
        except (TypeError, json.JSONDecodeError):
            logger.error("Discarding invalid campaign payload id=%s", row[0])
            return None
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("header_key"), str)
            or not isinstance(payload.get("items"), list)
        ):
            logger.error("Discarding malformed campaign payload id=%s", row[0])
            return None
        status = str(row[10])
        if status not in _MUSIC_CAMPAIGN_STATUSES:
            return None
        return {
            "campaign_id": int(row[0]), "dedupe_key": row[1],
            "collection_key": row[2], "collection_generation": int(row[3]),
            "kind": row[4], "payload": payload,
            "eligible_at": int(row[7]),
            "expires_at": int(row[8]) if row[8] is not None else None,
            "priority": int(row[9]), "status": status, "week_key": row[11],
            "slot_no": int(row[12]) if row[12] is not None else None,
            "audience_upper_user_id": int(row[13]),
            "broadcast_cursor": int(row[14]), "broadcast_sent": int(row[15]),
            "broadcast_inactive": int(row[16]),
            "broadcast_failed": int(row[17]), "runner_token": row[18],
            "runner_lease_until": (
                int(row[19]) if row[19] is not None else None
            ),
            "created_at": int(row[20]),
            "started_at": int(row[21]) if row[21] is not None else None,
            "completed_at": int(row[22]) if row[22] is not None else None,
        }

    @staticmethod
    def _music_campaign_select() -> str:
        return (
            "campaign_id, dedupe_key, collection_key, collection_generation, "
            "campaign_kind, payload_json, notification_mask, eligible_at, "
            "expires_at, priority, status, week_key, slot_no, "
            "audience_upper_user_id, broadcast_cursor, broadcast_sent, "
            "broadcast_inactive, broadcast_failed, runner_token, "
            "runner_lease_until, created_at, started_at, completed_at"
        )

    async def enqueue_music_campaign(
        self,
        *,
        dedupe_key: str,
        collection_key: str,
        generation: int,
        kind: str,
        header_key: str,
        items: list[dict],
        eligible_at: int,
        expires_at: int | None,
        priority: int = 0,
        now: int | None = None,
    ) -> int:
        """Idempotently enqueue an already-published frozen collection."""
        key = self._validate_music_key(collection_key, label="collection key")
        state = await self.get_music_collection_state(key)
        if state["expected_count"] <= 0:
            raise ValueError("music collection has not been configured")
        normalized = self._normalize_music_collection_items(
            items, state["expected_count"]
        )
        campaign = {
            "dedupe_key": dedupe_key,
            "kind": kind,
            "header_key": header_key,
            "eligible_at": eligible_at,
            "expires_at": expires_at,
            "priority": priority,
        }
        async with self._music_write_lock:
            await self._music_db.execute("BEGIN IMMEDIATE")
            try:
                campaign_id, _created = await self._enqueue_music_campaign_locked(
                    collection_key=key,
                    generation=int(generation),
                    items=normalized,
                    campaign=campaign,
                    created_at=int(time.time()) if now is None else int(now),
                )
                await self._music_db.commit()
            except BaseException:
                await self._music_db.rollback()
                raise
        return campaign_id

    async def get_music_campaign(self, campaign_id: int) -> dict | None:
        async with self._music_db.execute(
            "SELECT " + self._music_campaign_select()
            + " FROM music_campaign_outbox WHERE campaign_id = ?",
            (int(campaign_id),),
        ) as cur:
            row = await cur.fetchone()
        return self._decode_music_campaign_row(row)

    async def claim_next_music_campaign(
        self,
        *,
        now: int,
        week_key: str,
        max_per_week: int = 3,
        lease_seconds: int = 120,
    ) -> dict | None:
        """Resume one delivery or atomically reserve a due weekly slot."""
        timestamp = int(now)
        week = str(week_key or "").strip()
        if not week or len(week) > 64:
            raise ValueError("music campaign week_key is invalid")
        weekly_limit = max(1, min(int(max_per_week), 7))
        lease_until = timestamp + max(30, int(lease_seconds))
        token = secrets.token_urlsafe(18)

        async with self._music_write_lock:
            await self._music_db.execute("BEGIN IMMEDIATE")
            try:
                # Expired candidates were never delivered and consume no slot.
                await self._music_db.execute(
                    "UPDATE music_campaign_outbox SET status = 'superseded', "
                    "completed_at = ? WHERE status = 'queued' "
                    "AND expires_at IS NOT NULL AND expires_at <= ?",
                    (timestamp, timestamp),
                )

                # There may be only one scheduled campaign runner. If its
                # short lease is still active, another process must stand down;
                # otherwise this caller atomically takes over its exact cursor.
                async with self._music_db.execute(
                    "SELECT campaign_id, runner_lease_until "
                    "FROM music_campaign_outbox WHERE status = 'sending' "
                    "ORDER BY started_at, campaign_id LIMIT 1"
                ) as cur:
                    sending = await cur.fetchone()
                campaign_id: int | None = None
                if sending is not None:
                    if (
                        sending[1] is not None
                        and int(sending[1]) > timestamp
                    ):
                        await self._music_db.commit()
                        return None
                    campaign_id = int(sending[0])
                    cursor = await self._music_db.execute(
                        "UPDATE music_campaign_outbox SET runner_token = ?, "
                        "runner_lease_until = ?, audience_upper_user_id = MAX("
                        "audience_upper_user_id, (SELECT COALESCE(MAX(user_id), 0) "
                        "FROM users WHERE is_active = 1)) WHERE campaign_id = ? "
                        "AND status = 'sending' AND (runner_lease_until IS NULL "
                        "OR runner_lease_until <= ?)",
                        (token, lease_until, campaign_id, timestamp),
                    )
                    if cursor.rowcount != 1:
                        await self._music_db.commit()
                        return None
                else:
                    async with self._music_db.execute(
                        "SELECT slot_no FROM music_campaign_outbox "
                        "WHERE week_key = ? AND slot_no IS NOT NULL "
                        "ORDER BY slot_no",
                        (week,),
                    ) as cur:
                        reserved = {int(row[0]) for row in await cur.fetchall()}
                    slot_no = None
                    if len(reserved) < weekly_limit:
                        slot_no = next(
                            (
                                slot for slot in range(1, weekly_limit + 1)
                                if slot not in reserved
                            ),
                            None,
                        )
                    if slot_no is None:
                        await self._music_db.commit()
                        return None
                    async with self._music_db.execute(
                        "SELECT campaign_id "
                        "FROM music_campaign_outbox WHERE status = 'queued' "
                        "AND eligible_at <= ? AND "
                        "(expires_at IS NULL OR expires_at > ?) "
                        "ORDER BY priority DESC, eligible_at, campaign_id LIMIT 1",
                        (timestamp, timestamp),
                    ) as cur:
                        ready = await cur.fetchone()
                    if ready is None:
                        await self._music_db.commit()
                        return None
                    campaign_id = int(ready[0])
                    async with self._music_db.execute(
                        "SELECT COALESCE(MAX(user_id), 0) FROM users "
                        "WHERE is_active = 1",
                    ) as cur:
                        upper = await cur.fetchone()
                    audience_upper = int(upper[0]) if upper else 0
                    cursor = await self._music_db.execute(
                        "UPDATE music_campaign_outbox SET status = 'sending', "
                        "week_key = ?, slot_no = ?, audience_upper_user_id = ?, "
                        "broadcast_cursor = 0, broadcast_sent = 0, "
                        "broadcast_inactive = 0, broadcast_failed = 0, "
                        "runner_token = ?, runner_lease_until = ?, "
                        "started_at = ? WHERE campaign_id = ? "
                        "AND status = 'queued'",
                        (
                            week, slot_no, audience_upper, token, lease_until,
                            timestamp, campaign_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("music campaign slot claim was lost")

                async with self._music_db.execute(
                    "SELECT " + self._music_campaign_select()
                    + " FROM music_campaign_outbox WHERE campaign_id = ?",
                    (campaign_id,),
                ) as cur:
                    row = await cur.fetchone()
                await self._music_db.commit()
            except BaseException:
                await self._music_db.rollback()
                raise
        campaign = self._decode_music_campaign_row(row)
        if campaign is None:
            raise RuntimeError("claimed music campaign payload is invalid")
        return campaign

    async def checkpoint_music_campaign(
        self,
        campaign_id: int,
        runner_token: str,
        user_id: int,
        outcome: str,
        *,
        lease_seconds: int = 120,
    ) -> bool:
        """Advance one terminal recipient and renew the delivery lease."""
        columns = {
            "sent": "broadcast_sent",
            "inactive": "broadcast_inactive",
            "failed": "broadcast_failed",
        }
        column = columns.get(outcome)
        if column is None:
            raise ValueError(f"unknown broadcast outcome: {outcome!r}")
        recipient = int(user_id)
        cursor = await self._music_db.execute(
            f"UPDATE music_campaign_outbox SET broadcast_cursor = ?, "
            f"{column} = {column} + 1, runner_lease_until = ? "
            "WHERE campaign_id = ? AND runner_token = ? "
            "AND status = 'sending' AND broadcast_cursor < ? "
            "AND audience_upper_user_id >= ?",
            (
                recipient, int(time.time()) + max(30, int(lease_seconds)),
                int(campaign_id), runner_token, recipient, recipient,
            ),
        )
        await self._music_db.commit()
        return cursor.rowcount == 1

    async def complete_music_campaign(
        self, campaign_id: int, runner_token: str,
    ) -> bool:
        cursor = await self._music_db.execute(
            "UPDATE music_campaign_outbox SET status = 'completed', "
            "runner_token = NULL, runner_lease_until = NULL, completed_at = ? "
            "WHERE campaign_id = ? AND runner_token = ? "
            "AND status = 'sending'",
            (int(time.time()), int(campaign_id), runner_token),
        )
        await self._music_db.commit()
        return cursor.rowcount == 1

    async def release_music_campaign(
        self, campaign_id: int, runner_token: str,
    ) -> None:
        """Make a graceful-shutdown campaign immediately reclaimable."""
        await self._music_db.execute(
            "UPDATE music_campaign_outbox SET runner_token = NULL, "
            "runner_lease_until = NULL WHERE campaign_id = ? "
            "AND runner_token = ? AND status = 'sending'",
            (int(campaign_id), runner_token),
        )
        await self._music_db.commit()

    async def fail_music_campaign(
        self, campaign_id: int, runner_token: str,
    ) -> bool:
        """Terminally reject an invalid frozen payload without freeing its slot."""
        cursor = await self._music_db.execute(
            "UPDATE music_campaign_outbox SET status = 'failed', "
            "runner_token = NULL, runner_lease_until = NULL, completed_at = ? "
            "WHERE campaign_id = ? AND runner_token = ? "
            "AND status = 'sending'",
            (int(time.time()), int(campaign_id), runner_token),
        )
        await self._music_db.commit()
        return cursor.rowcount == 1

    # ── per-user favorite playlists ──────────────────
    @staticmethod
    def _favorite_value(item: object, key: str):
        if isinstance(item, dict):
            return item.get(key)
        return getattr(item, key, None)

    @staticmethod
    def _favorite_user_id(user_id: int) -> int:
        if isinstance(user_id, bool):
            raise ValueError("favorite user_id must be a positive integer")
        try:
            value = int(user_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "favorite user_id must be a positive integer"
            ) from exc
        if value <= 0:
            raise ValueError("favorite user_id must be a positive integer")
        return value

    @staticmethod
    def _favorite_video_id(video_id: object) -> str | None:
        if video_id is None or video_id == "":
            return None
        value = str(video_id).strip()
        if _YOUTUBE_VIDEO_ID.fullmatch(value) is None:
            raise ValueError("favorite has an invalid video_id")
        return value

    @staticmethod
    def _favorite_source_url(source_url: object) -> str | None:
        if source_url is None or source_url == "":
            return None
        if not isinstance(source_url, str):
            raise ValueError("favorite source_url must be text")
        value = source_url.strip()
        if not value or len(value) > _FAVORITE_SOURCE_URL_MAX:
            raise ValueError("favorite source_url is empty or too long")
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("favorite source_url must be an HTTP(S) URL")
        return value

    @staticmethod
    def _favorite_track_key(track_key: object) -> str:
        value = str(track_key or "").strip()
        if _FAVORITE_TRACK_KEY.fullmatch(value) is None:
            raise ValueError("favorite has an invalid track_key")
        return value

    @classmethod
    def _favorite_lookup_key(cls, identifier: object) -> str:
        """Accept either the public YouTube ID or the durable opaque key."""
        value = str(identifier or "").strip()
        if _YOUTUBE_VIDEO_ID.fullmatch(value):
            return f"yt:{value}"
        return cls._favorite_track_key(value)

    @classmethod
    def _normalize_favorite_item(
        cls, item: object, *, file_id: str | None = None,
    ) -> dict:
        """Reduce a search/download item to the bounded playlist schema."""
        if item is None:
            raise ValueError("favorite item is required")
        video_id = cls._favorite_video_id(
            cls._favorite_value(item, "video_id")
        )
        source_url = cls._favorite_value(item, "source_url")
        if source_url is None:
            source_url = cls._favorite_value(item, "url")
        source_url = cls._favorite_source_url(source_url)
        if source_url is None and video_id:
            source_url = f"https://www.youtube.com/watch?v={video_id}"
        track_key = cls._favorite_value(item, "track_key")
        source_key = cls._favorite_value(item, "source_key")
        if source_key is None:
            source_key = cls._favorite_value(item, "media_key")
        if track_key is not None:
            track_key = cls._favorite_track_key(track_key)
        elif video_id:
            track_key = f"yt:{video_id}"
        elif source_key is not None:
            if not isinstance(source_key, str) or not source_key.strip():
                raise ValueError("favorite source_key must be non-empty text")
            digest = sha256(source_key.strip().encode("utf-8")).hexdigest()[:40]
            track_key = f"m:{digest}"
        elif source_url:
            digest = sha256(source_url.encode("utf-8")).hexdigest()[:40]
            track_key = f"u:{digest}"
        else:
            raise ValueError(
                "favorite needs video_id, track_key, source_url, "
                "or source_key"
            )

        raw_title = cls._favorite_value(item, "title")
        if not isinstance(raw_title, str):
            raise ValueError("favorite title must be text")
        title = " ".join(raw_title.split())
        if not title or len(title) > _FAVORITE_TITLE_MAX:
            raise ValueError("favorite title is empty or too long")

        duration = cls._favorite_value(item, "duration")
        if duration is not None:
            if isinstance(duration, bool):
                raise ValueError("favorite duration must be an integer")
            try:
                duration = int(duration)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "favorite duration must be an integer"
                ) from exc
            if duration < 0:
                raise ValueError("favorite duration cannot be negative")

        uploader = cls._favorite_value(item, "uploader") or ""
        if not isinstance(uploader, str):
            raise ValueError("favorite uploader must be text")
        uploader = " ".join(uploader.split())
        if len(uploader) > _FAVORITE_UPLOADER_MAX:
            raise ValueError("favorite uploader is too long")

        if file_id is None:
            file_id = cls._favorite_value(item, "file_id")
        if file_id is not None:
            if not isinstance(file_id, str):
                raise ValueError("favorite file_id must be text")
            file_id = file_id.strip()
            if not file_id or len(file_id) > _TELEGRAM_FILE_ID_MAX:
                raise ValueError("favorite file_id is empty or too long")
        return {
            "track_key": track_key,
            "video_id": video_id,
            "source_url": source_url,
            "title": title,
            "duration": duration,
            "uploader": uploader,
            "file_id": file_id,
        }

    @staticmethod
    def _decode_favorite_row(row) -> dict | None:
        if not row:
            return None
        return {
            "track_key": row[0],
            "video_id": row[1],
            "source_url": row[2],
            "title": row[3],
            "duration": int(row[4]) if row[4] is not None else None,
            "uploader": row[5],
            "file_id": row[6],
            "updated_at": int(row[7]),
            "saved_at": int(row[8]) if row[8] is not None else None,
        }

    async def upsert_favorite_track(
        self, item: object, *, file_id: str | None = None,
    ) -> str:
        """Register metadata for a stable heart callback and return its key.

        Catalog registration does not favorite the track for any user. Old
        message buttons remain useful across process restarts, while bounded
        cleanup removes only old catalog rows that nobody has saved.
        """
        favorite = self._normalize_favorite_item(item, file_id=file_id)
        now = int(time.time())
        async with self._favorite_write_lock:
            try:
                await self._db.execute(
                    "INSERT INTO favorite_tracks "
                    "(track_key, video_id, source_url, title, duration, "
                    "uploader, audio_file_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(track_key) DO UPDATE SET video_id = "
                    "COALESCE(excluded.video_id, video_id), source_url = "
                    "COALESCE(excluded.source_url, source_url), "
                    "title = excluded.title, duration = "
                    "COALESCE(excluded.duration, duration), uploader = "
                    "CASE WHEN excluded.uploader = '' THEN uploader "
                    "ELSE excluded.uploader END, audio_file_id = "
                    "COALESCE(excluded.audio_file_id, audio_file_id), "
                    "updated_at = excluded.updated_at",
                    (
                        favorite["track_key"], favorite["video_id"],
                        favorite["source_url"], favorite["title"],
                        favorite["duration"], favorite["uploader"],
                        favorite["file_id"], now,
                    ),
                )
                if now >= self._favorite_prune_after:
                    await self._db.execute(
                        "DELETE FROM favorite_tracks WHERE updated_at < ? "
                        "AND NOT EXISTS (SELECT 1 FROM user_favorites uf "
                        "WHERE uf.track_key = favorite_tracks.track_key)",
                        (now - _UNSAVED_FAVORITE_TRACK_TTL,),
                    )
                    await self._db.execute(
                        "DELETE FROM favorite_tracks WHERE track_key IN ("
                        "SELECT ft.track_key FROM favorite_tracks ft "
                        "WHERE NOT EXISTS (SELECT 1 FROM user_favorites uf "
                        "WHERE uf.track_key = ft.track_key) "
                        "ORDER BY ft.updated_at DESC, ft.track_key DESC "
                        "LIMIT -1 OFFSET ?)",
                        (_UNSAVED_FAVORITE_TRACK_LIMIT,),
                    )
                await self._db.commit()
                if now >= self._favorite_prune_after:
                    self._favorite_prune_after = (
                        now + _FAVORITE_PRUNE_INTERVAL
                    )
            except Exception:
                await self._db.rollback()
                raise
        return favorite["track_key"]

    async def add_favorite(self, user_id: int, track_key: str) -> bool:
        """Idempotently add a registered track to one user's playlist."""
        owner = self._favorite_user_id(user_id)
        key = self._favorite_lookup_key(track_key)
        async with self._favorite_write_lock:
            try:
                await self._db.execute(
                    "INSERT INTO users (user_id) VALUES (?) "
                    "ON CONFLICT(user_id) DO UPDATE SET is_active = 1, "
                    "deactivated_at = NULL",
                    (owner,),
                )
                cursor = await self._db.execute(
                    "INSERT OR IGNORE INTO user_favorites "
                    "(user_id, track_key, saved_at) "
                    "SELECT ?, track_key, ? FROM favorite_tracks "
                    "WHERE track_key = ?",
                    (owner, time.time_ns(), key),
                )
                if cursor.rowcount == 0:
                    async with self._db.execute(
                        "SELECT 1 FROM favorite_tracks WHERE track_key = ?",
                        (key,),
                    ) as cur:
                        exists = await cur.fetchone()
                    if not exists:
                        raise KeyError("favorite track is no longer available")
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise
        return cursor.rowcount == 1

    async def remove_favorite(self, user_id: int, identifier: str) -> bool:
        """Idempotently remove by video ID/track key; report whether it changed."""
        owner = self._favorite_user_id(user_id)
        track_key = self._favorite_lookup_key(identifier)
        async with self._favorite_write_lock:
            cursor = await self._db.execute(
                "DELETE FROM user_favorites WHERE user_id = ? AND track_key = ?",
                (owner, track_key),
            )
            await self._db.commit()
        return cursor.rowcount == 1

    async def toggle_favorite(
        self, user_id: int, track_key: str,
    ) -> bool:
        """Atomically toggle one track; return ``True`` when it is now saved.

        Button handlers should normally use explicit ``add_favorite`` and
        ``remove_favorite`` actions so a stale/retried callback is idempotent.
        This method exists for interfaces that intentionally expose a toggle.
        """
        owner = self._favorite_user_id(user_id)
        key = self._favorite_lookup_key(track_key)
        async with self._favorite_write_lock:
            try:
                cursor = await self._db.execute(
                    "DELETE FROM user_favorites "
                    "WHERE user_id = ? AND track_key = ?",
                    (owner, key),
                )
                if cursor.rowcount == 1:
                    await self._db.commit()
                    return False
                await self._db.execute(
                    "INSERT INTO users (user_id) VALUES (?) "
                    "ON CONFLICT(user_id) DO UPDATE SET is_active = 1, "
                    "deactivated_at = NULL",
                    (owner,),
                )
                inserted = await self._db.execute(
                    "INSERT OR IGNORE INTO user_favorites "
                    "(user_id, track_key, saved_at) "
                    "SELECT ?, track_key, ? FROM favorite_tracks "
                    "WHERE track_key = ?",
                    (owner, time.time_ns(), key),
                )
                if inserted.rowcount == 0:
                    async with self._db.execute(
                        "SELECT 1 FROM favorite_tracks WHERE track_key = ?",
                        (key,),
                    ) as cur:
                        exists = await cur.fetchone()
                    if not exists:
                        raise KeyError("favorite track is no longer available")
                await self._db.commit()
                return True
            except Exception:
                await self._db.rollback()
                raise

    async def is_favorite(self, user_id: int, identifier: str) -> bool:
        owner = self._favorite_user_id(user_id)
        track_key = self._favorite_lookup_key(identifier)
        async with self._db.execute(
            "SELECT 1 FROM user_favorites "
            "WHERE user_id = ? AND track_key = ?",
            (owner, track_key),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_favorite(self, user_id: int, identifier: str) -> dict | None:
        owner = self._favorite_user_id(user_id)
        track_key = self._favorite_lookup_key(identifier)
        async with self._db.execute(
            "SELECT ft.track_key, ft.video_id, ft.source_url, ft.title, "
            "ft.duration, ft.uploader, ft.audio_file_id, ft.updated_at, "
            "uf.saved_at FROM user_favorites uf JOIN favorite_tracks ft "
            "ON ft.track_key = uf.track_key "
            "WHERE uf.user_id = ? AND uf.track_key = ?",
            (owner, track_key),
        ) as cur:
            row = await cur.fetchone()
        return self._decode_favorite_row(row)

    async def get_favorite_track(self, track_key: str) -> dict | None:
        """Resolve catalog metadata for a durable heart callback."""
        key = self._favorite_lookup_key(track_key)
        async with self._db.execute(
            "SELECT track_key, video_id, source_url, title, duration, "
            "uploader, audio_file_id, updated_at, NULL "
            "FROM favorite_tracks WHERE track_key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()
        return self._decode_favorite_row(row)

    async def list_favorites(
        self, user_id: int, *, limit: int = 10, offset: int = 0,
    ) -> list[dict]:
        """Return one bounded, stable newest-first playlist page."""
        owner = self._favorite_user_id(user_id)
        page_size = max(1, min(int(limit), 100))
        page_offset = max(0, int(offset))
        async with self._db.execute(
            "SELECT ft.track_key, ft.video_id, ft.source_url, ft.title, "
            "ft.duration, ft.uploader, ft.audio_file_id, ft.updated_at, "
            "uf.saved_at FROM user_favorites uf JOIN favorite_tracks ft "
            "ON ft.track_key = uf.track_key WHERE uf.user_id = ? "
            "ORDER BY uf.saved_at DESC, uf.track_key DESC LIMIT ? OFFSET ?",
            (owner, page_size, page_offset),
        ) as cur:
            rows = await cur.fetchall()
        return [self._decode_favorite_row(row) for row in rows]

    async def count_favorites(self, user_id: int) -> int:
        owner = self._favorite_user_id(user_id)
        async with self._db.execute(
            "SELECT COUNT(*) FROM user_favorites WHERE user_id = ?",
            (owner,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def set_favorite_file_id(
        self, track_key: str, file_id: str | None,
    ) -> bool:
        """Refresh or clear Telegram's reusable audio handle for a favorite."""
        key = self._favorite_lookup_key(track_key)
        if file_id is not None:
            if not isinstance(file_id, str):
                raise ValueError("favorite file_id must be text")
            file_id = file_id.strip()
            if not file_id or len(file_id) > _TELEGRAM_FILE_ID_MAX:
                raise ValueError("favorite file_id is empty or too long")
        async with self._favorite_write_lock:
            cursor = await self._db.execute(
                "UPDATE favorite_tracks SET audio_file_id = ?, updated_at = ? "
                "WHERE track_key = ?",
                (file_id, int(time.time()), key),
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
        """Delete directly-associated user, favorite, recognition, and session data."""
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
        favorite_cursor = await self._db.execute(
            "DELETE FROM user_favorites WHERE user_id = ?", (user_id,)
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
            + max(0, favorite_cursor.rowcount)
        )
