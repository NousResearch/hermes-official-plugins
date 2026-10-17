"""Tests for Telegram Business Mode (Secretary Bots).

Covers two layers:
  - ``state.BusinessStateDB`` business-connection + draft CRUD
  - ``manager.BusinessModeManager`` orchestration

The manager tests use lightweight async stubs for the adapter callbacks
(``send_message``, ``draft_generator``) so the agent loop / network are
never touched — pure unit-level coverage of the state machine.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from telegram_business_plugin.state import BusinessStateDB


@pytest.fixture()
def db(tmp_path):
    """Fresh plugin state DB."""
    db_path = tmp_path / "biz_state.db"
    sdb = BusinessStateDB(db_path)
    yield sdb
    sdb.close()


# =========================================================================
# Schema / CRUD
# =========================================================================


class TestBusinessConnectionPersistence:
    def test_upsert_and_get(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="100", can_reply=True, is_enabled=True,
        )
        row = db.get_telegram_business_connection("c1")
        assert row is not None
        assert row["connection_id"] == "c1"
        assert row["owner_user_id"] == "42"
        assert row["owner_chat_id"] == "100"
        assert row["can_reply"] is True
        assert row["is_enabled"] is True
        assert row["auto_draft"] is True
        assert row["paused_chats"] == []

    def test_upsert_preserves_auto_draft_and_paused(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="100", can_reply=True, is_enabled=True,
        )
        db.set_telegram_business_auto_draft("c1", auto_draft=False)
        db.set_telegram_business_paused_chats("c1", ["200", "300"])
        # Simulate Telegram re-sending the BusinessConnection
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="100", can_reply=False, is_enabled=True,
        )
        row = db.get_telegram_business_connection("c1")
        # can_reply updated, but owner preferences kept
        assert row["can_reply"] is False
        assert row["auto_draft"] is False
        assert sorted(row["paused_chats"]) == ["200", "300"]

    def test_list_enabled_filter(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="100", can_reply=True, is_enabled=True,
        )
        db.upsert_telegram_business_connection(
            connection_id="c2", owner_user_id="42",
            owner_chat_id="100", can_reply=False, is_enabled=False,
        )
        active = db.list_telegram_business_connections(
            owner_user_id="42", enabled_only=True,
        )
        all_rows = db.list_telegram_business_connections(
            owner_user_id="42", enabled_only=False,
        )
        assert len(active) == 1 and active[0]["connection_id"] == "c1"
        assert len(all_rows) == 2

    def test_get_returns_none_for_unknown(self, db):
        assert db.get_telegram_business_connection("missing") is None

    def test_owner_change_supersedes_unresolved_old_owner_drafts(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="100", can_reply=True, is_enabled=True,
        )
        pending = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="pending", draft_text="reply 1",
        )
        awaiting_edit = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="201",
            customer_msg_id="m2", customer_text="editing", draft_text="reply 2",
        )
        sending = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="202",
            customer_msg_id="m3", customer_text="sending", draft_text="reply 3",
        )
        assert db.transition_telegram_business_draft(
            awaiting_edit, from_status="pending", to_status="awaiting_edit",
        ) is not None
        assert db.transition_telegram_business_draft(
            sending, from_status="pending", to_status="sending",
        ) is not None

        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="84",
            owner_chat_id="101", can_reply=True, is_enabled=True,
        )

        assert db.get_telegram_business_draft(pending)["status"] == "superseded"
        assert db.get_telegram_business_draft(awaiting_edit)["status"] == "superseded"
        assert db.get_telegram_business_draft(sending)["status"] == "sending"



class TestBusinessDraftLifecycle:
    @pytest.fixture(autouse=True)
    def _conn(self, db):
        db.upsert_telegram_business_connection(
            connection_id="c1", owner_user_id="42",
            owner_chat_id="100", can_reply=True, is_enabled=True,
        )

    def test_create_and_get(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
        )
        row = db.get_telegram_business_draft(did)
        assert row is not None
        assert row["customer_text"] == "hi"
        assert row["draft_text"] == "hello"
        assert row["status"] == "pending"
        assert row["owner_message_id"] is None
        assert row["final_sent_text"] is None
        assert row["expires_at"] > row["created_at"]

    def test_new_draft_supersedes_prior_pending(self, db):
        d1 = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
        )
        d2 = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m2", customer_text="hi again", draft_text="hello again",
        )
        assert d1 != d2
        # Prior draft should be marked superseded.
        prior = db.get_telegram_business_draft(d1)
        new = db.get_telegram_business_draft(d2)
        assert prior["status"] == "superseded"
        assert new["status"] == "pending"

    def test_supersede_only_affects_same_customer_chat(self, db):
        d1 = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="A", draft_text="a",
        )
        d2 = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="999",
            customer_msg_id="m2", customer_text="B", draft_text="b",
        )
        # Different customer chats — d1 should still be pending.
        assert db.get_telegram_business_draft(d1)["status"] == "pending"
        assert db.get_telegram_business_draft(d2)["status"] == "pending"

    def test_resolve_sent_returns_prior_and_blocks_double_resolve(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
        )
        first = db.resolve_telegram_business_draft(
            did, status="sent", final_sent_text="hello",
        )
        assert first is not None
        # Status now committed
        row = db.get_telegram_business_draft(did)
        assert row["status"] == "sent"
        assert row["final_sent_text"] == "hello"
        # Second resolution must be a no-op.
        second = db.resolve_telegram_business_draft(did, status="discarded")
        assert second is None
        assert db.get_telegram_business_draft(did)["status"] == "sent"

    def test_resolve_invalid_status_raises(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
        )
        with pytest.raises(ValueError):
            db.resolve_telegram_business_draft(did, status="bogus")

    def test_stale_resolver_cannot_overwrite_sent_from_other_connection(
        self, tmp_path,
    ):
        db_path = tmp_path / "stale-resolver.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        selected = threading.Event()
        resume = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)

        class _PausingCursor:
            def __init__(self, cursor):
                self._cursor = cursor

            def fetchone(self):
                row = self._cursor.fetchone()
                selected.set()
                assert resume.wait(timeout=5)
                return row

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        class _PausingConnection:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, sql, parameters=()):
                cursor = self._connection.execute(sql, parameters)
                if (
                    "SELECT * FROM business_drafts" in sql
                    and "status = 'pending'" in sql
                ):
                    return _PausingCursor(cursor)
                return cursor

            def __getattr__(self, name):
                return getattr(self._connection, name)

        try:
            first_db.upsert_telegram_business_connection(
                connection_id="c1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            draft_id = first_db.create_telegram_business_draft(
                connection_id="c1", owner_chat_id="100",
                customer_chat_id="200", customer_msg_id="m1",
                customer_text="hi", draft_text="hello",
            )
            first_db._conn = _PausingConnection(first_db._conn)

            stale_result = executor.submit(
                first_db.resolve_telegram_business_draft,
                draft_id,
                status="discarded",
            )
            assert selected.wait(timeout=5)
            assert second_db.transition_telegram_business_draft(
                draft_id, from_status="pending", to_status="sending",
            ) is not None
            assert second_db.transition_telegram_business_draft(
                draft_id, from_status="sending", to_status="sent",
                final_sent_text="delivered",
            ) is not None

            resume.set()
            assert stale_result.result(timeout=5) is None
            final = second_db.get_telegram_business_draft(draft_id)
            assert final["status"] == "sent"
            assert final["final_sent_text"] == "delivered"
        finally:
            resume.set()
            executor.shutdown(wait=True)
            second_db.close()
            first_db.close()

    def test_owner_message_id_round_trip(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
        )
        db.set_telegram_business_draft_owner_message(did, "owner_msg_42")
        row = db.get_telegram_business_draft(did)
        assert row["owner_message_id"] == "owner_msg_42"

    def test_expire_only_affects_overdue_pending(self, db):
        d_old = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m_old", customer_text="old", draft_text="old reply",
            ttl_seconds=60.0,
        )
        # Different customer chat so it doesn't supersede d_old.
        d_new = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="201",
            customer_msg_id="m_new", customer_text="new", draft_text="new reply",
            ttl_seconds=999_999.0,
        )
        # Run expiry just past d_old's expiry but well before d_new's.
        d_old_row = db.get_telegram_business_draft(d_old)
        affected = db.expire_telegram_business_drafts(now=d_old_row["expires_at"] + 1.0)
        assert affected == 1
        assert db.get_telegram_business_draft(d_old)["status"] == "expired"
        assert db.get_telegram_business_draft(d_new)["status"] == "pending"

    def test_expiry_cleanup_includes_awaiting_edit(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="old", draft_text="old reply",
            ttl_seconds=60.0,
        )
        row = db.get_telegram_business_draft(did)
        assert db.transition_telegram_business_draft(
            did, from_status="pending", to_status="awaiting_edit",
        ) is not None

        affected = db.expire_telegram_business_drafts(now=row["expires_at"] + 1.0)

        assert affected == 1
        assert db.get_telegram_business_draft(did)["status"] == "expired"

    def test_get_pending_for_owner(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
        )
        # Resolved draft must not appear.
        d2 = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="999",
            customer_msg_id="m2", customer_text="hi2", draft_text="hello2",
        )
        db.resolve_telegram_business_draft(d2, status="discarded")
        pending = db.get_pending_telegram_business_drafts_for_owner("100")
        ids = {row["draft_id"] for row in pending}
        assert did in ids
        assert d2 not in ids

    def test_atomic_claim_allows_only_one_caller(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
        )

        claimed = db.transition_telegram_business_draft(
            did, from_status="pending", to_status="sending",
        )
        duplicate = db.transition_telegram_business_draft(
            did, from_status="pending", to_status="sending",
        )

        assert claimed is not None
        assert claimed["status"] == "pending"
        assert duplicate is None
        assert db.get_telegram_business_draft(did)["status"] == "sending"

    def test_two_database_instances_concurrently_claim_at_most_once(
        self, tmp_path,
    ):
        db_path = tmp_path / "shared_biz_state.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            first_db.upsert_telegram_business_connection(
                connection_id="shared", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            did = first_db.create_telegram_business_draft(
                connection_id="shared", owner_chat_id="100",
                customer_chat_id="200", customer_msg_id="m1",
                customer_text="hi", draft_text="hello",
            )
            barrier = threading.Barrier(3)

            def _claim(store):
                barrier.wait(timeout=5.0)
                return store.transition_telegram_business_draft(
                    did, from_status="pending", to_status="sending",
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(_claim, first_db)
                second = executor.submit(_claim, second_db)
                barrier.wait(timeout=5.0)
                results = [first.result(timeout=5.0), second.result(timeout=5.0)]

            assert sum(result is not None for result in results) == 1
            assert first_db.get_telegram_business_draft(did)["status"] == "sending"
        finally:
            second_db.close()
            first_db.close()

    def test_claim_expires_overdue_draft_at_action_time(self, db, monkeypatch):
        from telegram_business_plugin import state as state_mod

        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
            ttl_seconds=60.0,
        )
        expires_at = db.get_telegram_business_draft(did)["expires_at"]
        monkeypatch.setattr(state_mod.time, "time", lambda: expires_at + 1.0)

        claimed = db.transition_telegram_business_draft(
            did, from_status="pending", to_status="sending",
        )

        assert claimed is None
        assert db.get_telegram_business_draft(did)["status"] == "expired"

    def test_generic_transition_cannot_release_send_claim_for_retry(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
        )
        assert db.transition_telegram_business_draft(
            did, from_status="pending", to_status="sending",
        ) is not None

        with pytest.raises(ValueError):
            db.transition_telegram_business_draft(
                did, from_status="sending", to_status="pending",
            )
        with pytest.raises(ValueError):
            db.transition_telegram_business_draft(
                did, from_status="sending", to_status="awaiting_edit",
            )

        assert db.get_telegram_business_draft(did)["status"] == "sending"

    def test_retry_release_restores_when_no_newer_draft_exists(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
        )
        assert db.transition_telegram_business_draft(
            did, from_status="pending", to_status="sending",
        ) is not None

        released = db.release_telegram_business_draft_for_retry(
            did, retry_status="pending",
        )

        assert released is not None
        assert released["status"] == "pending"
        assert db.get_telegram_business_draft(did)["status"] == "pending"

    def test_retry_release_supersedes_when_newer_same_chat_draft_exists(self, db):
        old = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="old", draft_text="old reply",
        )
        assert db.transition_telegram_business_draft(
            old, from_status="pending", to_status="sending",
        ) is not None
        newer = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m2", customer_text="new", draft_text="new reply",
        )

        released = db.release_telegram_business_draft_for_retry(
            old, retry_status="pending",
        )

        assert released is not None
        assert released["status"] == "superseded"
        assert db.get_telegram_business_draft(old)["status"] == "superseded"
        assert db.get_telegram_business_draft(newer)["status"] == "pending"

    def test_cross_connection_newer_creation_before_release_supersedes_old(
        self, tmp_path,
    ):
        db_path = tmp_path / "release_ordering.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            first_db.upsert_telegram_business_connection(
                connection_id="c1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            old = first_db.create_telegram_business_draft(
                connection_id="c1", owner_chat_id="100", customer_chat_id="200",
                customer_msg_id="m1", customer_text="old", draft_text="old reply",
            )
            assert first_db.transition_telegram_business_draft(
                old, from_status="pending", to_status="sending",
            ) is not None

            newer = second_db.create_telegram_business_draft(
                connection_id="c1", owner_chat_id="100", customer_chat_id="200",
                customer_msg_id="m2", customer_text="new", draft_text="new reply",
            )
            released = first_db.release_telegram_business_draft_for_retry(
                old, retry_status="pending",
            )

            assert released is not None and released["status"] == "superseded"
            assert second_db.get_telegram_business_draft(newer)["status"] == "pending"
        finally:
            second_db.close()
            first_db.close()

    def test_cross_connection_release_before_newer_creation_is_superseded_later(
        self, tmp_path,
    ):
        db_path = tmp_path / "release_then_create.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            first_db.upsert_telegram_business_connection(
                connection_id="c1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            old = first_db.create_telegram_business_draft(
                connection_id="c1", owner_chat_id="100", customer_chat_id="200",
                customer_msg_id="m1", customer_text="old", draft_text="old reply",
            )
            assert first_db.transition_telegram_business_draft(
                old, from_status="pending", to_status="sending",
            ) is not None
            released = first_db.release_telegram_business_draft_for_retry(
                old, retry_status="pending",
            )
            assert released is not None and released["status"] == "pending"

            newer = second_db.create_telegram_business_draft(
                connection_id="c1", owner_chat_id="100", customer_chat_id="200",
                customer_msg_id="m2", customer_text="new", draft_text="new reply",
            )

            assert first_db.get_telegram_business_draft(old)["status"] == "superseded"
            assert second_db.get_telegram_business_draft(newer)["status"] == "pending"
        finally:
            second_db.close()
            first_db.close()

    def test_cross_connection_terminal_newer_draft_still_blocks_old_release(
        self, tmp_path,
    ):
        db_path = tmp_path / "terminal_newer.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            first_db.upsert_telegram_business_connection(
                connection_id="c1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            old = first_db.create_telegram_business_draft(
                connection_id="c1", owner_chat_id="100", customer_chat_id="200",
                customer_msg_id="m1", customer_text="old", draft_text="old reply",
            )
            assert first_db.transition_telegram_business_draft(
                old, from_status="pending", to_status="sending",
            ) is not None
            newer = second_db.create_telegram_business_draft(
                connection_id="c1", owner_chat_id="100", customer_chat_id="200",
                customer_msg_id="m2", customer_text="new", draft_text="new reply",
            )
            assert second_db.transition_telegram_business_draft(
                newer, from_status="pending", to_status="sending",
            ) is not None
            assert second_db.transition_telegram_business_draft(
                newer, from_status="sending", to_status="sent",
                final_sent_text="new reply",
            ) is not None

            released = first_db.release_telegram_business_draft_for_retry(
                old, retry_status="pending",
            )

            assert released is not None and released["status"] == "superseded"
            assert second_db.get_telegram_business_draft(newer)["status"] == "sent"
        finally:
            second_db.close()
            first_db.close()

    def test_retry_release_expires_old_sending_draft(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
            ttl_seconds=60.0,
        )
        expires_at = db.get_telegram_business_draft(did)["expires_at"]
        assert db.transition_telegram_business_draft(
            did, from_status="pending", to_status="sending",
        ) is not None

        released = db.release_telegram_business_draft_for_retry(
            did, retry_status="pending", now=expires_at + 1.0,
        )

        assert released is not None
        assert released["status"] == "expired"
        assert db.get_telegram_business_draft(did)["status"] == "expired"

    def test_invalid_draft_transition_is_rejected(self, db):
        did = db.create_telegram_business_draft(
            connection_id="c1", owner_chat_id="100", customer_chat_id="200",
            customer_msg_id="m1", customer_text="hi", draft_text="hello",
        )
        with pytest.raises(ValueError):
            db.transition_telegram_business_draft(
                did, from_status="pending", to_status="sent",
            )


# =========================================================================
# Manager orchestration
# =========================================================================


def _fake_business_connection(
    *, conn_id="conn1", owner_id=42, owner_chat=100,
    is_enabled=True, can_reply=True,
) -> Any:
    """Build a duck-typed BusinessConnection good enough for the manager."""
    rights = SimpleNamespace(can_reply=can_reply) if can_reply is not None else None
    return SimpleNamespace(
        id=conn_id,
        user=SimpleNamespace(id=owner_id, full_name="Owner"),
        user_chat_id=owner_chat,
        is_enabled=is_enabled,
        rights=rights,
    )


def _fake_business_message(
    *, conn_id="conn1", customer_chat_id=200, customer_id=999,
    text="Hi there", msg_id=42,
) -> Any:
    """Build a duck-typed business_message-style PTB Message."""
    return SimpleNamespace(
        business_connection_id=conn_id,
        chat=SimpleNamespace(id=customer_chat_id, type="private"),
        from_user=SimpleNamespace(
            id=customer_id, full_name="Customer Carol",
            first_name="Carol", username="carol",
        ),
        text=text,
        caption=None,
        message_id=msg_id,
    )


class _SentRecorder:
    """Capture all send_message kwargs the manager produces."""

    def __init__(self, *, fail: bool = False):
        self.calls: List[Dict[str, Any]] = []
        self.fail = fail
        self._next_id = 1000

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("simulated send failure")
        sent = SimpleNamespace(message_id=self._next_id)
        self._next_id += 1
        return sent


class _BlockingFirstCustomerSender(_SentRecorder):
    """Block the first customer delivery while later calls return normally."""

    def __init__(self):
        super().__init__()
        self.first_customer_started = asyncio.Event()
        self.release_first_customer = asyncio.Event()
        self.customer_calls = 0

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("business_connection_id") is not None:
            self.customer_calls += 1
            if self.customer_calls == 1:
                self.first_customer_started.set()
                await self.release_first_customer.wait()
        sent = SimpleNamespace(message_id=self._next_id)
        self._next_id += 1
        return sent


class _BlockingThenPreDeliveryFailureSender(_SentRecorder):
    """Pause the first customer call, then fail it before delivery."""

    def __init__(self):
        super().__init__()
        self.first_customer_started = asyncio.Event()
        self.release_first_customer = asyncio.Event()
        self.customer_calls = 0

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("business_connection_id") is not None:
            self.customer_calls += 1
            if self.customer_calls == 1:
                self.first_customer_started.set()
                await self.release_first_customer.wait()
                error = RuntimeError("definite blocked pre-delivery failure")
                error.delivery_not_attempted = True
                raise error
        sent = SimpleNamespace(message_id=self._next_id)
        self._next_id += 1
        return sent


class _FailFirstCustomerSender(_SentRecorder):
    def __init__(self):
        super().__init__()
        self.customer_calls = 0

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("business_connection_id") is not None:
            self.customer_calls += 1
            if self.customer_calls == 1:
                error = RuntimeError("definite pre-delivery failure")
                error.delivery_not_attempted = True
                raise error
        sent = SimpleNamespace(message_id=self._next_id)
        self._next_id += 1
        return sent


class _AmbiguousCustomerFailureSender(_SentRecorder):
    def __init__(self):
        super().__init__()
        self.customer_calls = 0

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("business_connection_id") is not None:
            self.customer_calls += 1
            raise RuntimeError("delivery outcome unknown")
        return SimpleNamespace(message_id=self._next_id)


class _UpdateInsideCustomerSender(_SentRecorder):
    """Start a manager connection update from inside customer dispatch."""

    def __init__(self, db, update):
        super().__init__()
        self.db = db
        self.update = update
        self.manager = None
        self.customer_started = asyncio.Event()
        self.release_customer = asyncio.Event()
        self.update_task = None
        self.connection_while_sending = None
        self.update_done_while_sending = None

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("business_connection_id") is not None:
            assert self.manager is not None
            self.update_task = asyncio.create_task(
                self.manager.handle_connection_update(self.update)
            )
            await asyncio.sleep(0)
            self.connection_while_sending = (
                self.db.get_telegram_business_connection("conn1")
            )
            self.update_done_while_sending = self.update_task.done()
            self.customer_started.set()
            await self.release_customer.wait()
        sent = SimpleNamespace(message_id=self._next_id)
        self._next_id += 1
        return sent


def _make_manager(db, *, draft_text="Draft reply!", draft_fails=False,
                  send_recorder=None, debounce=0.0):
    from telegram_business_plugin.manager import BusinessModeManager

    async def _draft(customer_text: str, customer_chat_id: str) -> str:
        if draft_fails:
            raise RuntimeError("model boom")
        return draft_text

    sender = send_recorder if send_recorder is not None else _SentRecorder()
    mgr = BusinessModeManager(
        session_db=db,
        send_message=sender,
        draft_generator=_draft,
        debounce_seconds=debounce,
    )
    return mgr, sender


class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_first_connection_persists_and_sends_onboarding(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        row = db.get_telegram_business_connection("conn1")
        assert row is not None
        assert row["is_enabled"] is True
        assert row["can_reply"] is True
        # Owner got the onboarding DM
        assert len(sender.calls) == 1
        assert sender.calls[0]["chat_id"] == 100
        assert "Business assistant" in sender.calls[0]["text"]

    @pytest.mark.asyncio
    async def test_repeat_connection_does_not_resend_onboarding(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        sender.calls.clear()
        await mgr.handle_connection_update(_fake_business_connection())
        assert sender.calls == []

    @pytest.mark.asyncio
    async def test_disconnection_dms_owner(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        sender.calls.clear()
        await mgr.handle_connection_update(
            _fake_business_connection(is_enabled=False)
        )
        assert any("ended" in c["text"].lower() for c in sender.calls)

    @pytest.mark.asyncio
    async def test_can_reply_toggle_notifies_owner(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection(can_reply=True))
        sender.calls.clear()
        await mgr.handle_connection_update(_fake_business_connection(can_reply=False))
        assert sender.calls, "owner should be told send permission flipped"
        assert "OFF" in sender.calls[0]["text"]


class TestBusinessMessageDraftFlow:
    @pytest.mark.asyncio
    async def test_incoming_customer_message_drafts_and_dms_owner(self, db):
        mgr, sender = _make_manager(db, draft_text="Hey, thanks for reaching out!")
        await mgr.handle_connection_update(_fake_business_connection())
        sender.calls.clear()
        await mgr.handle_business_message(_fake_business_message(text="Hi!"))
        assert sender.calls, "draft should have been DM'd to the owner"
        owner_msg = sender.calls[0]
        assert owner_msg["chat_id"] == 100
        assert "Hey, thanks for reaching out!" in owner_msg["text"]
        assert "Hi!" in owner_msg["text"]
        # Inline keyboard rendered (under the test telegram mock,
        # InlineKeyboardMarkup is a MagicMock — we just verify a keyboard
        # was attached and that three callback_datas were produced).
        assert owner_msg.get("reply_markup") is not None
        # One draft row recorded
        drafts = db.get_pending_telegram_business_drafts_for_owner("100")
        assert len(drafts) == 1
        assert drafts[0]["draft_text"] == "Hey, thanks for reaching out!"

    @pytest.mark.asyncio
    async def test_no_can_reply_drops_send_button(self, db, monkeypatch):
        # Capture callback_data passed to InlineKeyboardButton to verify
        # the Send button is omitted when can_reply is False.  We wrap
        # _build_draft_keyboard so we can inspect the choices it produced
        # — the underlying telegram mock makes the resulting keyboard
        # object opaque.
        from telegram_business_plugin import manager as biz_mod

        original = biz_mod.BusinessModeManager._build_draft_keyboard
        captured: List[str] = []

        def _spy(draft_id, *, can_reply):
            if can_reply:
                captured.append("send")
            captured.append("edit")
            captured.append("discard")
            return original(draft_id, can_reply=can_reply)

        monkeypatch.setattr(
            biz_mod.BusinessModeManager,
            "_build_draft_keyboard",
            staticmethod(_spy),
        )

        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection(can_reply=False))
        sender.calls.clear()
        await mgr.handle_business_message(_fake_business_message())
        assert captured == ["edit", "discard"]

    @pytest.mark.asyncio
    async def test_unknown_connection_silently_ignored(self, db):
        mgr, sender = _make_manager(db)
        # No prior handle_connection_update — should silently skip.
        await mgr.handle_business_message(_fake_business_message())
        assert sender.calls == []
        assert db.get_pending_telegram_business_drafts_for_owner("100") == []

    @pytest.mark.asyncio
    async def test_auto_draft_paused_skips_drafting(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        db.set_telegram_business_auto_draft("conn1", auto_draft=False)
        sender.calls.clear()
        await mgr.handle_business_message(_fake_business_message())
        assert sender.calls == []
        assert db.get_pending_telegram_business_drafts_for_owner("100") == []

    @pytest.mark.asyncio
    async def test_paused_customer_chat_skipped(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        db.set_telegram_business_paused_chats("conn1", ["200"])
        sender.calls.clear()
        await mgr.handle_business_message(_fake_business_message(customer_chat_id=200))
        assert sender.calls == []
        # Other chat still drafts.
        await mgr.handle_business_message(_fake_business_message(customer_chat_id=300))
        assert sender.calls

    @pytest.mark.asyncio
    async def test_empty_text_skipped(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        sender.calls.clear()
        await mgr.handle_business_message(_fake_business_message(text=""))
        assert sender.calls == []

    @pytest.mark.asyncio
    async def test_draft_failure_reports_to_owner(self, db):
        mgr, sender = _make_manager(db, draft_fails=True)
        await mgr.handle_connection_update(_fake_business_connection())
        sender.calls.clear()
        await mgr.handle_business_message(_fake_business_message(text="Hi"))
        assert sender.calls
        assert "couldn't draft" in sender.calls[0]["text"]

    @pytest.mark.asyncio
    async def test_debounce_coalesces_burst(self, db):
        # Use a real debounce window and fire 3 messages in quick succession.
        mgr, sender = _make_manager(db, debounce=0.05,
                                    draft_text="single draft")
        await mgr.handle_connection_update(_fake_business_connection())
        sender.calls.clear()
        for i in range(3):
            await mgr.handle_business_message(
                _fake_business_message(text=f"part {i}", msg_id=i)
            )
        # Let the debounce fire.
        await asyncio.sleep(0.2)
        # Only one draft should have been generated and one owner DM sent.
        owner_dms = [c for c in sender.calls if c.get("chat_id") == 100]
        assert len(owner_dms) == 1


class TestCallbackDispatch:
    @pytest.mark.asyncio
    async def test_send_button_delivers_to_customer_chat(self, db):
        mgr, sender = _make_manager(db, draft_text="hello there")
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        sender.calls.clear()
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        did = draft["draft_id"]
        # Fake the inline-button click
        answered: List[Dict[str, Any]] = []
        edited: List[Dict[str, Any]] = []

        async def _answer(**kw): answered.append(kw)
        async def _edit(**kw): edited.append(kw)

        dispatched = await mgr.handle_callback(
            data=f"bd:send:{did}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )
        assert dispatched is True
        # Sent to customer chat with business_connection_id
        sends_to_customer = [
            c for c in sender.calls if c.get("chat_id") == 200
        ]
        assert sends_to_customer
        assert sends_to_customer[0]["business_connection_id"] == "conn1"
        assert sends_to_customer[0]["text"] == "hello there"
        # Draft now sent
        row = db.get_telegram_business_draft(did)
        assert row["status"] == "sent"
        assert row["final_sent_text"] == "hello there"
        # Owner DM was edited to show resolution
        assert edited and "Sent" in edited[0]["text"]

    @pytest.mark.asyncio
    async def test_discard_marks_status(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        did = draft["draft_id"]

        async def _answer(**kw): pass
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data=f"bd:discard:{did}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )
        assert db.get_telegram_business_draft(did)["status"] == "discarded"

    @pytest.mark.asyncio
    async def test_callback_for_unknown_draft_no_ops(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        sender.calls.clear()

        answered: List[Dict[str, Any]] = []

        async def _answer(**kw): answered.append(kw)
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data="bd:send:999999", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )
        assert answered and "expired" in answered[0]["text"].lower()
        # No customer-chat sends.
        assert not any(c.get("chat_id") == 200 for c in sender.calls)

    @pytest.mark.asyncio
    async def test_callback_rejects_non_owner(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        did = draft["draft_id"]
        answered: List[Dict[str, Any]] = []

        async def _answer(**kw): answered.append(kw)
        async def _edit(**kw): pass

        # Different user — should be rejected.
        await mgr.handle_callback(
            data=f"bd:send:{did}", caller_user_id="9999",
            answer=_answer, edit_message_text=_edit,
        )
        assert answered and "Only the connected account owner" in answered[0]["text"]
        assert db.get_telegram_business_draft(did)["status"] == "pending"

    @pytest.mark.asyncio
    async def test_current_owner_cannot_approve_legacy_draft_for_old_owner(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        sender.calls.clear()
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        with db._lock:
            db._conn.execute(
                "UPDATE business_connections "
                "SET owner_user_id = ?, owner_chat_id = ? WHERE connection_id = ?",
                ("84", "101", "conn1"),
            )
            db._conn.commit()
        answers: List[str] = []

        async def _answer(**kw): answers.append(kw.get("text", ""))
        async def _edit(**kw): pass

        assert await mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="84",
            answer=_answer, edit_message_text=_edit,
        ) is True

        assert not any(c.get("chat_id") == 200 for c in sender.calls)
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "superseded"
        assert any("owner" in text.lower() for text in answers)

    @pytest.mark.asyncio
    async def test_owner_change_before_callback_supersedes_old_draft(
        self, tmp_path,
    ):
        db_path = tmp_path / "owner_change_before_callback.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            first_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            draft_id = first_db.create_telegram_business_draft(
                connection_id="conn1", owner_chat_id="100",
                customer_chat_id="200", customer_msg_id="m1",
                customer_text="old owner message", draft_text="old owner reply",
            )
            second_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="84",
                owner_chat_id="101", can_reply=True, is_enabled=True,
            )
            mgr, sender = _make_manager(first_db)
            answers: List[str] = []

            async def _answer(**kw): answers.append(kw.get("text", ""))
            async def _edit(**kw): pass

            assert await mgr.handle_callback(
                data=f"bd:send:{draft_id}", caller_user_id="84",
                answer=_answer, edit_message_text=_edit,
            ) is True

            assert not any(c.get("chat_id") == 200 for c in sender.calls)
            assert first_db.get_telegram_business_draft(draft_id)["status"] == "superseded"
            assert any("resolved" in text.lower() for text in answers)
        finally:
            second_db.close()
            first_db.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("falsy_caller", [None, ""])
    async def test_callback_rejects_falsy_caller(self, db, falsy_caller):
        """Regression: a missing/falsy caller_user_id must be rejected.

        The owner check was previously ``caller_user_id and ... != owner``,
        so a falsy caller_user_id skipped the check entirely (fail-open).
        """
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        sender.calls.clear()
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        did = draft["draft_id"]
        answered: List[Dict[str, Any]] = []

        async def _answer(**kw): answered.append(kw)
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data=f"bd:send:{did}", caller_user_id=falsy_caller,
            answer=_answer, edit_message_text=_edit,
        )
        assert answered and "Only the connected account owner" in answered[0]["text"]
        assert db.get_telegram_business_draft(did)["status"] == "pending"
        assert not any(c.get("chat_id") == 200 for c in sender.calls)

    @pytest.mark.asyncio
    async def test_send_blocked_when_can_reply_false(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection(can_reply=False))
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        did = draft["draft_id"]
        answered: List[Dict[str, Any]] = []

        async def _answer(**kw): answered.append(kw)
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data=f"bd:send:{did}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )
        # Send-on-your-behalf is OFF → reject with explanation, no customer send.
        assert any("send-on-your-behalf is off" in (a.get("text") or "").lower()
                   for a in answered)
        assert not any(c.get("chat_id") == 200 for c in sender.calls)
        assert db.get_telegram_business_draft(did)["status"] == "pending"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("choice", ["send", "edit", "discard"])
    async def test_expired_button_expires_and_no_ops(
        self, db, monkeypatch, choice,
    ):
        from telegram_business_plugin import state as state_mod

        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        sender.calls.clear()
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        expires_at = draft["expires_at"]
        monkeypatch.setattr(state_mod.time, "time", lambda: expires_at + 1.0)
        answered: List[Dict[str, Any]] = []

        async def _answer(**kw): answered.append(kw)
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data=f"bd:{choice}:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )

        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "expired"
        assert answered and "expired" in answered[0]["text"].lower()
        assert not any(c.get("chat_id") == 200 for c in sender.calls)

    @pytest.mark.asyncio
    async def test_concurrent_send_callbacks_deliver_at_most_once(self, db):
        sender = _BlockingFirstCustomerSender()
        mgr, _ = _make_manager(db, send_recorder=sender)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        sender.calls.clear()
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        first = asyncio.create_task(mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        ))
        await sender.first_customer_started.wait()

        # The duplicate reaches the callback while the first network call is
        # still blocked. It must return without entering send_message.
        await mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        assert sender.customer_calls == 1

        sender.release_first_customer.set()
        await first
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "sent"

    @pytest.mark.asyncio
    async def test_send_claim_blocks_concurrent_discard(self, db):
        sender = _BlockingFirstCustomerSender()
        mgr, _ = _make_manager(db, send_recorder=sender)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        answers: List[str] = []

        async def _answer(**kw): answers.append(kw.get("text", ""))
        async def _edit(**kw): pass

        sending = asyncio.create_task(mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        ))
        await sender.first_customer_started.wait()

        assert await mgr.handle_callback(
            data=f"bd:discard:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        ) is True
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "sending"
        assert sender.customer_calls == 1

        sender.release_first_customer.set()
        assert await sending is True
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "sent"
        assert sender.customer_calls == 1
        assert any("processed" in text.lower() for text in answers)

    @pytest.mark.asyncio
    async def test_send_failure_releases_claim_for_later_retry(self, db):
        sender = _FailFirstCustomerSender()
        mgr, _ = _make_manager(db, send_recorder=sender)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        answers: List[str] = []

        async def _answer(**kw): answers.append(kw.get("text", ""))
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "pending"

        await mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )
        assert sender.customer_calls == 2
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "sent"
        assert any("failed" in text.lower() for text in answers)

    @pytest.mark.asyncio
    async def test_failed_old_send_is_superseded_by_newer_same_chat_draft(self, db):
        sender = _BlockingThenPreDeliveryFailureSender()
        mgr, _ = _make_manager(db, send_recorder=sender)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message(
            customer_chat_id=200, text="first customer message", msg_id=1,
        ))
        draft_a = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        answers: List[str] = []

        async def _answer(**kw): answers.append(kw.get("text", ""))
        async def _edit(**kw): pass

        send_a = asyncio.create_task(mgr.handle_callback(
            data=f"bd:send:{draft_a['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        ))
        await sender.first_customer_started.wait()

        # A separate SQLite writer can create B while the manager keeps its
        # per-connection network critical section. Creation cannot supersede A
        # while A is claimed as sending.
        draft_b_id = db.create_telegram_business_draft(
            connection_id="conn1", owner_chat_id="100",
            customer_chat_id="200", customer_msg_id="2",
            customer_text="newer customer message", draft_text="newer reply",
        )
        draft_b = db.get_telegram_business_draft(draft_b_id)
        assert draft_b["draft_id"] > draft_a["draft_id"]

        sender.release_first_customer.set()
        assert await send_a is True

        assert db.get_telegram_business_draft(draft_a["draft_id"])["status"] == "superseded"
        live = db.get_pending_telegram_business_drafts_for_owner("100")
        assert [row["draft_id"] for row in live] == [draft_b["draft_id"]]
        assert any("superseded" in text.lower() for text in answers)

    @pytest.mark.asyncio
    async def test_post_claim_validation_release_cannot_resurrect_old_draft(
        self, tmp_path, monkeypatch,
    ):
        db_path = tmp_path / "post_claim_release.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            first_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            old = first_db.create_telegram_business_draft(
                connection_id="conn1", owner_chat_id="100",
                customer_chat_id="200", customer_msg_id="m1",
                customer_text="old", draft_text="old reply",
            )
            mgr, sender = _make_manager(first_db)
            answers: List[str] = []
            original_transition = first_db.transition_telegram_business_draft
            injected = False

            def _transition(*args, **kwargs):
                nonlocal injected
                result = original_transition(*args, **kwargs)
                if (
                    not injected
                    and kwargs.get("from_status") == "pending"
                    and kwargs.get("to_status") == "sending"
                    and result is not None
                ):
                    injected = True
                    second_db.create_telegram_business_draft(
                        connection_id="conn1", owner_chat_id="100",
                        customer_chat_id="200", customer_msg_id="m2",
                        customer_text="new", draft_text="new reply",
                    )
                    second_db.upsert_telegram_business_connection(
                        connection_id="conn1", owner_user_id="42",
                        owner_chat_id="100", can_reply=False, is_enabled=True,
                    )
                return result

            monkeypatch.setattr(
                first_db, "transition_telegram_business_draft", _transition,
            )

            async def _answer(**kw): answers.append(kw.get("text", ""))
            async def _edit(**kw): pass

            assert await mgr.handle_callback(
                data=f"bd:send:{old}", caller_user_id="42",
                answer=_answer, edit_message_text=_edit,
            ) is True

            assert sender.calls == []
            assert first_db.get_telegram_business_draft(old)["status"] == "superseded"
            assert any("superseded" in text.lower() for text in answers)

            second_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            assert await mgr.handle_callback(
                data=f"bd:send:{old}", caller_user_id="42",
                answer=_answer, edit_message_text=_edit,
            ) is True
            assert sender.calls == []
        finally:
            second_db.close()
            first_db.close()

    @pytest.mark.asyncio
    async def test_owner_change_between_claim_and_recheck_is_terminal(
        self, tmp_path, monkeypatch,
    ):
        db_path = tmp_path / "owner_change_during_callback.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            first_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            draft_id = first_db.create_telegram_business_draft(
                connection_id="conn1", owner_chat_id="100",
                customer_chat_id="200", customer_msg_id="m1",
                customer_text="old owner message", draft_text="old owner reply",
            )
            mgr, sender = _make_manager(first_db)
            original_transition = first_db.transition_telegram_business_draft
            injected = False

            def _transition(*args, **kwargs):
                nonlocal injected
                result = original_transition(*args, **kwargs)
                if (
                    not injected
                    and kwargs.get("from_status") == "pending"
                    and kwargs.get("to_status") == "sending"
                    and result is not None
                ):
                    injected = True
                    second_db.upsert_telegram_business_connection(
                        connection_id="conn1", owner_user_id="84",
                        owner_chat_id="100", can_reply=True, is_enabled=True,
                    )
                return result

            monkeypatch.setattr(
                first_db, "transition_telegram_business_draft", _transition,
            )
            answers: List[str] = []

            async def _answer(**kw): answers.append(kw.get("text", ""))
            async def _edit(**kw): pass

            assert await mgr.handle_callback(
                data=f"bd:send:{draft_id}", caller_user_id="42",
                answer=_answer, edit_message_text=_edit,
            ) is True

            assert not any(c.get("chat_id") == 200 for c in sender.calls)
            assert first_db.get_telegram_business_draft(draft_id)["status"] == "superseded"
            assert any("owner" in text.lower() for text in answers)
        finally:
            second_db.close()
            first_db.close()

    @pytest.mark.asyncio
    async def test_ambiguous_send_failure_is_not_retryable(self, db):
        sender = _AmbiguousCustomerFailureSender()
        mgr, _ = _make_manager(db, send_recorder=sender)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        answers: List[str] = []

        async def _answer(**kw): answers.append(kw.get("text", ""))
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )
        await mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )

        assert sender.customer_calls == 1
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "failed"
        assert any("outcome is unknown" in text.lower() for text in answers)

    @pytest.mark.asyncio
    async def test_send_blocked_when_connection_inactive(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        await mgr.handle_connection_update(
            _fake_business_connection(is_enabled=False)
        )
        sender.calls.clear()
        answered: List[Dict[str, Any]] = []

        async def _answer(**kw): answered.append(kw)
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )

        assert answered and "inactive" in answered[0]["text"].lower()
        assert not any(c.get("chat_id") == 200 for c in sender.calls)
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "pending"

    @pytest.mark.asyncio
    async def test_non_bd_callback_returns_false(self, db):
        mgr, _ = _make_manager(db)

        async def _noop(**kw):
            pass

        dispatched = await mgr.handle_callback(
            data="ea:once:1", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        assert dispatched is False


class TestEditCapture:
    @pytest.mark.asyncio
    async def test_edit_then_text_sends_override(self, db):
        mgr, sender = _make_manager(db, draft_text="original draft")
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        did = draft["draft_id"]
        # Tap Edit
        async def _answer(**kw): pass
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{did}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )
        sender.calls.clear()
        # Owner sends override text
        consumed = await mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="my custom reply",
        )
        assert consumed is True
        # Customer received the override
        customer_sends = [c for c in sender.calls if c.get("chat_id") == 200]
        assert customer_sends
        assert customer_sends[0]["text"] == "my custom reply"
        assert customer_sends[0]["business_connection_id"] == "conn1"
        # Draft now resolved as edited
        row = db.get_telegram_business_draft(did)
        assert row["status"] == "edited"
        assert row["final_sent_text"] == "my custom reply"

    @pytest.mark.asyncio
    async def test_edit_capture_idle_returns_false(self, db):
        mgr, _ = _make_manager(db)
        consumed = await mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="hello",
        )
        assert consumed is False

    @pytest.mark.asyncio
    async def test_edit_button_claim_blocks_stale_send(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        sender.calls.clear()
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        answers: List[str] = []

        async def _answer(**kw): answers.append(kw.get("text", ""))
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "awaiting_edit"

        await mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )
        assert not any(c.get("chat_id") == 200 for c in sender.calls)
        assert any("resolved" in text.lower() for text in answers)

    @pytest.mark.asyncio
    async def test_edit_button_blocked_when_connection_inactive(self, db):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]
        await mgr.handle_connection_update(
            _fake_business_connection(is_enabled=False)
        )
        answered: List[str] = []

        async def _answer(**kw): answered.append(kw.get("text", ""))
        async def _edit(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft['draft_id']}", caller_user_id="42",
            answer=_answer, edit_message_text=_edit,
        )

        assert any("inactive" in text.lower() for text in answered)
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "pending"
        assert "100" not in mgr._edit_capture

    @pytest.mark.asyncio
    async def test_edit_override_expires_at_delivery_time(self, db, monkeypatch):
        from telegram_business_plugin import state as state_mod

        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        sender.calls.clear()
        monkeypatch.setattr(
            state_mod.time, "time", lambda: draft["expires_at"] + 1.0,
        )

        assert await mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="too late",
        ) is True
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "expired"
        assert not any(c.get("chat_id") == 200 for c in sender.calls)
        assert any("expired" in c.get("text", "").lower() for c in sender.calls)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("connection_state", ["missing", "inactive", "no_reply"])
    async def test_edit_override_fails_closed_for_connection(
        self, db, connection_state,
    ):
        mgr, sender = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        if connection_state == "missing":
            with db._lock:
                db._conn.execute(
                    "DELETE FROM business_connections WHERE connection_id = ?",
                    ("conn1",),
                )
                db._conn.commit()
        else:
            await mgr.handle_connection_update(_fake_business_connection(
                is_enabled=connection_state != "inactive",
                can_reply=connection_state != "no_reply",
            ))
        sender.calls.clear()

        assert await mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="must not send",
        ) is True
        assert not any(c.get("chat_id") == 200 for c in sender.calls)
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "awaiting_edit"
        assert any(
            token in c.get("text", "").lower()
            for c in sender.calls
            for token in ("connection", "inactive", "send-on-your-behalf")
        )

    @pytest.mark.asyncio
    async def test_concurrent_edit_overrides_deliver_at_most_once(self, db):
        sender = _BlockingFirstCustomerSender()
        mgr1, _ = _make_manager(db, send_recorder=sender)
        mgr2, _ = _make_manager(db, send_recorder=sender)
        await mgr1.handle_connection_update(_fake_business_connection())
        await mgr1.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        await mgr1.handle_callback(
            data=f"bd:edit:{draft['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        # Simulate two adapter workers that both retained the edit capture.
        mgr2._edit_capture["100"] = draft["draft_id"]
        sender.calls.clear()
        first = asyncio.create_task(mgr1.maybe_handle_edit_capture(
            owner_chat_id="100", text="first override",
        ))
        await sender.first_customer_started.wait()

        assert await mgr2.maybe_handle_edit_capture(
            owner_chat_id="100", text="duplicate override",
        ) is True
        assert sender.customer_calls == 1

        sender.release_first_customer.set()
        await first
        row = db.get_telegram_business_draft(draft["draft_id"])
        assert row["status"] == "edited"
        assert row["final_sent_text"] == "first override"

    @pytest.mark.asyncio
    async def test_edit_send_failure_restores_capture_for_retry(self, db):
        sender = _FailFirstCustomerSender()
        mgr, _ = _make_manager(db, send_recorder=sender)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        assert await mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="first attempt",
        ) is True
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "awaiting_edit"
        assert mgr._edit_capture["100"] == draft["draft_id"]

        assert await mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="retry attempt",
        ) is True
        row = db.get_telegram_business_draft(draft["draft_id"])
        assert sender.customer_calls == 2
        assert row["status"] == "edited"
        assert row["final_sent_text"] == "retry attempt"

    @pytest.mark.asyncio
    async def test_failed_old_edit_cannot_resurrect_after_new_capture_consumed(
        self, db,
    ):
        sender = _BlockingThenPreDeliveryFailureSender()
        mgr, _ = _make_manager(db, send_recorder=sender)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message(
            customer_chat_id=200, text="customer A", msg_id=1,
        ))
        draft_a = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft_a['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        sender.calls.clear()
        send_a = asyncio.create_task(mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="override for A",
        ))
        await sender.first_customer_started.wait()

        # Advance to a different customer's edit flow while A is suspended,
        # then consume B's capture. The generation must remain advanced even
        # though the capture map is empty again.
        draft_b_id = db.create_telegram_business_draft(
            connection_id="conn1", owner_chat_id="100",
            customer_chat_id="300", customer_msg_id="2",
            customer_text="customer B", draft_text="draft B",
        )
        draft_b = db.get_telegram_business_draft(draft_b_id)
        await mgr.handle_callback(
            data=f"bd:edit:{draft_b['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        send_b = asyncio.create_task(mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="override for B",
        ))

        sender.release_first_customer.set()
        assert await send_a is True
        assert await send_b is True

        assert db.get_telegram_business_draft(draft_a["draft_id"])["status"] == "superseded"
        assert db.get_telegram_business_draft(draft_b["draft_id"])["status"] == "edited"
        assert "100" not in mgr._edit_capture
        customer_calls_before = sender.customer_calls
        assert await mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="unrelated later owner DM",
        ) is False
        assert sender.customer_calls == customer_calls_before

    @pytest.mark.asyncio
    async def test_failed_old_edit_is_superseded_by_newer_same_chat_draft(self, db):
        sender = _BlockingThenPreDeliveryFailureSender()
        mgr, _ = _make_manager(db, send_recorder=sender)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message(
            customer_chat_id=200, text="first customer message", msg_id=1,
        ))
        draft_a = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft_a['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        sender.calls.clear()
        send_a = asyncio.create_task(mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="override for old message",
        ))
        await sender.first_customer_started.wait()

        # B exists but has not entered Edit mode, so the generation is still
        # A's. The durable retry-release decision must independently reject A.
        draft_b_id = db.create_telegram_business_draft(
            connection_id="conn1", owner_chat_id="100",
            customer_chat_id="200", customer_msg_id="2",
            customer_text="newer customer message", draft_text="newer reply",
        )
        draft_b = db.get_telegram_business_draft(draft_b_id)
        assert draft_b["draft_id"] > draft_a["draft_id"]

        sender.release_first_customer.set()
        assert await send_a is True

        assert db.get_telegram_business_draft(draft_a["draft_id"])["status"] == "superseded"
        assert db.get_telegram_business_draft(draft_b["draft_id"])["status"] == "pending"
        assert "100" not in mgr._edit_capture
        assert any(
            call.get("chat_id") == 100
            and "superseded" in call.get("text", "").lower()
            for call in sender.calls
        )

    @pytest.mark.asyncio
    async def test_post_claim_edit_validation_cannot_restore_old_capture(
        self, tmp_path, monkeypatch,
    ):
        db_path = tmp_path / "post_claim_edit_release.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            first_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            old = first_db.create_telegram_business_draft(
                connection_id="conn1", owner_chat_id="100",
                customer_chat_id="200", customer_msg_id="m1",
                customer_text="old", draft_text="old reply",
            )
            mgr, sender = _make_manager(first_db)

            async def _noop(**kw): pass

            assert await mgr.handle_callback(
                data=f"bd:edit:{old}", caller_user_id="42",
                answer=_noop, edit_message_text=_noop,
            ) is True
            sender.calls.clear()
            original_transition = first_db.transition_telegram_business_draft
            injected = False

            def _transition(*args, **kwargs):
                nonlocal injected
                result = original_transition(*args, **kwargs)
                if (
                    not injected
                    and kwargs.get("from_status") == "awaiting_edit"
                    and kwargs.get("to_status") == "sending"
                    and result is not None
                ):
                    injected = True
                    second_db.create_telegram_business_draft(
                        connection_id="conn1", owner_chat_id="100",
                        customer_chat_id="200", customer_msg_id="m2",
                        customer_text="new", draft_text="new reply",
                    )
                    second_db.upsert_telegram_business_connection(
                        connection_id="conn1", owner_user_id="42",
                        owner_chat_id="100", can_reply=False, is_enabled=True,
                    )
                return result

            monkeypatch.setattr(
                first_db, "transition_telegram_business_draft", _transition,
            )

            assert await mgr.maybe_handle_edit_capture(
                owner_chat_id="100", text="old override",
            ) is True

            assert not any(c.get("chat_id") == 200 for c in sender.calls)
            assert first_db.get_telegram_business_draft(old)["status"] == "superseded"
            assert "100" not in mgr._edit_capture
            assert any(
                c.get("chat_id") == 100
                and "superseded" in c.get("text", "").lower()
                for c in sender.calls
            )

            second_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            customer_calls = len([
                c for c in sender.calls if c.get("chat_id") == 200
            ])
            assert await mgr.maybe_handle_edit_capture(
                owner_chat_id="100", text="later unrelated owner DM",
            ) is False
            assert len([
                c for c in sender.calls if c.get("chat_id") == 200
            ]) == customer_calls
        finally:
            second_db.close()
            first_db.close()

    @pytest.mark.asyncio
    async def test_edit_owner_change_between_claim_and_recheck_is_terminal(
        self, tmp_path, monkeypatch,
    ):
        db_path = tmp_path / "edit_owner_change_during_delivery.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            first_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="42",
                owner_chat_id="100", can_reply=True, is_enabled=True,
            )
            draft_id = first_db.create_telegram_business_draft(
                connection_id="conn1", owner_chat_id="100",
                customer_chat_id="200", customer_msg_id="m1",
                customer_text="old owner message", draft_text="old owner reply",
            )
            mgr, sender = _make_manager(first_db)

            async def _noop(**kw): pass

            assert await mgr.handle_callback(
                data=f"bd:edit:{draft_id}", caller_user_id="42",
                answer=_noop, edit_message_text=_noop,
            ) is True
            sender.calls.clear()
            original_transition = first_db.transition_telegram_business_draft
            injected = False

            def _transition(*args, **kwargs):
                nonlocal injected
                result = original_transition(*args, **kwargs)
                if (
                    not injected
                    and kwargs.get("from_status") == "awaiting_edit"
                    and kwargs.get("to_status") == "sending"
                    and result is not None
                ):
                    injected = True
                    second_db.upsert_telegram_business_connection(
                        connection_id="conn1", owner_user_id="84",
                        owner_chat_id="100", can_reply=True, is_enabled=True,
                    )
                return result

            monkeypatch.setattr(
                first_db, "transition_telegram_business_draft", _transition,
            )

            assert await mgr.maybe_handle_edit_capture(
                owner_chat_id="100", text="old owner override",
            ) is True

            assert not any(c.get("chat_id") == 200 for c in sender.calls)
            assert first_db.get_telegram_business_draft(draft_id)["status"] == "superseded"
            assert "100" not in mgr._edit_capture
            assert any(
                c.get("chat_id") == 100 and "owner" in c.get("text", "").lower()
                for c in sender.calls
            )
        finally:
            second_db.close()
            first_db.close()

    @pytest.mark.asyncio
    async def test_ambiguous_edit_send_failure_is_not_retryable(self, db):
        sender = _AmbiguousCustomerFailureSender()
        mgr, _ = _make_manager(db, send_recorder=sender)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        await mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="possibly delivered",
        )
        assert await mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="must not retry",
        ) is False

        assert sender.customer_calls == 1
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "failed"


class TestBizCommand:
    @pytest.mark.asyncio
    async def test_status_with_no_connections(self, db):
        mgr, _ = _make_manager(db)
        reply = await mgr.handle_biz_command(
            owner_user_id="42", owner_chat_id="100", args=[],
        )
        assert "haven't connected" in reply.lower()

    @pytest.mark.asyncio
    async def test_status_with_connection(self, db):
        mgr, _ = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        reply = await mgr.handle_biz_command(
            owner_user_id="42", owner_chat_id="100", args=[],
        )
        assert "active" in reply
        assert "drafting ON" in reply

    @pytest.mark.asyncio
    async def test_pause_and_resume(self, db):
        mgr, _ = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        reply = await mgr.handle_biz_command(
            owner_user_id="42", owner_chat_id="100", args=["pause"],
        )
        assert "Paused" in reply
        assert db.get_telegram_business_connection("conn1")["auto_draft"] is False
        reply = await mgr.handle_biz_command(
            owner_user_id="42", owner_chat_id="100", args=["resume"],
        )
        assert "Resumed" in reply
        assert db.get_telegram_business_connection("conn1")["auto_draft"] is True

    @pytest.mark.asyncio
    async def test_per_chat_off_and_on(self, db):
        mgr, _ = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        reply = await mgr.handle_biz_command(
            owner_user_id="42", owner_chat_id="100", args=["off", "200"],
        )
        assert "200" in reply
        assert "200" in db.get_telegram_business_connection("conn1")["paused_chats"]
        reply = await mgr.handle_biz_command(
            owner_user_id="42", owner_chat_id="100", args=["on", "200"],
        )
        assert "200" in reply
        assert "200" not in db.get_telegram_business_connection("conn1")["paused_chats"]

    @pytest.mark.asyncio
    async def test_unknown_subcommand_returns_help(self, db):
        mgr, _ = _make_manager(db)
        reply = await mgr.handle_biz_command(
            owner_user_id="42", owner_chat_id="100", args=["unknown"],
        )
        assert "Usage" in reply


# =========================================================================
# Durable ownership revision + manager dispatch serialization regressions
# =========================================================================


class TestOwnershipRevisionAndMigration:
    def test_owner_revision_detects_a_to_b_to_a_and_snapshots_new_drafts(self, db):
        db.upsert_telegram_business_connection(
            connection_id="roundtrip", owner_user_id="42",
            owner_chat_id="100", can_reply=True, is_enabled=True,
        )
        first = db.get_telegram_business_connection("roundtrip")
        db.upsert_telegram_business_connection(
            connection_id="roundtrip", owner_user_id="84",
            owner_chat_id="101", can_reply=True, is_enabled=True,
        )
        second = db.get_telegram_business_connection("roundtrip")
        db.upsert_telegram_business_connection(
            connection_id="roundtrip", owner_user_id="42",
            owner_chat_id="100", can_reply=True, is_enabled=True,
        )
        third = db.get_telegram_business_connection("roundtrip")

        assert second["owner_revision"] == first["owner_revision"] + 1
        assert third["owner_revision"] == second["owner_revision"] + 1

        draft_id = db.create_telegram_business_draft(
            connection_id="roundtrip", owner_chat_id="100",
            customer_chat_id="200", customer_msg_id="m1",
            customer_text="private customer text", draft_text="reply",
        )
        draft = db.get_telegram_business_draft(draft_id)
        assert draft["owner_user_id"] == "42"
        assert draft["owner_revision"] == third["owner_revision"]

    def test_head_schema_migrates_fail_closed_without_losing_preferences(
        self, tmp_path,
    ):
        db_path = tmp_path / "legacy.db"
        now = time.time()
        legacy = sqlite3.connect(db_path)
        legacy.executescript(
            """
            CREATE TABLE business_connections (
                connection_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                owner_chat_id TEXT NOT NULL,
                can_reply INTEGER NOT NULL DEFAULT 0,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                auto_draft INTEGER NOT NULL DEFAULT 1,
                paused_chats TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE business_drafts (
                draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id TEXT NOT NULL,
                owner_chat_id TEXT NOT NULL,
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
            """
        )
        legacy.execute(
            "INSERT INTO business_connections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("conn1", "42", "100", 1, 1, 0, '["200"]', now, now),
        )
        for offset, (status, customer) in enumerate((
            ("pending", "legacy pending"),
            ("awaiting_edit", "legacy edit"),
            ("sending", "legacy sending"),
            ("sent", "legacy terminal"),
        )):
            legacy.execute(
                """
                INSERT INTO business_drafts (
                    connection_id, owner_chat_id, customer_chat_id,
                    customer_msg_id, customer_text, draft_text, status,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "conn1", "100", str(300 + offset), "m1", customer,
                    "reply", status, now, now, now + 3600,
                ),
            )
        legacy.commit()
        legacy.close()

        migrated = BusinessStateDB(db_path)
        try:
            conn = migrated.get_telegram_business_connection("conn1")
            assert conn["auto_draft"] is False
            assert conn["paused_chats"] == ["200"]
            assert conn["owner_revision"] >= 1

            with migrated._lock:
                rows = migrated._conn.execute(
                    "SELECT draft_id, customer_text, status FROM business_drafts "
                    "ORDER BY draft_id"
                ).fetchall()
                connection_columns = {
                    row["name"] for row in migrated._conn.execute(
                        "PRAGMA table_info(business_connections)"
                    ).fetchall()
                }
                draft_columns = {
                    row["name"] for row in migrated._conn.execute(
                        "PRAGMA table_info(business_drafts)"
                    ).fetchall()
                }
            statuses = {row["customer_text"]: row["status"] for row in rows}
            assert statuses["legacy pending"] == "superseded"
            assert statuses["legacy edit"] == "superseded"
            assert statuses["legacy sending"] == "failed"
            assert statuses["legacy terminal"] == "sent"
            legacy_pending_id = next(
                row["draft_id"]
                for row in rows
                if row["customer_text"] == "legacy pending"
            )
            assert migrated.transition_telegram_business_draft(
                legacy_pending_id, from_status="pending", to_status="sending",
            ) is None
            assert "owner_revision" in connection_columns
            assert {"owner_user_id", "owner_revision"} <= draft_columns

            fresh_id = migrated.create_telegram_business_draft(
                connection_id="conn1", owner_chat_id="100",
                customer_chat_id="999", customer_msg_id="m2",
                customer_text="fresh", draft_text="fresh reply",
            )
            fresh = migrated.get_telegram_business_draft(fresh_id)
            assert fresh["owner_user_id"] == "42"
            assert fresh["owner_revision"] == conn["owner_revision"]
        finally:
            migrated.close()

        reopened = BusinessStateDB(db_path)
        try:
            reopened_conn = reopened.get_telegram_business_connection("conn1")
            with reopened._lock:
                reopened_statuses = {
                    row["customer_text"]: row["status"]
                    for row in reopened._conn.execute(
                        "SELECT customer_text, status FROM business_drafts"
                    ).fetchall()
                }
            assert reopened_conn["auto_draft"] is False
            assert reopened_conn["paused_chats"] == ["200"]
            assert reopened_statuses == {
                **statuses,
                "fresh": "pending",
            }
        finally:
            reopened.close()

    def test_partial_ownership_migration_self_heals_and_is_idempotent(
        self, tmp_path,
    ):
        db_path = tmp_path / "partial-migration.db"
        now = time.time()
        partial = sqlite3.connect(db_path)
        partial.executescript(
            """
            CREATE TABLE business_connections (
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
            CREATE TABLE business_drafts (
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
            """
        )
        partial.execute(
            "INSERT INTO business_connections VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("conn1", "42", "100", 7, 1, 1, 0, '["200", "300"]', now, now),
        )
        sentinel_statuses = (
            "pending", "awaiting_edit", "sending", "sent", "edited",
            "discarded", "failed", "superseded", "expired",
        )
        for offset, status in enumerate(sentinel_statuses):
            partial.execute(
                """
                INSERT INTO business_drafts (
                    connection_id, owner_user_id, owner_chat_id, owner_revision,
                    customer_chat_id, customer_msg_id, customer_text, draft_text,
                    status, owner_message_id, final_sent_text,
                    created_at, updated_at, expires_at
                ) VALUES (?, '', ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "conn1", "100", str(300 + offset), f"m{offset}",
                    f"sentinel {status}", f"reply {status}", status,
                    f"owner-message-{offset}", f"final {status}",
                    now - 10, now, now + 3600,
                ),
            )
        for offset, status in enumerate(("pending", "awaiting_edit", "sending")):
            partial.execute(
                """
                INSERT INTO business_drafts (
                    connection_id, owner_user_id, owner_chat_id, owner_revision,
                    customer_chat_id, customer_msg_id, customer_text, draft_text,
                    status, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "conn1", "42", "100", 7, str(400 + offset),
                    f"trusted-m{offset}", f"trusted {status}", "trusted reply",
                    status, now - 10, now, now + 3600,
                ),
            )
        for offset, (owner_user_id, owner_revision, status) in enumerate((
            ("", 7, "pending"),
            ("42", 0, "awaiting_edit"),
        )):
            partial.execute(
                """
                INSERT INTO business_drafts (
                    connection_id, owner_user_id, owner_chat_id, owner_revision,
                    customer_chat_id, customer_text, draft_text, status,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "conn1", owner_user_id, "100", owner_revision,
                    str(500 + offset), f"one sentinel {status}", "reply", status,
                    now - 10, now, now + 3600,
                ),
            )
        partial.commit()
        partial.close()

        first = BusinessStateDB(db_path)
        try:
            connection = first.get_telegram_business_connection("conn1")
            with first._lock:
                columns = {
                    row["name"] for row in first._conn.execute(
                        "PRAGMA table_info(business_drafts)"
                    ).fetchall()
                }
                connection_columns = {
                    row["name"] for row in first._conn.execute(
                        "PRAGMA table_info(business_connections)"
                    ).fetchall()
                }
                first_rows = [
                    dict(row) for row in first._conn.execute(
                        "SELECT * FROM business_drafts ORDER BY draft_id"
                    ).fetchall()
                ]
            statuses = {
                row["customer_text"]: row["status"] for row in first_rows
            }
            assert connection["auto_draft"] is False
            assert connection["paused_chats"] == ["200", "300"]
            assert {"owner_user_id", "owner_revision"} <= columns
            assert "owner_revision" in connection_columns
            assert statuses["sentinel pending"] == "superseded"
            assert statuses["sentinel awaiting_edit"] == "superseded"
            assert statuses["sentinel sending"] == "failed"
            assert statuses["one sentinel pending"] == "superseded"
            assert statuses["one sentinel awaiting_edit"] == "superseded"
            for status in (
                "sent", "edited", "discarded", "failed", "superseded", "expired",
            ):
                row = next(
                    row for row in first_rows
                    if row["customer_text"] == f"sentinel {status}"
                )
                assert row["status"] == status
                assert row["updated_at"] == now
                assert row["owner_message_id"].startswith("owner-message-")
                assert row["final_sent_text"] == f"final {status}"
            for status in ("pending", "awaiting_edit", "sending"):
                assert statuses[f"trusted {status}"] == status
        finally:
            first.close()

        second = BusinessStateDB(db_path)
        try:
            second_connection = second.get_telegram_business_connection("conn1")
            with second._lock:
                second_rows = [
                    dict(row) for row in second._conn.execute(
                        "SELECT * FROM business_drafts ORDER BY draft_id"
                    ).fetchall()
                ]
            assert second_connection["auto_draft"] is False
            assert second_connection["paused_chats"] == ["200", "300"]
            assert second_rows == first_rows
        finally:
            second.close()

    def test_failed_migration_rolls_back_and_next_open_recovers(self, tmp_path):
        db_path = tmp_path / "migration-rollback.db"
        now = time.time()
        legacy = sqlite3.connect(db_path)
        legacy.executescript(
            """
            CREATE TABLE business_connections (
                connection_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                owner_chat_id TEXT NOT NULL,
                can_reply INTEGER NOT NULL DEFAULT 0,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                auto_draft INTEGER NOT NULL DEFAULT 1,
                paused_chats TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE business_drafts (
                draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id TEXT NOT NULL,
                owner_chat_id TEXT NOT NULL,
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
            CREATE TRIGGER abort_legacy_closure
            BEFORE UPDATE OF status ON business_drafts
            WHEN OLD.status IN ('pending', 'awaiting_edit', 'sending')
            BEGIN
                SELECT RAISE(ABORT, 'simulated migration interruption');
            END;
            """
        )
        legacy.execute(
            "INSERT INTO business_connections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("conn1", "42", "100", 1, 1, 0, '["200"]', now, now),
        )
        legacy.execute(
            """
            INSERT INTO business_drafts (
                connection_id, owner_chat_id, customer_chat_id,
                customer_msg_id, customer_text, draft_text, status,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            ("conn1", "100", "300", "m1", "legacy pending", "reply",
             now, now, now + 3600),
        )
        legacy.commit()
        legacy.close()

        with pytest.raises(sqlite3.IntegrityError, match="migration interruption"):
            BusinessStateDB(db_path)

        inspection = sqlite3.connect(db_path)
        try:
            connection_columns = {
                row[1] for row in inspection.execute(
                    "PRAGMA table_info(business_connections)"
                ).fetchall()
            }
            draft_columns = {
                row[1] for row in inspection.execute(
                    "PRAGMA table_info(business_drafts)"
                ).fetchall()
            }
            assert "owner_revision" not in connection_columns
            assert "owner_user_id" not in draft_columns
            assert "owner_revision" not in draft_columns
            inspection.execute("DROP TRIGGER abort_legacy_closure")
            inspection.commit()
        finally:
            inspection.close()

        recovered = BusinessStateDB(db_path)
        try:
            draft = recovered._conn.execute(
                "SELECT * FROM business_drafts WHERE customer_text = ?",
                ("legacy pending",),
            ).fetchone()
            connection = recovered.get_telegram_business_connection("conn1")
            assert draft["status"] == "superseded"
            assert draft["owner_user_id"] == ""
            assert draft["owner_revision"] == 0
            assert connection["auto_draft"] is False
            assert connection["paused_chats"] == ["200"]
        finally:
            recovered.close()


class TestStaleDraftOwnershipSnapshots:
    @pytest.mark.asyncio
    async def test_owner_change_while_debounce_pending_drops_customer_text(self, db):
        mgr, sender = _make_manager(db, debounce=3600)
        await mgr.handle_connection_update(_fake_business_connection())
        sender.calls.clear()
        await mgr.handle_business_message(
            _fake_business_message(text="debounce secret")
        )
        key = "conn1:200"
        debounce_task = mgr._debounce_tasks[key]

        await mgr.handle_connection_update(_fake_business_connection(
            owner_id=84, owner_chat=101,
        ))
        await mgr._run_draft(key)
        debounce_task.cancel()
        await asyncio.gather(debounce_task, return_exceptions=True)

        assert db.get_pending_telegram_business_drafts_for_owner("100") == []
        assert db.get_pending_telegram_business_drafts_for_owner("101") == []
        assert not any(
            "debounce secret" in call.get("text", "") for call in sender.calls
        )

    @pytest.mark.asyncio
    async def test_a_to_b_to_a_while_generator_blocked_drops_stale_result(self, db):
        from telegram_business_plugin.manager import BusinessModeManager

        started = asyncio.Event()
        release = asyncio.Event()

        async def _draft(customer_text, customer_chat_id):
            started.set()
            await release.wait()
            return "generated for stale owner"

        sender = _SentRecorder()
        mgr = BusinessModeManager(
            session_db=db, send_message=sender, draft_generator=_draft,
            debounce_seconds=0,
        )
        await mgr.handle_connection_update(_fake_business_connection())
        sender.calls.clear()
        drafting = asyncio.create_task(mgr.handle_business_message(
            _fake_business_message(text="blocked generation secret")
        ))
        await started.wait()
        await mgr.handle_connection_update(_fake_business_connection(
            owner_id=84, owner_chat=101,
        ))
        await mgr.handle_connection_update(_fake_business_connection())
        release.set()
        await drafting

        assert db.get_pending_telegram_business_drafts_for_owner("100") == []
        assert db.get_pending_telegram_business_drafts_for_owner("101") == []
        assert not any(
            "blocked generation secret" in call.get("text", "")
            for call in sender.calls
        )

    @pytest.mark.asyncio
    async def test_owner_change_while_failing_generator_blocked_is_silent(self, db):
        from telegram_business_plugin.manager import BusinessModeManager

        started = asyncio.Event()
        release = asyncio.Event()

        async def _draft(customer_text, customer_chat_id):
            started.set()
            await release.wait()
            raise RuntimeError("model failed after reassignment")

        sender = _SentRecorder()
        mgr = BusinessModeManager(
            session_db=db, send_message=sender, draft_generator=_draft,
            debounce_seconds=0,
        )
        await mgr.handle_connection_update(_fake_business_connection())
        sender.calls.clear()
        drafting = asyncio.create_task(mgr.handle_business_message(
            _fake_business_message(text="generator failure secret")
        ))
        await started.wait()
        await mgr.handle_connection_update(_fake_business_connection(
            owner_id=84, owner_chat=101,
        ))
        release.set()
        await drafting

        assert db.get_pending_telegram_business_drafts_for_owner("100") == []
        assert db.get_pending_telegram_business_drafts_for_owner("101") == []
        assert not any(
            "generator failure secret" in call.get("text", "")
            for call in sender.calls
        )


class TestConnectionDispatchSerialization:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "update",
        [
            _fake_business_connection(owner_id=84, owner_chat=101),
            _fake_business_connection(is_enabled=False),
            _fake_business_connection(can_reply=False),
        ],
        ids=["owner", "disabled", "no-reply"],
    )
    async def test_direct_send_serializes_connection_update_through_transition(
        self, db, update,
    ):
        sender = _UpdateInsideCustomerSender(db, update)
        mgr, _ = _make_manager(db, send_recorder=sender)
        sender.manager = mgr
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        sender.calls.clear()
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        sending = asyncio.create_task(mgr.handle_callback(
            data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        ))
        await sender.customer_started.wait()
        sender.release_customer.set()
        assert await sending is True
        await sender.update_task

        assert sender.update_done_while_sending is False
        assert sender.connection_while_sending["owner_user_id"] == "42"
        assert sender.connection_while_sending["owner_chat_id"] == "100"
        assert sender.connection_while_sending["is_enabled"] is True
        assert sender.connection_while_sending["can_reply"] is True
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "sent"
        final = db.get_telegram_business_connection("conn1")
        assert final["owner_user_id"] == str(update.user.id)
        assert final["owner_chat_id"] == str(update.user_chat_id)
        assert final["is_enabled"] is bool(update.is_enabled)
        assert final["can_reply"] is bool(update.rights.can_reply)

    @pytest.mark.asyncio
    async def test_edited_send_serializes_owner_update_through_transition(self, db):
        update = _fake_business_connection(owner_id=84, owner_chat=101)
        sender = _UpdateInsideCustomerSender(db, update)
        mgr, _ = _make_manager(db, send_recorder=sender)
        sender.manager = mgr
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        sender.calls.clear()
        sending = asyncio.create_task(mgr.maybe_handle_edit_capture(
            owner_chat_id="100", text="approved immutable edit",
        ))
        await sender.customer_started.wait()
        sender.release_customer.set()
        assert await sending is True
        await sender.update_task

        assert sender.update_done_while_sending is False
        assert sender.connection_while_sending["owner_user_id"] == "42"
        assert sender.connection_while_sending["owner_chat_id"] == "100"
        row = db.get_telegram_business_draft(draft["draft_id"])
        assert row["status"] == "edited"
        assert row["final_sent_text"] == "approved immutable edit"
        assert db.get_telegram_business_connection("conn1")["owner_user_id"] == "84"


class TestHistoricalOwnerChangeRetryRelease:
    @pytest.mark.asyncio
    async def test_direct_pre_delivery_failure_after_a_to_b_to_a_is_terminal(
        self, tmp_path,
    ):
        db_path = tmp_path / "direct_roundtrip.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            sender = _BlockingThenPreDeliveryFailureSender()
            mgr, _ = _make_manager(first_db, send_recorder=sender)
            await mgr.handle_connection_update(_fake_business_connection())
            await mgr.handle_business_message(_fake_business_message())
            draft = first_db.get_pending_telegram_business_drafts_for_owner("100")[0]

            async def _noop(**kw): pass

            sending = asyncio.create_task(mgr.handle_callback(
                data=f"bd:send:{draft['draft_id']}", caller_user_id="42",
                answer=_noop, edit_message_text=_noop,
            ))
            await sender.first_customer_started.wait()
            second_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="84", owner_chat_id="101",
                can_reply=True, is_enabled=True,
            )
            second_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="42", owner_chat_id="100",
                can_reply=True, is_enabled=True,
            )
            sender.release_first_customer.set()
            assert await sending is True

            assert sender.customer_calls == 1
            assert first_db.get_telegram_business_draft(
                draft["draft_id"]
            )["status"] == "superseded"
        finally:
            second_db.close()
            first_db.close()

    @pytest.mark.asyncio
    async def test_edited_pre_delivery_failure_after_a_to_b_to_a_is_terminal(
        self, tmp_path,
    ):
        db_path = tmp_path / "edited_roundtrip.db"
        first_db = BusinessStateDB(db_path)
        second_db = BusinessStateDB(db_path)
        try:
            sender = _BlockingThenPreDeliveryFailureSender()
            mgr, _ = _make_manager(first_db, send_recorder=sender)
            await mgr.handle_connection_update(_fake_business_connection())
            await mgr.handle_business_message(_fake_business_message())
            draft = first_db.get_pending_telegram_business_drafts_for_owner("100")[0]

            async def _noop(**kw): pass

            await mgr.handle_callback(
                data=f"bd:edit:{draft['draft_id']}", caller_user_id="42",
                answer=_noop, edit_message_text=_noop,
            )
            sending = asyncio.create_task(mgr.maybe_handle_edit_capture(
                owner_chat_id="100", text="old capture",
            ))
            await sender.first_customer_started.wait()
            second_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="84", owner_chat_id="101",
                can_reply=True, is_enabled=True,
            )
            second_db.upsert_telegram_business_connection(
                connection_id="conn1", owner_user_id="42", owner_chat_id="100",
                can_reply=True, is_enabled=True,
            )
            sender.release_first_customer.set()
            assert await sending is True

            assert sender.customer_calls == 1
            assert first_db.get_telegram_business_draft(
                draft["draft_id"]
            )["status"] == "superseded"
            assert "100" not in mgr._edit_capture
        finally:
            second_db.close()
            first_db.close()


