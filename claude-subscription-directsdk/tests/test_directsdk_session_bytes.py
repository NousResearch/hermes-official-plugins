"""A kept process sends per-call's bytes or nothing: histories where its request differed from per-call's.

Each case runs the same Hermes script per-call and kept and compares every upstream body byte for byte. A request the
relay cannot make per-call's is refused before upstream and the step replays on a new process. Real Claude Code
against a scripted loopback upstream; no credentials, no inference. Skipped without the CLI; ``CLAUDE_CODE_BIN``
picks a binary.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import threading

import pytest

from test_directsdk_session import MODEL, TOOLS, USAGE, client, home  # noqa: F401  (fixtures)
from test_directsdk_session_steer import STEER, upstream  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (os.environ.get('CLAUDE_CODE_BIN') or shutil.which('claude')), reason='needs the Claude Code CLI')


def keep(message):
    return {k: v for k, v in message.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None}


def both(home, upstream, monkeypatch, script):
    """``script(client)`` per-call, then kept; returns (per-call bodies, kept bodies, what the kept run returned)."""
    one = client(home, upstream, monkeypatch, session=False)
    try:
        script(one)
    finally:
        one.close()
    expected, upstream.bodies[:] = list(upstream.bodies), []
    kept = client(home, upstream, monkeypatch, session=True)
    try:
        out = script(kept)
    finally:
        kept.close()
    return expected, list(upstream.bodies), out


def first_difference(a, b):
    at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return at, a[max(0, at - 60):at + 120], b[max(0, at - 60):at + 120]


# A new process's first request is the replay itself.

FIRST = {
    'empty-user-text': [{'type': 'text', 'text': ''}, {'type': 'text', 'text': 'second'}],
    'whitespace-user-text': [{'type': 'text', 'text': ' \t\n'}, {'type': 'text', 'text': 'second'}],
    'marked-user-text': [{'type': 'text', 'text': 'a', 'cache_control': {'type': 'ephemeral'}}, {'type': 'text', 'text': 'b'}],
    'consecutive-user-text': [{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}],
}


@pytest.mark.parametrize('shape', FIRST)
def test_a_new_process_sends_its_first_request_as_per_call_builds_it(home, upstream, monkeypatch, shape):
    """A history with an unusual earlier user turn, replayed into a new kept process. Before, the relay rewrote
    that first request as if the process held the history itself (an empty block put back, a mark copied)."""
    history = [{'role': 'system', 'content': 'You are a fixture agent.'}, {'role': 'user', 'content': 'go'},
               {'role': 'assistant', 'content': 'ack'}, {'role': 'user', 'content': FIRST[shape]},
               {'role': 'assistant', 'content': 'ack again'}, {'role': 'user', 'content': 'next'}]
    expected, actual, _ = both(home, upstream, monkeypatch,
                               lambda c: c.create(model=MODEL, messages=history, tools=TOOLS, max_tokens=1000))
    assert actual == expected, first_difference(actual[0], expected[0])


def test_a_restart_for_another_model_sends_per_calls_bytes(home, upstream, monkeypatch):
    """The model changes after two kept steps: the process restarts, and the new one's first request carries the
    context per-call sends for that model. Before, the relay removed it."""
    def script(c):
        messages = [{'role': 'system', 'content': 'fixture'}, {'role': 'user', 'content': 'go'}]
        for _ in range(2):
            reply = c.create(model=MODEL, messages=messages, tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
            messages += [keep(reply), {'role': 'tool', 'tool_call_id': reply['tool_calls'][0]['id'], 'content': 'result'}]
        c.create(model='claude-opus-4-6', messages=messages, tools=TOOLS, max_tokens=1000)
    expected, actual, _ = both(home, upstream, monkeypatch, script)
    assert actual == expected, first_difference(actual[-1], expected[-1])


# An earlier user turn with a mark of its own.

def test_an_earlier_marked_user_turn_keeps_only_its_own_mark(home, upstream, monkeypatch):
    """A second user turn holds two text blocks, the first marked. On later kept steps the relay rewrote that turn to
    Hermes' frame and also copied the mark onto its last block: one mark more than per-call sends."""
    def script(c):
        messages = [{'role': 'system', 'content': 'You are a fixture agent.'}, {'role': 'user', 'content': 'go'}]
        for turn in range(2):
            if turn:
                messages.append({'role': 'user', 'content': [{'type': 'text', 'text': 'a', 'cache_control': {'type': 'ephemeral'}},
                                                             {'type': 'text', 'text': 'b'}]})
            while True:
                reply = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
                messages.append(keep(reply))
                if not reply.get('tool_calls'):
                    break
                messages.append({'role': 'tool', 'tool_call_id': reply['tool_calls'][0]['id'], 'content': 'ordinary result'})
    expected, actual, _ = both(home, upstream, monkeypatch, script)
    assert len(actual) == len(expected) and actual == expected, first_difference(actual[-1], expected[-1])
