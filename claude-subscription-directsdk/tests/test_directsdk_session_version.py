"""Session mode runs only on a Claude Code version its tests passed on (``session_versions`` in plugin.yaml); any
other runs per-call, with one log line that says why. Real Claude Code CLI against the scripted loopback upstream of
``test_directsdk_session_steer``; no credentials, no inference. Skipped without the CLI; ``CLAUDE_CODE_BIN`` picks a
binary (evals/ci.sh runs this on the pin, so a pin bumped without listing it fails here).
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk
import directsdk_setup
from test_directsdk_session import client, home, session_pids  # noqa: F401  (fixtures)
from test_directsdk_session_steer import ROUNDS, converse, per_call, upstream  # noqa: F401  (fixtures)

CLAUDE = os.environ.get('CLAUDE_CODE_BIN') or shutil.which('claude')
pytestmark = pytest.mark.skipif(not CLAUDE, reason='needs the Claude Code CLI')
WHY = 'session mode runs only on Claude Code'


def printed():
    """The version this Claude Code prints for itself."""
    return subprocess.check_output([CLAUDE, '--version'], text=True, stdin=subprocess.DEVNULL).split()[0]


def lines(caplog, text):
    return [r.getMessage() for r in caplog.records if text in r.getMessage()]


def test_this_claude_code_is_a_version_session_mode_runs_on():
    assert printed() in directsdk_setup.session_versions()
    assert directsdk_setup.claude_version([CLAUDE], dict(os.environ)) == printed()


def test_an_untested_claude_code_runs_every_call_per_call(home, upstream, monkeypatch, caplog):
    """Its version is not listed: no process is kept, every request is per-call's, and one log line (not one per call)
    names the version it read and the ones it runs on."""
    expected = per_call(home, upstream, monkeypatch, steer_at=None)
    monkeypatch.setattr(directsdk_setup, 'session_versions', lambda: frozenset({'2.0.0'}))
    monkeypatch.setattr(directsdk_setup, '_untested', set())
    kept = client(home, upstream, monkeypatch, session=True)
    try:
        with caplog.at_level('DEBUG'):
            pids = converse(kept, steer_at=None)
    finally:
        kept.close()
    assert pids == [None] * (ROUNDS + 1)
    assert lines(caplog, WHY) == [f'claude-subscription-directsdk: {WHY} 2.0.0; this is {printed()}, so every call runs '
                                  'per-call (a new process each)']
    assert upstream.bodies == expected


def test_a_kept_process_ends_when_its_version_is_no_longer_one_session_mode_runs_on(home, upstream, monkeypatch, caplog):
    """The list changes under a kept process (here: a plugin update drops the version). The next step ends it with
    the reason ``version`` and runs per-call from then on, with per-call's bytes."""
    expected = per_call(home, upstream, monkeypatch, steer_at=None)
    tested, steps = directsdk_setup.session_versions, []
    monkeypatch.setattr(directsdk_setup, 'session_versions', lambda: tested() if len(steps) < 2 else frozenset())
    monkeypatch.setattr(directsdk_setup, '_untested', set())
    kept = client(home, upstream, monkeypatch, session=True)
    create = kept.create

    def step(**kwargs):
        steps.append(True)
        return create(**kwargs)
    kept.create = step
    try:
        with caplog.at_level('DEBUG'):
            pids = converse(kept, steer_at=None)
    finally:
        kept.close()
    assert pids[0] is not None and pids[1:] == [None] * ROUNDS, pids
    assert lines(caplog, 'session restart') == ['Claude session restart (version): replaying into a new process']
    assert len(lines(caplog, WHY)) == 1
    assert upstream.bodies == expected
