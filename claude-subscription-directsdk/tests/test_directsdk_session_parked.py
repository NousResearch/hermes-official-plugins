"""A tool parked on a human: session mode against the real Claude Code CLI and a scripted loopback upstream.

Hermes approvals and hand-offs can hold a tool for minutes. The kept process must keep the call open meanwhile and
continue on the same process once the result arrives; when the plugin's own idle eviction fires first, the next step
must run anew with the bytes per-call sends. No credentials, no inference. Skipped without the CLI; ``CLAUDE_CODE_BIN``
picks a binary. ``DIRECTSDK_PARK_SECONDS`` sets the park (default 2 s; run it at 150 to prove minutes).
"""
import os
from pathlib import Path
import sys
import time

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk
from test_directsdk_session import MODEL, TOOLS, client, home, session_pids, upstream  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (os.environ.get('CLAUDE_CODE_BIN') or __import__('shutil').which('claude')),
                                reason='needs the Claude Code CLI')
PARK = float(os.environ.get('DIRECTSDK_PARK_SECONDS', '2'))


def step(c, messages):
    message = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
    messages.append({k: v for k, v in message.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None})
    return message


def results(message):
    return [{'role': 'tool', 'tool_call_id': call['id'], 'content': 'contents of ' + call['id']} for call in message['tool_calls']]


def alive(pid):
    return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE


def test_a_parked_tool_continues_on_the_same_process(home, upstream, monkeypatch):
    """The call stays parked while Hermes holds the tool; the result continues the kept process, no restart."""
    kept = client(home, upstream, monkeypatch, session=True)
    try:
        messages = [{'role': 'system', 'content': 'You are a fixture agent.'}, {'role': 'user', 'content': 'go'}]
        first = step(kept, messages)
        assert first['tool_calls'] and session_pids(kept)
        pid = next(iter(session_pids(kept)))
        time.sleep(PARK)
        assert alive(pid), 'the kept process did not survive the park'
        sent = len(upstream.bodies)
        messages += results(first)
        second = step(kept, messages)
        assert session_pids(kept) == {pid}, 'the parked call was not answered on the same process'
        assert len(upstream.bodies) == sent + 1 and second['tool_calls']  # one request, the step continued
    finally:
        kept.close()


def test_idle_eviction_during_a_park_falls_back_to_per_call_bytes(home, upstream, monkeypatch):
    """Idle eviction fires while the tool is parked: the process is gone, and the next step replays into a new one
    whose request is exactly what a one-shot sends for the same history."""
    monkeypatch.setattr(directsdk, 'SESSION_IDLE_S', .5)
    kept, one_shot = client(home, upstream, monkeypatch, session=True), None
    try:
        messages = [{'role': 'system', 'content': 'You are a fixture agent.'}, {'role': 'user', 'content': 'go'}]
        first = step(kept, messages)
        pid = next(iter(session_pids(kept)))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and alive(pid):
            time.sleep(.05)
        assert not alive(pid), 'idle eviction did not end the parked process'
        assert not session_pids(kept)
        messages += results(first)
        history = list(messages)
        upstream.bodies = []
        step(kept, messages)
        replayed = upstream.bodies[-1]
        assert session_pids(kept) and session_pids(kept) != {pid}
        one_shot = directsdk.Client(command=kept.command, timeout=60, env=kept.env)
        monkeypatch.setattr(directsdk, 'session_settings', lambda: (False, 4))
        upstream.bodies = []
        one_shot.create(model=MODEL, messages=history, tools=TOOLS, max_tokens=1000)
        assert upstream.bodies == [replayed]
    finally:
        kept.close()
        if one_shot is not None:
            one_shot.close()
