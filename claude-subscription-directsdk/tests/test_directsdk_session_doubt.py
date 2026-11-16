"""Any doubt about a kept process restarts it into per-call bytes: session mode against the real Claude Code CLI and
the scripted loopback upstream of ``test_directsdk_session_steer`` (no credentials, no inference).

The doubts are forced on what the real CLI emits: its own first resumed request with one row changed past the
opening turn, or one of its own output lines changed as the plugin reads it (a tool call without its id, with an id
the step never received or one already asked, a control request of an unknown kind, a line that is not an object).
Skipped without the CLI; ``CLAUDE_CODE_BIN`` picks a binary.
"""
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admission
import directsdk
from test_directsdk_session import client, home, session_pids  # noqa: F401  (fixtures)
from test_directsdk_session_steer import ROUNDS, converse, per_call, upstream  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (os.environ.get('CLAUDE_CODE_BIN') or shutil.which('claude')), reason='needs the Claude Code CLI')


def restarts(caplog):
    return [r.getMessage() for r in caplog.records if 'session restart' in r.getMessage()]


def test_a_first_resumed_request_that_is_not_per_calls_is_refused_and_replayed(home, upstream, monkeypatch, caplog):
    """The kept process's first resumed request holds the opening turn, then its own record of the first reply and the
    results. Here its record of the reply differs from what a per-call replay sends (one tool argument changed in the
    relay, after the rows were lined up). The rows line up and the request extends the launch's, so only a comparison
    with per-call's own request for the same frames catches it: refused before upstream, replayed, kept after."""
    expected = per_call(home, upstream, monkeypatch, steer_at=None)
    canonical, drifted = admission.canonical_history, []

    def drift(payload, frames):
        out = canonical(payload, frames)
        body = json.loads(out)
        convo = [m for m in body['messages'] if m.get('role') != 'system']
        if len(convo) != 3 or drifted:
            return out
        drifted.append(True)
        convo[1]['content'][-1]['input']['path'] += ' (as native recalls it)'
        return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    monkeypatch.setattr(admission, 'canonical_history', drift)
    kept = client(home, upstream, monkeypatch, session=True)
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            pids = converse(kept, steer_at=None)
    finally:
        kept.close()
    assert drifted, 'the first resumed request never reached the relay'
    assert pids[1] != pids[0] and len(set(pids[1:])) == 1 and None not in pids, pids
    assert restarts(caplog) == ['Claude session restart (diverged): replaying into a new process']
    assert len(expected) == ROUNDS + 1 and upstream.bodies == expected  # the changed request never went upstream

