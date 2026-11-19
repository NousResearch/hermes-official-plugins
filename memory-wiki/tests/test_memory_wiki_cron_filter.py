"""#379: cron/subagent sessions must not dominate subjects or Recent sessions."""

from __future__ import annotations

import pytest


@pytest.fixture
def cron_and_chat_sessions():
    def session(sid, source, title):
        return {
            "id": sid, "title": title, "source": source, "preview": "",
            "started_at": 100.0, "last_active": 100.0,
        }
    return [
        session("cron-1", "cron", "exit code 0"),
        session("cron-2", "cron", "chief pilot"),
        session("chat-1", "cli", "Project Alpha roadmap"),
        session("sub-1", "subagent", "delegate research"),
    ]


def test_subjects_exclude_cron_and_subagent(memory_wiki_core, cron_and_chat_sessions):
    db = _FakeDB(cron_and_chat_sessions)
    subjects = memory_wiki_core.build_memory_subjects(db, limit=10)
    slugs = {s["slug"] for s in subjects}
    assert "project-alpha-roadmap" in slugs
    assert "exit-code" not in slugs
    assert "chief-pilot" not in slugs


def test_recent_sessions_exclude_cron_and_subagent(memory_wiki_core, cron_and_chat_sessions):
    db = _FakeDB(cron_and_chat_sessions)
    overview = memory_wiki_core.build_memory_overview(db, subject_limit=10, day_limit=30)
    ids = {s["id"] for s in overview["recent_sessions"]}
    assert ids == {"chat-1"}


def test_daily_log_keeps_full_activity_record(memory_wiki_core, cron_and_chat_sessions):
    db = _FakeDB(cron_and_chat_sessions)
    logs = memory_wiki_core.build_daily_logs(db, limit_days=30)
    session_ids = {s["id"] for day in logs for s in day.get("sessions", [])}
    assert {"cron-1", "cron-2", "sub-1"} <= session_ids


def test_cron_burst_does_not_empty_recent_sessions(memory_wiki_core, tmp_path):
    """The 10 newest rows being cron runs must not push the last conversation out of the list."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("chat-1", "cli")
        db.append_message("chat-1", "user", "Plan the Project Alpha roadmap")
        for i in range(15):
            db.create_session(f"cron-{i}", "cron")
            db.append_message(f"cron-{i}", "user", "[IMPORTANT: You are running as a scheduled cron job] exit code")
        overview = memory_wiki_core.build_memory_overview(db, subject_limit=10, day_limit=1)
    finally:
        db.close()

    assert [s["id"] for s in overview["recent_sessions"]] == ["chat-1"]


class _FakeDB:
    def __init__(self, sessions):
        self.sessions = sessions

    def list_sessions_rich(self, *, limit, include_children, order_by_last_active, exclude_sources=None):
        excluded = set(exclude_sources or ())
        return [s for s in self.sessions if s["source"] not in excluded][:limit]

    def get_messages(self, session_id):
        return []
