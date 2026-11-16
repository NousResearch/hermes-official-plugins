"""A /steer mid tool loop: session mode against the real Claude Code CLI and a scripted loopback upstream.

Hermes delivers a steer as a user row right after the newest tool results; the plugin folds it into that frame, a
text block after the results. The kept process takes it on its stdin before the results, so native sends one request
with both (holding the steer as a system row of its own, which the relay puts back where Hermes has it). The bytes
must be per-call's. A steer the process cannot carry restarts into per-call bytes, and the new process is kept. No
credentials, no inference. Skipped without the CLI; ``CLAUDE_CODE_BIN`` picks a binary.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admission
import directsdk
from test_directsdk_session import MODEL, TOOLS, USAGE, client, home, session_pids  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (os.environ.get('CLAUDE_CODE_BIN') or shutil.which('claude')), reason='needs the Claude Code CLI')
STEER = ('[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool '
         'output and not a new delivery when replayed from conversation history]\nread g1 instead\n[/OUT-OF-BAND USER MESSAGE]')
ROUNDS = 5  # assistant steps with tool calls before the answer


class Upstream(BaseHTTPRequestHandler):
    """Answers from the history alone: ``server.calls`` tool calls in the first step, one per step after, then text.
    Every tool_use id is unique in the conversation (native drops a repeated one)."""

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        self.server.bodies.append(raw)
        rounds = sum(m['role'] == 'assistant' for m in json.loads(raw)['messages'])
        blocks = [{'type': 'tool_use', 'id': f'toolu_{rounds:02d}_{j}', 'name': 'mcp__hermes__read_file', 'input': {'path': f'f{rounds}_{j}'}}
                  for j in range(self.server.calls if rounds == 0 else 1)] if rounds < ROUNDS else [{'type': 'text', 'text': f'step {rounds}'}]
        events = [{'type': 'message_start', 'message': {'id': f'msg_{rounds}', 'type': 'message', 'role': 'assistant',
                                                       'model': MODEL, 'content': [], 'usage': USAGE}}]
        for i, block in enumerate(blocks):
            tool = block['type'] == 'tool_use'
            events += [{'type': 'content_block_start', 'index': i, 'content_block': {**block, **({'input': {}} if tool else {'text': ''})}},
                       {'type': 'content_block_delta', 'index': i, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
                        if tool else {'type': 'text_delta', 'text': block['text']}},
                       {'type': 'content_block_stop', 'index': i}]
        events += [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if rounds < ROUNDS else 'end_turn'}, 'usage': USAGE},
                   {'type': 'message_stop'}]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    server.bodies, server.calls = [], 1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def converse(c, steer_at, steer=(STEER,)):
    """Hermes' tool loop; after the results of step ``steer_at`` the steer comes in as user rows. Returns each step's
    kept pid."""
    messages = [{'role': 'system', 'content': 'You are a fixture agent.'}, {'role': 'user', 'content': 'go'}]
    pids = []
    for step in range(ROUNDS + 1):
        message = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
        pids.append(next(iter(session_pids(c)), None))
        messages.append({k: v for k, v in message.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None})
        if not message.get('tool_calls'):
            return pids
        messages += [{'role': 'tool', 'tool_call_id': call['id'], 'content': 'contents of ' + call['id']} for call in message['tool_calls']]
        if step == steer_at:
            messages += [{'role': 'user', 'content': text} for text in steer]
    raise AssertionError('the loop did not end')


def per_call(home, upstream, monkeypatch, steer_at, login='api_key', **kwargs):
    one_shot = client(home, upstream, monkeypatch, session=False, login=login)
    try:
        converse(one_shot, steer_at, **kwargs)
    finally:
        one_shot.close()
    expected, upstream.bodies[:] = list(upstream.bodies), []
    return expected


@pytest.mark.parametrize('login', ['api_key', 'claude_ai'])
@pytest.mark.parametrize('calls', [1, 3])
def test_a_steer_continues_the_kept_process(home, upstream, monkeypatch, caplog, calls, login):
    """The steer lands after the results of one call, or of three parked in one step (native asks for them one at a
    time): the same process serves the whole loop, every request is per-call's, the steer where Hermes put it. The
    claude.ai login adds native's account reminder to the opening turn, as a live login does."""
    upstream.calls = calls
    expected = per_call(home, upstream, monkeypatch, steer_at=0, login=login)
    kept = client(home, upstream, monkeypatch, session=True, login=login)
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            pids = converse(kept, steer_at=0)
    finally:
        kept.close()
    assert len(set(pids)) == 1 and pids[0] is not None, f'the steer restarted the kept process: {pids}'
    assert not [r for r in caplog.records if 'session restart' in r.getMessage()]
    assert len(expected) == ROUNDS + 1 and upstream.bodies == expected
    steered = json.loads(upstream.bodies[1])['messages'][-1]['content']
    assert [b['type'] for b in steered] == ['tool_result'] * calls + ['text'] and steered[-1]['text'] == STEER


@pytest.mark.parametrize('why', ['steer', 'diverged'])
def test_a_steer_that_cannot_be_carried_restarts_into_per_call_bytes(home, upstream, monkeypatch, caplog, why):
    """``steer``: two user rows after the results (two text blocks), which the kept path does not carry. ``diverged``:
    a Claude Code that words its steer row otherwise (forced here), so the relay cannot put it back and refuses before
    upstream. Either way the step restarts, logs why, sends per-call's bytes, and the new process is the kept one."""
    steer = (STEER, 'and then stop') if why == 'steer' else (STEER,)
    expected = per_call(home, upstream, monkeypatch, steer_at=0, steer=steer)
    if why == 'diverged':
        monkeypatch.setattr(admission, 'STEER_ROW', ('Another wording:\n', ''), raising=False)
    kept = client(home, upstream, monkeypatch, session=True)
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            pids = converse(kept, steer_at=0, steer=steer)
    finally:
        kept.close()
    assert pids[1] != pids[0] and len(set(pids[1:])) == 1, f'not one restart at the steer, then one kept process: {pids}'
    assert [r.getMessage() for r in caplog.records if 'session restart' in r.getMessage()] == \
        [f'Claude session restart ({why}): replaying into a new process']
    assert upstream.bodies == expected  # a refused request never reached upstream
