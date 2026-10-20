"""Plugin-owned SQLite state for Telegram Business Mode.

Two tables, both living in the plugin's own database file
(``<HERMES_HOME>/telegram-business/state.db``) — the plugin never touches
Hermes' core ``state.db`` schema:

  business_connections — one row per Telegram Business account that linked
    the bot via BotFather Business Mode. Updated by BusinessConnection
    updates (established / edited / ended).

  business_drafts — pending owner-approval drafts. Created when a customer
    messages a connected chat and the manager produces a candidate reply;
    resolved when the owner taps Send / Edit / Discard or when it expires.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS business_connections (
    connection_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    owner_chat_id TEXT NOT NULL,
    owner_revision INTEGER NOT NULL DEFAULT 1,
    can_reply INTEGER NOT NULL DEFAULT 0,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    auto_draft INTEGER NOT NULL DEFAULT 1,
    paused_chats TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_biz_conn_owner
    ON business_connections(owner_user_id);

CREATE TABLE IF NOT EXISTS business_drafts (
    draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL DEFAULT '',
    owner_chat_id TEXT NOT NULL,
    owner_revision INTEGER NOT NULL DEFAULT 0,
    customer_chat_id TEXT NOT NULL,
    customer_msg_id TEXT,
    customer_text TEXT NOT NULL,
    draft_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    owner_message_id TEXT,
    final_sent_text TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_biz_drafts_conn_customer
    ON business_drafts(connection_id, customer_chat_id, status);
"""


_DRAFT_TRANSITIONS = {
    "pending": {
        "sending", "awaiting_edit", "discarded", "expired", "superseded",
    },
    "awaiting_edit": {"sending", "pending", "expired", "superseded"},
    "sending": {"sent", "edited", "failed", "superseded"},
}


