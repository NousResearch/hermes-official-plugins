"""The state.db poll thread starts on the first events_poll / events_wait, not at server launch.

One MCP server runs per agent session; each started a 200 ms poller whose open tick re-reads every indexed transcript,
and a busy gateway keeps the data_version gate open almost every tick (measured ~25k pread/s per idle bridge). Launch
still baselines (prime), so a message that arrives after launch is delivered once a client asks for events.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from mcp_serve_plugin import bridge as B


class _Ver:
    """Stand-in for _StateDbVersion: a new value on every read, so the poll gate is always open."""

    def __init__(self):
        self.n = 0

    def read(self):
        self.n += 1
        return self.n

    def close(self):
        pass


class _DB:
    def __init__(self, messages):
        self.messages = messages

    def get_messages(self, session_id):
        return list(self.messages)

    def close(self):
        pass


@pytest.fixture
def fake_state(monkeypatch):
    """One conversation 'k' backed by an in-memory message list; no real state.db."""
    msgs = [{"id": 1, "role": "user", "content": "before launch", "timestamp": 1.0}]
    monkeypatch.setattr(B, "_get_session_db", lambda: _DB(msgs))
    monkeypatch.setattr(B, "_load_sessions_index", lambda: {"k": {"session_id": "s1"}})
    return msgs


def _bridge(auto=True):
    """A bridge armed the way run_mcp_server arms it (auto=True), or a plain one."""
    b = B.EventBridge()
    b._db_version = _Ver()
    b._auto_start = auto
    return b


def test_a_plain_bridge_never_starts_a_thread_from_events_calls(fake_state):
    """Bridges built outside run_mcp_server (library users, unit tests) keep the old behaviour: no thread."""
    b = _bridge(auto=False)
    B._ToolHandlers(b).events_poll()
    b.ensure_started()
    assert b._thread is None and not b._running


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_prime_baselines_without_a_thread(fake_state):
    b = _bridge()
    b.prime()
    assert b._baselined and b._thread is None and not b._running


def test_message_after_launch_is_delivered_on_first_use(fake_state):
    """Launch baselines; a later message is not swallowed by a second baseline at first use."""
    b = _bridge()
    b.prime()
    fake_state.append({"id": 2, "role": "user", "content": "after launch", "timestamp": 2.0})
    try:
        b.ensure_started()
        assert _wait(lambda: b.poll_events()["events"])
        contents = [e["content"] for e in b.poll_events()["events"]]
        assert contents == ["after launch"]
    finally:
        b.stop()


def test_events_tools_start_the_poller_and_approval_tools_do_not(fake_state):
    b = _bridge()
    b.prime()
    h = B._ToolHandlers(b)
    try:
        h.permissions_list_open()
        assert b._thread is None
        h.events_poll()
        assert b._running and b._thread.is_alive()
    finally:
        b.stop()
    b2 = _bridge()
    b2.prime()
    try:
        B._ToolHandlers(b2).events_wait(timeout_ms=0)
        assert b2._running
    finally:
        b2.stop()


def test_ensure_started_starts_once_under_concurrency(monkeypatch, fake_state):
    """A slow baseline (unprimed bridge) is where an unlocked check-then-start would start several threads."""
    starts = []
    real = B.EventBridge._establish_baseline

    def slow_baseline(self):
        time.sleep(0.2)
        return real(self)

    monkeypatch.setattr(B.EventBridge, "_establish_baseline", slow_baseline)
    real_start = B.EventBridge.start
    monkeypatch.setattr(B.EventBridge, "start", lambda self: (starts.append(1), real_start(self))[1])
    b = _bridge()
    try:
        ts = [threading.Thread(target=b.ensure_started) for _ in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(starts) == 1
    finally:
        b.stop()


def test_stop_is_final(fake_state):
    b = _bridge()
    b.prime()
    b.stop()
    b.ensure_started()
    assert not b._running and b._thread is None


@pytest.mark.parametrize("eager", [False, True])
def test_run_mcp_server_primes_and_starts_only_when_eager(monkeypatch, fake_state, eager):
    seen = {}

    class _Server:
        async def run_stdio_async(self):
            b = seen["bridge"]
            seen["thread_at_run"] = b._thread is not None and b._thread.is_alive()
            seen["baselined_at_run"] = b._baselined

    def fake_create(event_bridge=None, include_tools=False, tools_only=False):
        event_bridge._db_version = _Ver()
        seen["bridge"] = event_bridge
        return _Server()

    monkeypatch.setattr(B, "_MCP_SERVER_AVAILABLE", True)
    monkeypatch.setattr(B, "create_mcp_server", fake_create)
    if eager:
        monkeypatch.setenv("HERMES_MCP_EAGER_EVENTS", "1")
    else:
        monkeypatch.delenv("HERMES_MCP_EAGER_EVENTS", raising=False)
    orig_init = B.EventBridge.__init__

    def init(self):
        orig_init(self)
        self._db_version = _Ver()

    monkeypatch.setattr(B.EventBridge, "__init__", init)
    B.run_mcp_server()
    assert seen["baselined_at_run"] is True
    assert seen["thread_at_run"] is eager
    assert seen["bridge"]._auto_start is (not eager)
    assert seen["bridge"]._stopped
