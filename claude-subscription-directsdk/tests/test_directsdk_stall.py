"""Issue #35: the stall watchdog kills a ping-fed stall and defers to real progress."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

USAGE = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0}

NATIVE = r'''
import json, os, sys, time, urllib.request, urllib.error
mode = sys.argv[1]
for line in sys.stdin:
    frame = json.loads(line)
    if frame.get('shouldQuery') is False:
        print(json.dumps({'type': 'result', 'num_turns': 0}), flush=True)
        continue
    break
if mode == 'stall':
    ping = json.dumps({'type': 'stream_event', 'event': {'type': 'ping'}})
    while True:
        print(ping, flush=True)
        time.sleep(0.05)
elif mode == 'omitted_think':
    # Thinking block with display "omitted": only pings during the block, then completion.
    print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}}}), flush=True)
    for _ in range(12):
        print(json.dumps({'type': 'stream_event', 'event': {'type': 'ping'}}), flush=True)
        time.sleep(0.5)
    print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_stop', 'index': 0}}), flush=True)
    print(json.dumps({'type': 'assistant', 'message': {'id': 'first', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'FIRST'}]}}))
    print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}))
    print(json.dumps({'type': 'result', 'subtype': 'success', 'usage': {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0}}))
else:
    url = os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages'
    for _ in range(2):
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json'}), timeout=5).read()
        except urllib.error.HTTPError:
            break
    pieces = {'progress': ['F', 'I', 'R', 'S', 'T'], 'ping_then_done': []}[mode]
    for piece in pieces:
        if mode == 'progress':
            print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': piece}}}), flush=True)
        else:
            print(json.dumps({'type': 'stream_event', 'event': {'type': 'ping'}}), flush=True)
        time.sleep(0.5)
    print(json.dumps({'type': 'assistant', 'message': {'id': 'first', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'FIRST'}]}}))
    print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}))
    print(json.dumps({'type': 'result', 'subtype': 'success', 'usage': {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0}}))
'''


class Peer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        events = [
            {'type': 'message_start', 'message': {'id': 'first', 'role': 'assistant', 'model': 'sonnet', 'content': [], 'usage': USAGE}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'FIRST'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': USAGE},
            {'type': 'message_stop'},
        ]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode())


def test_ping_fed_stall_is_killed_on_progress_silence(tmp_path, monkeypatch):
    monkeypatch.setattr(directsdk, 'stall_timeout_seconds', lambda: 1)
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    client = directsdk.Client(
        command=[sys.executable, str(native), 'stall'],
        env={'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9'},
        timeout=25,
    )
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match='stalled: no model progress'):
            client.create(model='sonnet', messages=[{'role': 'user', 'content': 'fixture'}])
    finally:
        client.close()
    assert time.monotonic() - started < 15, 'the stall watchdog did not fire early'


def test_progress_events_defer_the_stall_kill(tmp_path, monkeypatch):
    monkeypatch.setattr(directsdk, 'stall_timeout_seconds', lambda: 1)
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    client = directsdk.Client(
        command=[sys.executable, str(native), 'progress'],
        env={'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}'},
        timeout=25,
    )
    try:
        result = client.create(model='sonnet', messages=[{'role': 'user', 'content': 'fixture'}])
        assert result.choices[0].message.content == 'FIRST'
    finally:
        client.close()
        peer.shutdown()
        thread.join()
        peer.server_close()


def test_zero_disables_the_stall_watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(directsdk, 'stall_timeout_seconds', lambda: 0)
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    client = directsdk.Client(
        command=[sys.executable, str(native), 'ping_then_done'],
        env={'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}'},
        timeout=25,
    )
    try:
        result = client.create(model='sonnet', messages=[{'role': 'user', 'content': 'fixture'}])
        assert result.choices[0].message.content == 'FIRST'
    finally:
        client.close()
        peer.shutdown()
        thread.join()
        peer.server_close()

