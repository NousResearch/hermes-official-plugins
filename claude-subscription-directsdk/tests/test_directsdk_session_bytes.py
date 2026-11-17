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

import directsdk
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


# A later result, plain or with a steer.

def results(result, steer):
    """Four steps; the second step's tool result is ``result``, followed by a steer when ``steer``."""
    def script(c):
        messages = [{'role': 'system', 'content': 'You are a fixture agent.'}, {'role': 'user', 'content': 'go'}]
        for step in range(4):
            reply = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
            messages += [keep(reply), {'role': 'tool', 'tool_call_id': reply['tool_calls'][0]['id'],
                                       **(result if step == 1 else {'content': 'ordinary result'})}]
            if step == 1 and steer:
                messages.append({'role': 'user', 'content': STEER})
    return script


@pytest.mark.parametrize('result', [
    pytest.param({'content': ' \n\t'}, id='whitespace'), pytest.param({'content': 'x' * 600_000}, id='large')])
def test_a_steered_result_claude_code_reworded_goes_up_as_per_calls_bytes(home, upstream, monkeypatch, result):
    """Claude Code replaces a whitespace-only result with its own "completed with no output" text, and a very large
    one with a preview. With a steer after it, the relay found the frame did not match Hermes' and still forwarded
    native's version. Now the steered frame is rewritten to Hermes' bytes (and one the relay still cannot restore is
    refused before upstream)."""
    expected, actual, _ = both(home, upstream, monkeypatch, results(result, steer=True))
    assert actual == expected, first_difference(actual[2], expected[2])


@pytest.mark.parametrize('result, steer', [
    pytest.param({'content': 'oops', 'is_error': True}, False, id='error'),
    pytest.param({'content': 'oops', 'is_error': True}, True, id='error-steer'),
    pytest.param({'content': '', 'is_error': True}, True, id='empty-error-steer')])
def test_a_later_error_result_goes_up_with_per_calls_key_order(home, upstream, monkeypatch, result, steer):
    """Native and Hermes hold an error result with the same keys in another order; equal as dicts, not as bytes.
    The relay kept native's order. Now a result is compared, and rewritten, as bytes."""
    expected, actual, _ = both(home, upstream, monkeypatch, results(result, steer))
    assert actual == expected, first_difference(actual[2], expected[2])


# A later reply.

def tool(n):
    return {'type': 'tool_use', 'id': f'toolu_{n:04d}', 'name': 'mcp__hermes__read_file', 'input': {'path': f'f{n}'}}


class Replies(BaseHTTPRequestHandler):
    """A tool call per step; the second reply leads with ``server.blocks``."""

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        self.server.bodies.append(raw)
        n = sum(m['role'] == 'assistant' for m in json.loads(raw)['messages'])
        blocks = (self.server.blocks if n == 1 else []) + [tool(n)]
        events = [{'type': 'message_start', 'message': {'id': f'msg_{n}', 'type': 'message', 'role': 'assistant',
                                                       'model': MODEL, 'content': [], 'usage': USAGE}}]
        for i, block in enumerate(blocks):
            field, kind = ('input', 'input_json_delta') if block['type'] == 'tool_use' else ('text', 'text_delta')
            delta = {'type': kind, **({'partial_json': json.dumps(block['input'])} if field == 'input' else {'text': block['text']})}
            events += [{'type': 'content_block_start', 'index': i, 'content_block': {**block, field: {} if field == 'input' else ''}},
                       {'type': 'content_block_delta', 'index': i, 'delta': delta},
                       {'type': 'content_block_stop', 'index': i}]
        events += [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': USAGE}, {'type': 'message_stop'}]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


@pytest.fixture
def replies():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Replies)
    server.bodies, server.blocks = [], []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize('blocks', [
    pytest.param([{'type': 'text', 'text': '  \n\t'}], id='whitespace-text'),
    pytest.param([{'type': 'text', 'text': 'one'}, {'type': 'text', 'text': 'two'}], id='two-texts')])
def test_native_s_record_of_a_later_reply_goes_up_only_as_hermes_frame(home, replies, monkeypatch, blocks):
    """The second reply leads with a whitespace-only text block. Claude Code's live history drops it; a per-call
    replay sends it. Native's record of a reply now has to be Hermes' frame, byte for byte, or the request is
    refused and replayed."""
    replies.blocks = blocks

    def script(c):
        messages = [{'role': 'user', 'content': 'go'}]
        for _ in range(4):
            reply = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
            messages += [keep(reply)] + [{'role': 'tool', 'tool_call_id': t['id'], 'content': 'ordinary result'} for t in reply['tool_calls']]
    expected, actual, _ = both(home, replies, monkeypatch, script)
    assert actual == expected, first_difference(actual[-1], expected[-1])


# An unusual earlier user turn, on every later step.

@pytest.fixture
def turns():
    """Five silent tool rounds per user turn, then thinking and text (``test_directsdk_session``'s upstream)."""
    from test_directsdk_session import Upstream as Turns
    server = ThreadingHTTPServer(('127.0.0.1', 0), Turns)
    server.bodies, server.slow = [], 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize('shape', FIRST)
def test_an_unusual_earlier_user_turn_keeps_its_process_and_per_calls_bytes(home, turns, monkeypatch, caplog, shape):
    """The second user turn holds an unusual content list, and its five tool rounds run on the kept process. On each
    of them that turn is an earlier row. Before, the relay rewrote it to Hermes' frame: it put back the empty block
    native drops, so no request extended the one before and every step was refused and relaunched; or it copied a
    mark next to Hermes' own, one more than per-call sends."""
    def script(c):
        messages, pids = [{'role': 'system', 'content': 'You are a fixture agent.'}, {'role': 'user', 'content': 'go'}], []
        for turn in ('go', FIRST[shape]):
            if turn is not messages[1]['content']:
                messages.append({'role': 'user', 'content': turn})
            while True:
                reply = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
                pids.append(c._session.process.pid if c._session else None)
                messages.append(keep(reply))
                if not reply.get('tool_calls'):
                    break
                messages.append({'role': 'tool', 'tool_call_id': reply['tool_calls'][0]['id'], 'content': 'ordinary result'})
        return pids
    with caplog.at_level('DEBUG', logger=directsdk.logger.name):
        expected, actual, pids = both(home, turns, monkeypatch, script)
    assert len(actual) == len(expected) and actual == expected, first_difference(actual[-1], expected[-1])
    assert len(set(pids)) == 1 and pids[0] is not None, pids
    assert not [r for r in caplog.records if 'session restart' in r.getMessage()]