class BusinessStateDB:
    """Thread-safe SQLite store for connections + drafts."""

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.executescript(_SCHEMA)
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    self._migrate_schema()
                    self._conn.commit()
                except BaseException:
                    try:
                        self._conn.rollback()
                    except BaseException:
                        pass
                    raise
        except BaseException:
            try:
                self._conn.close()
            except BaseException:
                pass
            raise

    def _migrate_schema(self) -> None:
        """Add ownership snapshot columns without replacing legacy tables."""
        connection_columns = {
            row["name"]
            for row in self._conn.execute(
                "PRAGMA table_info(business_connections)"
            ).fetchall()
        }
        if "owner_revision" not in connection_columns:
            self._conn.execute(
                "ALTER TABLE business_connections "
                "ADD COLUMN owner_revision INTEGER NOT NULL DEFAULT 1"
            )

        draft_columns = {
            row["name"]
            for row in self._conn.execute(
                "PRAGMA table_info(business_drafts)"
            ).fetchall()
        }
        if "owner_user_id" not in draft_columns:
            self._conn.execute(
                "ALTER TABLE business_drafts "
                "ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''"
            )
        if "owner_revision" not in draft_columns:
            self._conn.execute(
                "ALTER TABLE business_drafts "
                "ADD COLUMN owner_revision INTEGER NOT NULL DEFAULT 0"
            )

        now = time.time()
        untrustworthy_snapshot = "(owner_user_id = '' OR owner_revision <= 0)"
        self._conn.execute(
            "UPDATE business_drafts SET status = 'superseded', updated_at = ? "
            "WHERE status IN ('pending', 'awaiting_edit') AND "
            + untrustworthy_snapshot,
            (now,),
        )
        # A legacy sending row has an unknowable delivery result. Preserve it
        # as terminal history and never make it retryable.
        self._conn.execute(
            "UPDATE business_drafts SET status = 'failed', updated_at = ? "
            "WHERE status = 'sending' AND " + untrustworthy_snapshot,
            (now,),
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def upsert_telegram_business_connection(
        self,
        *,
        connection_id: str,
        owner_user_id: str,
        owner_chat_id: str,
        can_reply: bool,
        is_enabled: bool,
    ) -> None:
        """Insert or update a connection row.

        Preserves ``auto_draft`` and ``paused_chats`` across updates so the
        owner's preferences survive Telegram re-issuing the connection.
        """
        now = time.time()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                previous = self._conn.execute(
                    "SELECT owner_user_id, owner_chat_id, owner_revision "
                    "FROM business_connections WHERE connection_id = ?",
                    (str(connection_id),),
                ).fetchone()
                owner_changed = previous is not None and (
                    str(previous["owner_user_id"]) != str(owner_user_id)
                    or str(previous["owner_chat_id"]) != str(owner_chat_id)
                )
                owner_revision = (
                    1 if previous is None
                    else int(previous["owner_revision"]) + (1 if owner_changed else 0)
                )
                if owner_changed:
                    self._conn.execute(
                        "UPDATE business_drafts "
                        "SET status = 'superseded', updated_at = ? "
                        "WHERE connection_id = ? "
                        "AND status IN ('pending', 'awaiting_edit')",
                        (now, str(connection_id)),
                    )
                self._conn.execute(
                    """
                    INSERT INTO business_connections (
                        connection_id, owner_user_id, owner_chat_id, owner_revision,
                        can_reply, is_enabled, auto_draft, paused_chats,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, '[]', ?, ?)
                    ON CONFLICT(connection_id) DO UPDATE SET
                        owner_user_id = excluded.owner_user_id,
                        owner_chat_id = excluded.owner_chat_id,
                        owner_revision = excluded.owner_revision,
                        can_reply = excluded.can_reply,
                        is_enabled = excluded.is_enabled,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(connection_id), str(owner_user_id), str(owner_chat_id),
                        owner_revision,
                        1 if can_reply else 0, 1 if is_enabled else 0, now, now,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _conn_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["paused_chats"] = json.loads(d.get("paused_chats") or "[]")
        except (TypeError, ValueError):
            d["paused_chats"] = []
        d["can_reply"] = bool(d.get("can_reply"))
        d["is_enabled"] = bool(d.get("is_enabled"))
        d["auto_draft"] = bool(d.get("auto_draft", 1))
        return d

    def get_telegram_business_connection(
        self, connection_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM business_connections WHERE connection_id = ?",
                (str(connection_id),),
            ).fetchone()
        return self._conn_row_to_dict(row) if row is not None else None

    def list_telegram_business_connections(
        self, *, owner_user_id: Optional[str] = None, enabled_only: bool = True
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM business_connections"
        clauses: List[str] = []
        params: List[Any] = []
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(str(owner_user_id))
        if enabled_only:
            clauses.append("is_enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._conn_row_to_dict(r) for r in rows]

    def set_telegram_business_auto_draft(
        self, connection_id: str, *, auto_draft: bool
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE business_connections SET auto_draft = ?, updated_at = ? "
                "WHERE connection_id = ?",
                (1 if auto_draft else 0, time.time(), str(connection_id)),
            )
            self._conn.commit()

    def set_telegram_business_paused_chats(
        self, connection_id: str, paused_chats: List[str]
    ) -> None:
        payload = json.dumps([str(c) for c in (paused_chats or [])])
        with self._lock:
            self._conn.execute(
                "UPDATE business_connections SET paused_chats = ?, updated_at = ? "
                "WHERE connection_id = ?",
                (payload, time.time(), str(connection_id)),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Drafts
    # ------------------------------------------------------------------

    def create_telegram_business_draft(
        self,
        *,
        connection_id: str,
        owner_chat_id: str,
        customer_chat_id: str,
        customer_msg_id: Optional[str],
        customer_text: str,
        draft_text: str,
        owner_user_id: Optional[str] = None,
        owner_revision: Optional[int] = None,
        ttl_seconds: float = 86400.0,
    ) -> Optional[int]:
        """Insert a pending draft row and return its draft_id.

        Any prior pending drafts for the same (connection, customer_chat)
        are marked superseded so only one Send button is ever live per
        conversation.
        """
        now = time.time()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                connection = self._conn.execute(
                    "SELECT owner_user_id, owner_chat_id, owner_revision "
                    "FROM business_connections WHERE connection_id = ?",
                    (str(connection_id),),
                ).fetchone()
                if connection is None:
                    self._conn.commit()
                    return None
                if str(connection["owner_chat_id"]) != str(owner_chat_id):
                    self._conn.commit()
                    return None
                if (
                    owner_user_id is not None
                    and str(connection["owner_user_id"]) != str(owner_user_id)
                ):
                    self._conn.commit()
                    return None
                if (
                    owner_revision is not None
                    and int(connection["owner_revision"]) != int(owner_revision)
                ):
                    self._conn.commit()
                    return None

                snapshot_user_id = str(connection["owner_user_id"])
                snapshot_revision = int(connection["owner_revision"])
                self._conn.execute(
                    "UPDATE business_drafts SET status = 'superseded', updated_at = ? "
                    "WHERE connection_id = ? AND customer_chat_id = ? "
                    "AND status IN ('pending', 'awaiting_edit')",
                    (now, str(connection_id), str(customer_chat_id)),
                )
                cur = self._conn.execute(
                    """
                    INSERT INTO business_drafts (
                        connection_id, owner_user_id, owner_chat_id, owner_revision,
                        customer_chat_id, customer_msg_id, customer_text, draft_text,
                        status, created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        str(connection_id), snapshot_user_id, str(owner_chat_id),
                        snapshot_revision, str(customer_chat_id),
                        str(customer_msg_id) if customer_msg_id is not None else None,
                        customer_text, draft_text,
                        now, now, now + max(60.0, float(ttl_seconds)),
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid or 0)
            except Exception:
                self._conn.rollback()
                raise

    def get_telegram_business_draft(self, draft_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM business_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_telegram_business_draft_owner_message(
        self, draft_id: int, owner_message_id: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE business_drafts SET owner_message_id = ?, updated_at = ? "
                "WHERE draft_id = ?",
                (str(owner_message_id), time.time(), int(draft_id)),
            )
            self._conn.commit()

    def resolve_telegram_business_draft(
        self,
        draft_id: int,
        *,
        status: str,
        final_sent_text: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically mark a draft sent / edited / discarded / expired.

        Returns the prior row, or None if the draft no longer exists or was
        already resolved (so callbacks for stale buttons no-op safely).
        """
        if status not in {"sent", "edited", "discarded", "expired"}:
            raise ValueError(f"invalid business draft status: {status}")
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM business_drafts WHERE draft_id = ? AND status = 'pending'",
                (int(draft_id),),
            ).fetchone()
            if row is None:
                return None
            cur = self._conn.execute(
                "UPDATE business_drafts SET status = ?, "
                "final_sent_text = COALESCE(?, final_sent_text), "
                "updated_at = ? WHERE draft_id = ? AND status = 'pending'",
                (status, final_sent_text, now, int(draft_id)),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                return None
            return dict(row)

    def transition_telegram_business_draft(
        self,
        draft_id: int,
        *,
        from_status: str,
        to_status: str,
        final_sent_text: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically perform one explicit draft-state transition.

        Pending and edit-capture claims enforce ``expires_at`` in the same
        critical section. If the draft is overdue it becomes ``expired`` and
        the requested transition fails. Returning the prior row lets callers
        use its delivery fields only after winning the claim.
        """
        allowed = _DRAFT_TRANSITIONS.get(from_status, set())
        if to_status not in allowed:
            raise ValueError(
                f"invalid business draft transition: {from_status} -> {to_status}"
            )

        action_time = time.time() if now is None else float(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM business_drafts "
                "WHERE draft_id = ? AND status = ?",
                (int(draft_id), from_status),
            ).fetchone()
            if row is None:
                return None

            if (
                from_status in {"pending", "awaiting_edit"}
                and to_status != "expired"
                and float(row["expires_at"]) <= action_time
            ):
                self._conn.execute(
                    "UPDATE business_drafts SET status = 'expired', updated_at = ? "
                    "WHERE draft_id = ? AND status = ?",
                    (action_time, int(draft_id), from_status),
                )
                self._conn.commit()
                return None

            cur = self._conn.execute(
                "UPDATE business_drafts SET status = ?, "
                "final_sent_text = COALESCE(?, final_sent_text), updated_at = ? "
                "WHERE draft_id = ? AND status = ?",
                (
                    to_status,
                    final_sent_text,
                    action_time,
                    int(draft_id),
                    from_status,
                ),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                return None
            return dict(row)

    def release_telegram_business_draft_for_retry(
        self,
        draft_id: int,
        *,
        retry_status: str,
        expected_owner_user_id: Optional[str] = None,
        expected_owner_chat_id: Optional[str] = None,
        expected_owner_revision: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically release a pre-delivery failure without reviving stale work.

        A sending draft may return to ``pending`` or ``awaiting_edit`` only if
        it is unexpired, still belongs to the expected connection owner, and
        no higher draft id exists for the same business conversation. The
        returned row reflects the decided terminal/retry status so callers can
        give accurate UX and restore capture safely.
        """
        if retry_status not in {"pending", "awaiting_edit"}:
            raise ValueError(f"invalid business draft retry status: {retry_status}")

        action_time = time.time() if now is None else float(now)
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE business_drafts AS old
                SET status = CASE
                        WHEN old.expires_at <= ? THEN 'expired'
                        WHEN NOT EXISTS (
                            SELECT 1
                            FROM business_connections AS connection
                            WHERE connection.connection_id = old.connection_id
                              AND connection.owner_user_id = old.owner_user_id
                              AND connection.owner_chat_id = old.owner_chat_id
                              AND connection.owner_revision = old.owner_revision
                              AND (
                                  ? IS NULL
                                  OR old.owner_user_id = ?
                              )
                              AND (
                                  ? IS NULL
                                  OR old.owner_chat_id = ?
                              )
                              AND (
                                  ? IS NULL
                                  OR old.owner_revision = ?
                              )
                        ) THEN 'superseded'
                        WHEN EXISTS (
                            SELECT 1
                            FROM business_drafts AS newer
                            WHERE newer.connection_id = old.connection_id
                              AND newer.customer_chat_id = old.customer_chat_id
                              AND newer.draft_id > old.draft_id
                        ) THEN 'superseded'
                        ELSE ?
                    END,
                    updated_at = ?
                WHERE old.draft_id = ? AND old.status = 'sending'
                """,
                (
                    action_time,
                    (
                        str(expected_owner_user_id)
                        if expected_owner_user_id is not None else None
                    ),
                    (
                        str(expected_owner_user_id)
                        if expected_owner_user_id is not None else None
                    ),
                    (
                        str(expected_owner_chat_id)
                        if expected_owner_chat_id is not None else None
                    ),
                    (
                        str(expected_owner_chat_id)
                        if expected_owner_chat_id is not None else None
                    ),
                    (
                        int(expected_owner_revision)
                        if expected_owner_revision is not None else None
                    ),
                    (
                        int(expected_owner_revision)
                        if expected_owner_revision is not None else None
                    ),
                    retry_status,
                    action_time,
                    int(draft_id),
                ),
            )
            if cur.rowcount != 1:
                self._conn.commit()
                return None
            row = self._conn.execute(
                "SELECT * FROM business_drafts WHERE draft_id = ?",
                (int(draft_id),),
            ).fetchone()
            self._conn.commit()
            return dict(row) if row is not None else None

    def invalidate_awaiting_edit_drafts(self) -> int:
        """Terminally close edit rows whose in-memory capture was lost."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE business_drafts SET status = 'superseded', updated_at = ? "
                "WHERE status = 'awaiting_edit'",
                (now,),
            )
            self._conn.commit()
            return int(cur.rowcount)

    def get_pending_telegram_business_drafts_for_owner(
        self, owner_chat_id: str
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM business_drafts WHERE owner_chat_id = ? "
                "AND status = 'pending' ORDER BY created_at ASC",
                (str(owner_chat_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def expire_telegram_business_drafts(self, *, now: Optional[float] = None) -> int:
        cutoff = now if now is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE business_drafts SET status = 'expired', updated_at = ? "
                "WHERE status IN ('pending', 'awaiting_edit') AND expires_at < ?",
                (cutoff, cutoff),
            )
            self._conn.commit()
            return int(cur.rowcount)
