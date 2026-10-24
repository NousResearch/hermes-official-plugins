"""Login health is recorded in the credential pool, keyed by login dir and status only (#37)."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk
import login_health

NATIVE = r'''
import json, os, sys, urllib.request, urllib.error
for line in sys.stdin:
    frame=json.loads(line)
    if frame.get('shouldQuery') is False:
        print(json.dumps({'type':'result','num_turns':0}),flush=True)
        continue
    break
try:
    urllib.request.urlopen(urllib.request.Request(os.environ['ANTHROPIC_BASE_URL']+'/v1/messages',data=b'{}',
        headers={'Content-Type':'application/json'}),timeout=5).read()
except urllib.error.HTTPError:
    pass
print(json.dumps({'type':'assistant','message':{'id':'first','role':'assistant','content':[{'type':'text','text':'FIRST'}]}}))
print(json.dumps({'type':'stream_event','event':{'type':'message_stop'}}))
print(json.dumps({'type':'result','subtype':'success','usage':{'input_tokens':0,'output_tokens':0}}))
'''


def _spy(monkeypatch):
    calls = []

    def write(provider, entries, **_kwargs):
        calls.append((provider, entries))
        return entries

    import hermes_cli.auth as auth
    monkeypatch.setattr(auth, 'write_credential_pool', write)
    return calls


def test_record_writes_a_status_only_entry_keyed_by_the_login_dir(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    login_health.record({'CLAUDE_CONFIG_DIR': str(tmp_path)}, True)
    provider, entries = calls[0]
    assert provider == 'claude-subscription-directsdk'
    assert entries[0]['id'] == 'login:' + str(tmp_path)
    assert entries[0]['last_status'] == 'ok' and entries[0]['last_status_at']
    assert 'access_token' not in entries[0] and 'refresh_token' not in entries[0]


def test_login_key_falls_back_to_the_cli_default(monkeypatch, tmp_path):
    login_health_home = tmp_path / 'home'
    login_health_home.mkdir()
    monkeypatch.setenv('HOME', str(login_health_home))
    monkeypatch.setenv('USERPROFILE', str(login_health_home))
    assert login_health.login_key({'CLAUDE_CONFIG_DIR': '/x/claude'}) == str(Path('/x/claude'))
    assert login_health.login_key({}) == str(Path.home() / '.claude')


def test_record_never_fails_the_turn(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError('store unavailable')

    import hermes_cli.auth as auth
    monkeypatch.setattr(auth, 'write_credential_pool', boom)
    login_health.record({}, True)


def _peer(reply):
    usage = {'input_tokens': 0, 'output_tokens': 0}

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            if reply == 401:
                body = json.dumps({'type': 'error', 'error': {'type': 'authentication_error',
                                                              'message': 'OAuth access token has been revoked.'}}).encode()
                self.send_response(401)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            events = [
                {'type': 'message_start', 'message': {'id': 'first', 'role': 'assistant', 'model': 'sonnet', 'content': [], 'usage': usage}},
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'FIRST'}},
                {'type': 'content_block_stop', 'index': 0},
                {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': usage},
                {'type': 'message_stop'},
            ]
            self.wfile.write(''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode())

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    return peer, thread


def test_a_successful_turn_marks_the_login_ok(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    peer, thread = _peer(200)
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'CLAUDE_CONFIG_DIR': str(tmp_path / 'claude'),
           'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}'}
    client = directsdk.Client(command=[sys.executable, str(native)], env=env)
    try:
        client.create(model='sonnet', messages=[{'role': 'user', 'content': 'fixture'}])
    finally:
        client.close(); peer.shutdown(); thread.join(timeout=3); peer.server_close()
    assert calls[-1][0] == 'claude-subscription-directsdk'
    assert calls[-1][1][0]['last_status'] == 'ok'
    assert calls[-1][1][0]['id'] == 'login:' + str(tmp_path / 'claude')


def test_an_upstream_401_marks_the_login_dead(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)
    peer, thread = _peer(401)
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    env = {'PATH': os.defpath, 'HOME': str(tmp_path),
           'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}'}
    client = directsdk.Client(command=[sys.executable, str(native)], env=env)
    try:
        with __import__('pytest').raises(directsdk.ClaudeAPIError):
            client.create(model='sonnet', messages=[{'role': 'user', 'content': 'fixture'}])
    finally:
        client.close(); peer.shutdown(); thread.join(timeout=3); peer.server_close()
    assert calls[-1][1][0]['last_status'] == 'dead'
