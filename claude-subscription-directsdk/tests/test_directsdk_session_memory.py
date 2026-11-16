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


def test_at_max_sessions_the_longest_idle_kept_process_gives_its_place_up(home, upstream, monkeypatch, caplog):
    """``max_sessions`` 1. Agent A ends its turn and waits for the user; agent B's first step takes A's place (A's
    process ends). Then B waits on a parked tool call, and agent C's step runs per-call: a parked call is never
    evicted. Before, B ran per-call beside an idle A until A's idle hour passed."""
    a, b, c = (client(home, upstream, monkeypatch, session=True, cap=1) for _ in range(3))
    try:
        converse(a, turns=('go',))
        first = next(iter(session_pids(a)))
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            b.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
        assert session_pids(b) and session_pids(b) != {first} and a._session is None and ended(first)
        assert [r.getMessage() for r in caplog.records if 'evicted' in r.getMessage()] == \
            ['Claude session evicted (the longest idle at max_sessions 1): its place goes to a step asking now']
        parked = next(iter(session_pids(b)))
        c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
        assert not session_pids(c) and session_pids(b) == {parked} and not ended(parked)
    finally:
        for each in (a, b, c):
            each.close()


def test_a_parked_process_ends_when_the_history_falls_below_session_min_rounds(home, upstream, monkeypatch, caplog):
    """``session_min_rounds`` 3: the fourth step keeps a process, which parks its tool call. Hermes then compacts the
    history to fewer rounds. The next step still meets the parked process: it cannot continue, ends, and
    the step runs per-call. Before, that step skipped the kept process altogether and the parked process held its
    memory and its place with no idle limit."""
    c = client(home, upstream, monkeypatch, session=True, min_rounds=3)
    try:
        messages = [{'role': 'system', 'content': 'You are a fixture agent.'}, {'role': 'user', 'content': 'go'}]
        for _ in range(4):
            reply = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
            messages.append({k: v for k, v in reply.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None})
            messages += [{'role': 'tool', 'tool_call_id': call['id'], 'content': 'contents'} for call in reply['tool_calls']]
        pid = next(iter(session_pids(c)))
        compacted = messages[:2] + [{'role': 'user', 'content': 'Summary of earlier work: read f0..f3.'},
                                    {'role': 'user', 'content': 'and now?'}]
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            c.create(model=MODEL, messages=compacted, tools=TOOLS, max_tokens=1000)
        assert ended(pid) and not session_pids(c)
        # The compaction changes the history the process holds (``edited``), here even what it was launched with
        # (``launch``: the conversation's session id comes from its opening).
        restarts = [r.getMessage() for r in caplog.records if 'session restart' in r.getMessage()]
        assert restarts in (['Claude session restart (launch): replaying into a new process'],
                            ['Claude session restart (edited): replaying into a new process']), restarts
    finally:
        c.close()


def test_a_step_with_several_tool_calls_is_one_round(home, monkeypatch):
    """Rounds are Hermes' tool-result frames. Three calls in one step are one round: with ``session_min_rounds`` 2 the
    process is kept from the third step, not the second (Claude Code holds each call as its own assistant message)."""
    from test_directsdk_session_steer import Upstream as Steps, converse as loop
    from http.server import ThreadingHTTPServer
    import threading
    server = ThreadingHTTPServer(('127.0.0.1', 0), Steps)
    server.bodies, server.calls = [], 3
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = client(home, server, monkeypatch, session=True, min_rounds=2)
        try:
            pids = loop(c, steer_at=None)
        finally:
            c.close()
    finally:
        server.shutdown()
        server.server_close()
    assert pids[:2] == [None, None] and pids[2] is not None and len(set(pids[2:])) == 1, pids


def test_a_call_without_tools_ends_the_parked_process_it_cannot_use(home, upstream, monkeypatch):
    """The agent's next call brings the results but no tools (Hermes asking for a final answer without them): it runs
    per-call, and the process parked on the earlier call ends instead of staying with no idle limit. A title call
    without tools in between leaves the parked process alone."""
    c = client(home, upstream, monkeypatch, session=True)
    try:
        messages = [{'role': 'system', 'content': 'You are a fixture agent.'}, {'role': 'user', 'content': 'go'}]
        reply = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
        pid = next(iter(session_pids(c)))
        c.create(model=MODEL, messages=[{'role': 'user', 'content': 'title this'}], max_tokens=100, hermes_auxiliary=True)
        assert session_pids(c) == {pid}
        messages.append({k: v for k, v in reply.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None})
        messages += [{'role': 'tool', 'tool_call_id': call['id'], 'content': 'contents'} for call in reply['tool_calls']]
        c.create(model=MODEL, messages=messages, max_tokens=1000)
        assert ended(pid) and not session_pids(c)
    finally:
        c.close()
