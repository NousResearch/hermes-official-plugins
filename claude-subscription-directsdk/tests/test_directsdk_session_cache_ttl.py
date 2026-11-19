"""A delegated child's 5m cache tier (#416) holds on a kept process: the same bytes as per-call, none asking for 1h.

A claude.ai login makes Claude Code ask for the 1h tier on its main conversation; ``hermes_cache_ttl='5m'`` sets the
variable that turns it to 5m. A kept process is launched with that environment, so its requests must match per-call's.
"""
import functools

import pytest

from test_directsdk_session import CLAUDE, client, converse, home, session_pids, upstream  # noqa: F401

pytestmark = pytest.mark.skipif(not CLAUDE, reason='needs the Claude Code CLI')


def test_a_kept_child_sends_per_calls_bytes_with_no_1h_marker(home, upstream, monkeypatch):
    bodies = {}
    for session in (False, True):
        upstream.bodies = []
        c = client(home, upstream, monkeypatch, session=session, login='claude_ai')
        c.create = functools.partial(c.create, hermes_cache_ttl='5m')
        pids = []
        try:
            converse(c, on_step=lambda: pids.append(session_pids(c)))
        finally:
            c.close()
        assert len(set().union(*pids)) == (1 if session else 0)
        bodies[session] = list(upstream.bodies)
    assert len(bodies[True]) == 12 and bodies[True] == bodies[False]
    assert not [b for b in bodies[True] if b'"ttl":"1h"' in b]
    assert all(b'cache_control' in b for b in bodies[True])
