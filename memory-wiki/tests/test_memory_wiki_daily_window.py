"""The daily log's session window: a busy profile still gets complete days.

``build_daily_logs`` reads a window of the most recently active sessions, sized from the number of
days asked for. Since the daily log keeps cron and subagent runs (#379), a profile with frequent
scheduled jobs fills that window before it covers the days asked for, and the oldest day it reaches
holds only the sessions that happened to fit.
"""

from __future__ import annotations

from datetime import datetime, timedelta

DAY0 = datetime(2026, 11, 10, 0, 30)  # local time: every session below stays on its own day


class _FakeDB:
    """``list_sessions_rich`` in the order Hermes returns it: most recently active first."""

    def __init__(self, sessions):
        self.sessions = sorted(sessions, key=lambda s: s["last_active"], reverse=True)
        self.limits = []

    def list_sessions_rich(self, *, limit, include_children, order_by_last_active, exclude_sources=None):
        self.limits.append(limit)
        excluded = set(exclude_sources or ())
        return [s for s in self.sessions if s["source"] not in excluded][:limit]

    def get_messages(self, session_id):
        return []


def _cron_sessions(days: int, per_day: int) -> list[dict]:
    sessions = []
    for day in range(days):
        for run in range(per_day):
            ts = (DAY0 + timedelta(days=day, minutes=15 * run)).timestamp()
            sessions.append({
                "id": f"cron-{day}-{run}", "title": "exit code 0", "source": "cron", "preview": "",
                "started_at": ts, "last_active": ts + 60,
            })
    return sessions


def _day(offset: int) -> str:
    return (DAY0 + timedelta(days=offset)).strftime("%Y-%m-%d")


def test_a_cron_heavy_profile_gets_every_day_it_asked_for_in_full(memory_wiki_core):
    # 40 scheduled runs a day: the 125-session window for 5 days covers barely 3 of them.
    db = _FakeDB(_cron_sessions(days=10, per_day=40))
    logs = memory_wiki_core.build_daily_logs(db, limit_days=5)

    assert [log["date"] for log in logs] == [_day(9), _day(8), _day(7), _day(6), _day(5)]
    assert [log["session_count"] for log in logs] == [40] * 5


def test_a_day_the_window_only_partly_reached_is_left_out(memory_wiki_core, monkeypatch):
    # With the window capped below what the days need, the result is shorter but every day in it is
    # whole: no day is reported with only the sessions that happened to fit.
    monkeypatch.setattr(memory_wiki_core, "_DAILY_SESSION_CAP", 150)
    db = _FakeDB(_cron_sessions(days=10, per_day=40))
    logs = memory_wiki_core.build_daily_logs(db, limit_days=5)

    assert [log["date"] for log in logs] == [_day(9), _day(8), _day(7)]
    assert [log["session_count"] for log in logs] == [40] * 3
    assert max(db.limits) == 150


def test_a_quiet_profile_is_read_in_one_query(memory_wiki_core):
    db = _FakeDB(_cron_sessions(days=3, per_day=2))
    logs = memory_wiki_core.build_daily_logs(db, limit_days=60)

    assert [log["session_count"] for log in logs] == [2, 2, 2]
    assert db.limits == [1500]
