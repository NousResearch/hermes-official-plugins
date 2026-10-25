"""Claude Code's own memory and account context never reach Claude as part of the user's turn (#195).

Native loads its auto memory (``MEMORY.md``) and managed CLAUDE.md even with ``--setting-sources ''``
and, on a subscription login, prepends them with the account email to the conversation's first
user turn as ``<system-reminder>`` blocks. Claude then reads Claude Code's notes as text smuggled
into the user's message. The child is told not to load them, and the relay sends the first turn as
Hermes queried it, which is also how every later request replays it.
"""
import copy
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from admission import Admission
from test_directsdk_telemetry import _turn_env

MARK = {'type': 'ephemeral', 'ttl': '1h'}
FRAME = {'type': 'text', 'text': 'Is this repo well-maintained?'}
MEMORY = {'type': 'text', 'text': '<system-reminder>\nContents of /home/u/.claude/projects/p/memory/MEMORY.md '
                                  "(user's auto-memory, persists across conversations):\n\n- fixture project note\n"
                                  '</system-reminder>\n'}
EMAIL = {'type': 'text', 'text': "<system-reminder>\nAs you answer the user's questions, you can use the following "
                                 "context:\n# userEmail\nThe user's email address is a@example.invalid.\n"
                                 '</system-reminder>\n'}
ENV = {'role': 'system', 'content': [{'type': 'text', 'text': 'Fixture environment'}]}


def test_native_is_told_not_to_load_its_memory_or_claude_md(profile, tmp_path):
    # "0" forces auto memory on in Claude Code; Hermes' own setting must still win.
    env = _turn_env(profile, tmp_path, CLAUDE_CODE_DISABLE_AUTO_MEMORY='0')
    assert env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'] == '1'
    assert env['CLAUDE_CODE_DISABLE_CLAUDE_MDS'] == '1'


@pytest.fixture
def relay():
    received = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()

    def send(messages, queried):
        gate = Admission(f'http://127.0.0.1:{peer.server_port}', 5, queried=queried)
        conn = http.client.HTTPConnection('127.0.0.1', gate.server.server_port, timeout=5)
        try:
            conn.request('POST', gate.prefix + '/v1/messages', json.dumps({'model': 'm', 'messages': messages}),
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            assert response.status == 200
            response.read()
            assert gate.failure is None
            return json.loads(received[-1])['messages']
        finally:
            conn.close()
            gate.close()

    try:
        yield send
    finally:
        peer.shutdown()
        thread.join()
        peer.server_close()


def opening(marked):
    """Native's opening request (Claude Code 2.1.310, Opus 5.5 layout): its context ahead of Hermes' frame."""
    turn = copy.deepcopy([MEMORY, EMAIL, FRAME])
    env = copy.deepcopy(ENV)
    if marked == 'context':
        turn[1]['cache_control'] = MARK
    elif marked == 'environment':
        env['content'][0]['cache_control'] = MARK
    return [{'role': 'user', 'content': turn}, env]


@pytest.mark.parametrize('marked', ['environment', 'context', 'none'])
def test_first_turn_reaches_claude_without_native_memory_or_email(relay, marked):
    out = relay(opening(marked), [FRAME])
    sent = json.dumps(out)
    assert 'fixture project note' not in sent and 'a@example.invalid' not in sent
    assert [{k: v for k, v in b.items() if k != 'cache_control'} for b in out[0]['content']] == [FRAME]
    marks = sum('cache_control' in b for m in out for b in m['content'])
    assert marks == (0 if marked == 'none' else 1)
