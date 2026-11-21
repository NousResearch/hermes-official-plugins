"""A kept process feeds the extra-usage policy on every step it serves, as a per-call process does (#398, #425).

The reserve stops a conversation once a plan window nears its limit, from the ``rate_limit_event`` native reports
with each answer. A kept process serves many steps, so it must report every one of them, not only its first.
"""
import contextvars

import pytest

import directsdk
from test_directsdk_session import CLAUDE, Upstream, client, converse, home, session_pids  # noqa: F401

pytestmark = pytest.mark.skipif(not CLAUDE, reason='needs the Claude Code CLI')
# Stands in for the serving profile a multiplexed gateway binds per context (hermes_scope).
PROFILE = contextvars.ContextVar('profile', default=None)


class Windowed(Upstream):
    """The session suite's upstream, with the plan window headers a subscription answer carries."""

    def end_headers(self):
        self.send_header('anthropic-ratelimit-unified-status', 'allowed')
        self.send_header('anthropic-ratelimit-unified-reset', '4102444800')
        self.send_header('anthropic-ratelimit-unified-5h-utilization', '0.42')
        self.send_header('anthropic-ratelimit-unified-representative-claim', 'five_hour')
        super().end_headers()


@pytest.fixture
def upstream():
    from http.server import ThreadingHTTPServer
    import threading
    server = ThreadingHTTPServer(('127.0.0.1', 0), Windowed)
    server.bodies, server.slow = [], 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize('session', [False, True])
def test_every_step_feeds_the_extra_usage_policy(home, upstream, monkeypatch, session):
    """Each step's extra-usage check must find the window fed since the step before. Native reports the window only
    when it changes and can write that report after the step's message_stop, where a kept native's step has ended."""
    fed, checks, pids, profiles = [], [], [], []
    PROFILE.set('work')
    observe, check = directsdk.observe_rate_limit, directsdk.check_extra_usage
    monkeypatch.setattr(directsdk, 'observe_rate_limit',
                        lambda env, summary: (fed.append(summary), profiles.append(PROFILE.get()), observe(env, summary)))
    monkeypatch.setattr(directsdk, 'check_extra_usage', lambda *a: (checks.append(len(fed)), check(*a)))
    c = client(home, upstream, monkeypatch, session=session, login='claude_ai')
    try:
        converse(c, on_step=lambda: pids.append(session_pids(c)))
    finally:
        c.close()
    assert len(pids) == len(checks) == 12
    # Kept on: one process served all twelve steps. Off: none was kept.
    assert len(set().union(*pids)) == (1 if session else 0)
    # A process's first report can land just after its first step has ended, so from the third step on.
    assert all(later > before for before, later in zip(checks[1:], checks[2:])), checks
    assert fed and all(s['window'] == 'five_hour' for s in fed), fed
    # Read in the serving profile's settings, on the reader thread too.
    assert set(profiles) == {'work'}, profiles
