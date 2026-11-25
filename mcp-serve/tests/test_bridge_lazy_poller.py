"""The state.db poller starts on the first event/approval call, not at server start (2026-11-25).

Incident: every Claude Code session runs ``hermes mcp-serve --tools`` and each one started an EventBridge poller at
launch. The poller wakes every 200 ms, and the ``data_version`` gate opens on any gateway commit, so each tick re-read
every indexed session's whole transcript (2,168 sessions, 77,649 messages on the M5). Measured: about 25,600
pread/pwrite per second per bridge, 13 bridges on the M5 (45 on the steambox), host load 200+, and gateway handshakes
past the client's 10 s open_timeout ("Gateway connection failed; no local fallback"). In 14 days the Claude seats made
no events_poll / events_wait / approval call at all, so the eager poller served nobody.
"""

from __future__ import annotations

import threading

import pytest

from mcp_serve_plugin import bridge as B


@pytest.fixture
def quiet_poll(monkeypatch):
    """Replace the DB poll loop with a loop that only records that it ran (no state.db)."""
    ran = threading.Event()

    def fake_loop(self):
        ran.set()
        while self._running:
            self._new_event.wait(0.05)

    monkeypatch.setattr(B.EventBridge, "_poll_loop", fake_loop)
    monkeypatch.setattr(B.EventBridge, "_establish_baseline", lambda self: None)
    return ran


def test_constructing_the_bridge_starts_no_poller(quiet_poll):
    b = B.EventBridge()
    assert b._thread is None and not b._running
    assert not quiet_poll.wait(0.2)


@pytest.mark.parametrize("call", [
    lambda b: b.poll_events(),
    lambda b: b.wait_for_event(timeout_ms=10),
    lambda b: b.list_pending_approvals(),
    lambda b: b.respond_to_approval("nope", "approve"),
])
def test_first_event_or_approval_call_starts_the_poller(quiet_poll, call):
    b = B.EventBridge()
    try:
        call(b)
        assert quiet_poll.wait(2.0), "the poller did not start on first use"
        assert b._running and b._thread is not None and b._thread.is_alive()
    finally:
        b.stop()


def test_ensure_started_is_idempotent_under_concurrency(quiet_poll, monkeypatch):
    starts = []
    real_start = B.EventBridge.start

    def counting_start(self):
        starts.append(1)
        return real_start(self)

    monkeypatch.setattr(B.EventBridge, "start", counting_start)
    b = B.EventBridge()
    try:
        ts = [threading.Thread(target=b.ensure_started) for _ in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(starts) == 1
    finally:
        b.stop()


def test_run_mcp_server_does_not_start_the_poller_eagerly(monkeypatch):
    """Eager start only when HERMES_MCP_EAGER_EVENTS=1 (the old behaviour, kept as an opt-in)."""
    monkeypatch.delenv("HERMES_MCP_EAGER_EVENTS", raising=False)
    assert B._eager_events() is False
    monkeypatch.setenv("HERMES_MCP_EAGER_EVENTS", "1")
    assert B._eager_events() is True
