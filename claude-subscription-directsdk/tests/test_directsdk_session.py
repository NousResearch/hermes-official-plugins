"""Session mode against the real Claude Code CLI and a scripted loopback upstream (no credentials, no inference).

The invariant: a kept-alive Claude Code sends upstream exactly the bytes a fresh one-shot sends for the same
history, so cache and cost cannot differ; and the process never outlives its owner. Skipped without the CLI;
``CLAUDE_CODE_BIN`` picks a binary (the CI pin in evals/ci.sh).
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

CLAUDE = os.environ.get('CLAUDE_CODE_BIN') or shutil.which('claude')
pytestmark = pytest.mark.skipif(not CLAUDE, reason='needs the Claude Code CLI')
MODEL = 'claude-sonnet-5-5'
TOOLS = [{'type': 'function', 'function': {'name': 'read_file', 'description': 'Read a file',
          'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}}]
USAGE = {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}


class Upstream(BaseHTTPRequestHandler):
    """Answers from the history alone (two tool rounds per user turn, then text), so a kept process and a fresh one
    get the same reply for the same conversation; keeps every body as received."""

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        self.server.bodies.append(raw)
        body = json.loads(raw)
        rounds = sum(m['role'] == 'assistant' for m in body['messages'])
        tool = rounds % 3 < 2
        blocks = [{'type': 'thinking', 'thinking': f'plan {rounds}', 'signature': f'sig{rounds:04d}'},
                  {'type': 'text', 'text': f'step {rounds}'}]
        if tool:
            blocks.append({'type': 'tool_use', 'id': f'toolu_{rounds:04d}', 'name': 'mcp__hermes__read_file',
                           'input': {'path': f'f{rounds}'}})
        events = [{'type': 'message_start', 'message': {'id': f'msg_{rounds}', 'type': 'message', 'role': 'assistant',
                                                       'model': body['model'], 'content': [], 'usage': USAGE}}]
        for i, block in enumerate(blocks):
            start = {**block, **({'thinking': '', 'signature': ''} if block['type'] == 'thinking' else
                                 {'input': {}} if block['type'] == 'tool_use' else {'text': ''})}
            events.append({'type': 'content_block_start', 'index': i, 'content_block': start})
            delta = ({'type': 'thinking_delta', 'thinking': block['thinking']}, {'type': 'signature_delta', 'signature': block['signature']}) \
                if block['type'] == 'thinking' else ({'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])},) \
                if block['type'] == 'tool_use' else ({'type': 'text_delta', 'text': block['text']},)
            events += [{'type': 'content_block_delta', 'index': i, 'delta': d} for d in delta]
            events.append({'type': 'content_block_stop', 'index': i})
        events += [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if tool else 'end_turn'}, 'usage': USAGE},
                   {'type': 'message_stop'}]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
            time.sleep(self.server.slow)


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    server.bodies, server.slow = [], 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def home():
    # One HOME for both runs: native derives its device id from it, and that id is on the wire.
    with tempfile.TemporaryDirectory(prefix='directsdk-session-') as path:
        yield path


EMAIL = 'fixture@example.invalid'


def client(home, upstream, monkeypatch, session, cap=4, login='api_key'):
    monkeypatch.setattr(directsdk, 'session_settings', lambda: (session, cap))
    if login == 'claude_ai':
        # A claude.ai login with an account email: native prepends the userEmail reminder to the opening user turn.
        account = Path(home) / '.claude.json'
        state = json.loads(account.read_text()) if account.exists() else {}
        state['oauthAccount'] = {'emailAddress': EMAIL, 'accountUuid': '00000000-0000-4000-8000-000000000001',
                                 'organizationUuid': '00000000-0000-4000-8000-000000000002'}
        account.write_text(json.dumps(state))
        credential = {'CLAUDE_CODE_OAUTH_TOKEN': 'sk-ant-oat01-fixture'}
    else:
        credential = {'ANTHROPIC_API_KEY': 'fixture-key'}
    return directsdk.Client(command=CLAUDE, timeout=60, env={
        'PATH': os.defpath, 'HOME': home, 'CLAUDE_CONFIG_DIR': home, **credential,
        'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{upstream.server_port}', 'NO_PROXY': '127.0.0.1',
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'})


def converse(c, turns=('go', 'and now?'), on_step=None):
    """Hermes' loop: one create() per model step, the tool result appended for each call."""
    messages = [{'role': 'system', 'content': 'You are a fixture agent. ' * 40}]
    for text in turns:
        messages.append({'role': 'user', 'content': text})
        while True:
            message = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
            if on_step:
                on_step()
            messages.append({k: v for k, v in message.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None})
            if not message.get('tool_calls'):
                break
            messages += [{'role': 'tool', 'tool_call_id': call['id'], 'content': 'contents of ' + call['id'] + ' ' + 'x' * 64}
                         for call in message['tool_calls']]
    return messages