class TestAwaitingEditRecovery:
    @pytest.mark.asyncio
    async def test_manager_startup_invalidates_unrecoverable_awaiting_edit(self, db):
        mgr, _ = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message())
        draft = db.get_pending_telegram_business_drafts_for_owner("100")[0]

        async def _noop(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "awaiting_edit"

        restarted, _ = _make_manager(db)

        assert restarted._edit_capture == {}
        assert db.get_telegram_business_draft(draft["draft_id"])["status"] == "superseded"

    @pytest.mark.asyncio
    async def test_new_edit_capture_supersedes_previous_unconsumed_capture(self, db):
        mgr, _ = _make_manager(db)
        await mgr.handle_connection_update(_fake_business_connection())
        await mgr.handle_business_message(_fake_business_message(
            customer_chat_id=200, text="customer A", msg_id=1,
        ))
        await mgr.handle_business_message(_fake_business_message(
            customer_chat_id=300, text="customer B", msg_id=2,
        ))
        drafts = db.get_pending_telegram_business_drafts_for_owner("100")
        draft_a = next(d for d in drafts if d["customer_chat_id"] == "200")
        draft_b = next(d for d in drafts if d["customer_chat_id"] == "300")

        async def _noop(**kw): pass

        await mgr.handle_callback(
            data=f"bd:edit:{draft_a['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )
        await mgr.handle_callback(
            data=f"bd:edit:{draft_b['draft_id']}", caller_user_id="42",
            answer=_noop, edit_message_text=_noop,
        )

        assert db.get_telegram_business_draft(draft_a["draft_id"])["status"] == "superseded"
        assert db.get_telegram_business_draft(draft_b["draft_id"])["status"] == "awaiting_edit"
        assert mgr._edit_capture["100"] == draft_b["draft_id"]
