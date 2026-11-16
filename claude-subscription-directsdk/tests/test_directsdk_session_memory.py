"""Memory follows the benefit: a conversation is kept only after ``session_min_rounds`` tool rounds, and at
``max_sessions`` the kept process that has waited longest for a new user turn gives its place to the step asking now.
Real Claude Code CLI against the scripted loopback upstream of ``test_directsdk_session``; no credentials, no
inference. Skipped without the CLI; ``CLAUDE_CODE_BIN`` picks a binary.
"""
import os
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk
from test_directsdk_session import MODEL, TOOLS, client, converse, ended, home, session_pids, upstream  # noqa: F401

pytestmark = pytest.mark.skipif(not (os.environ.get('CLAUDE_CODE_BIN') or shutil.which('claude')), reason='needs the Claude Code CLI')


def configure(path, **settings):
    """What `hermes config set plugins.entries.<id>.settings.<key> <value>` writes for this profile."""
    rows = ''.join(f'        {key}: {value}\n' for key, value in settings.items())
    (path / 'config.yaml').write_text('plugins:\n  entries:\n    claude-subscription-directsdk-experimental:\n'
                                      '      settings:\n' + rows, encoding='utf-8')


def test_a_conversation_is_kept_only_after_session_min_rounds_tool_rounds(home, upstream, monkeypatch, tmp_path, caplog):
    """``session_min_rounds`` 3 from the profile's config: the first three steps run per-call, the fourth starts the
    process that serves every later step of both turns. Every request is per-call's."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    configure(tmp_path, session_min_rounds=3)
    one_shot = client(home, upstream, monkeypatch, session=False, min_rounds=None)
    history = converse(one_shot)
    one_shot.close()
    expected, upstream.bodies = list(upstream.bodies), []
    kept, pids = client(home, upstream, monkeypatch, session=True, min_rounds=None), []
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            assert converse(kept, on_step=lambda: pids.append(session_pids(kept))) == history
    finally:
        kept.close()
    assert len(pids) == 12 and pids[:3] == [set()] * 3, pids
    assert all(pids[3:]) and len(set().union(*pids[3:])) == 1, pids
    assert not [r for r in caplog.records if 'session restart' in r.getMessage()]
    assert upstream.bodies == expected