def session_pids(c):
    native = c._session
    return {native.process.pid} if native is not None else set()


# claude_ai is the live A/B's shape: the kept native replayed its opening turn with the reminder per-call never sent,
# so every session restarted after its first step (two processes per conversation, slower than per-call).
@pytest.mark.parametrize('login', ['api_key', 'claude_ai'])
def test_kept_session_sends_the_same_bytes_as_one_shots(home, upstream, monkeypatch, caplog, login):
    """One kept process serves the conversation with the bytes per-call sends; a diverged history (a compaction-like
    edit) restarts it and still matches per-call; past the cap another agent runs per-call with the same bytes."""
    one_shot = client(home, upstream, monkeypatch, session=False, login=login)
    history = converse(one_shot)
    one_shot.close()
    expected, upstream.bodies = list(upstream.bodies), []
    kept, pids, other = client(home, upstream, monkeypatch, session=True, cap=1, login=login), [], None
    try:
        with caplog.at_level('INFO', logger=directsdk.logger.name):
            assert converse(kept, on_step=lambda: pids.append(session_pids(kept))) == history
        assert len(expected) == 6 and upstream.bodies == expected
        # One process served the whole conversation, and it never diverged.
        assert len(set().union(*pids)) == 1
        assert not [r for r in caplog.records if 'diverged' in r.getMessage()]
        other, upstream.bodies = directsdk.Client(command=kept.command, timeout=60, env=kept.env), []
        assert converse(other) == history and not session_pids(other)
        assert upstream.bodies == expected
        first = session_pids(kept)
        history[1] = {'role': 'user', 'content': 'go (summarised)'}
        history.append({'role': 'user', 'content': 'next'})
        kept.create(model=MODEL, messages=history, tools=TOOLS, max_tokens=1000)
        assert session_pids(kept) and session_pids(kept) != first
        after, upstream.bodies = upstream.bodies[-1], []
        other.create(model=MODEL, messages=history, tools=TOOLS, max_tokens=1000)
        assert upstream.bodies == [after]
    finally:
        kept.close()
        if other is not None:
            other.close()


def test_a_kept_session_never_outlives_its_turn_or_its_owner(home, upstream, monkeypatch):
    """Cancel mid-stream kills the kept process and the retry sends exactly the interrupted request; a SIGKILLed
    Hermes takes its session with it (native's stdin closes with the owner)."""
    kept = client(home, upstream, monkeypatch, session=True)
    try:
        history = converse(kept, turns=('go',))
        pid = next(iter(session_pids(kept)))
        history.append({'role': 'user', 'content': 'and now?'})
        upstream.slow, errors = .05, []
        sent = len(upstream.bodies)

        def step():
            try:
                kept.create(model=MODEL, messages=list(history), tools=TOOLS, max_tokens=1000)
            except RuntimeError as error:
                errors.append(str(error))
        worker = threading.Thread(target=step)
        worker.start()
        while len(upstream.bodies) == sent:
            time.sleep(.005)
        time.sleep(.1)
        kept.cancel()
        worker.join(5)
        assert errors == ['Claude request cancelled'] and not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        upstream.slow = 0
        kept.create(model=MODEL, messages=list(history), tools=TOOLS, max_tokens=1000)
    finally:
        kept.close()
    assert upstream.bodies[-1] == upstream.bodies[-2]  # the retry sent exactly the interrupted request
    if os.name == 'nt':
        return  # POSIX SIGKILL of the owner
    owner = subprocess.Popen([sys.executable, '-c', f'''
import sys
sys.path[:0] = {[str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]!r}
import test_directsdk_session as t, directsdk, time
directsdk.session_settings = lambda: (True, 4)
c = directsdk.Client(command={CLAUDE!r}, timeout=60, env={{"PATH": "{os.defpath}", "HOME": {home!r}, "CLAUDE_CONFIG_DIR": {home!r},
    "ANTHROPIC_API_KEY": "***", "ANTHROPIC_BASE_URL": "http://127.0.0.1:{upstream.server_port}", "NO_PROXY": "127.0.0.1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}})
c.create(model=t.MODEL, messages=[{{"role": "user", "content": "go"}}], tools=t.TOOLS, max_tokens=1000)
print(c._session.process.pid, flush=True)
time.sleep(60)
'''], stdout=subprocess.PIPE, text=True, env={**os.environ, 'PYTHONPATH': os.environ.get('PYTHONPATH', '')})
    pid = int(owner.stdout.readline())
    owner.send_signal(signal.SIGKILL)
    owner.wait()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
        time.sleep(.05)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
