"""The opt-in skills nudge restates the skills rule on the final frame (#41)."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admission
import directsdk
import directsdk_setup

NATIVE = r'''
import json, os, sys, urllib.request
for line in sys.stdin:
    frame=json.loads(line)
    if frame.get('shouldQuery') is False:
        print(json.dumps({'type':'result','num_turns':0}),flush=True)
        continue
    break
body={'model':'sonnet','messages':[{'role':'user','content':'fixture'}]}
urllib.request.urlopen(urllib.request.Request(os.environ['ANTHROPIC_BASE_URL']+'/v1/messages',data=json.dumps(body).encode(),
    headers={'Content-Type':'application/json'}),timeout=5).read()
print(json.dumps({'type':'assistant','message':{'id':'first','role':'assistant','content':[{'type':'text','text':'FIRST'}]}}))
print(json.dumps({'type':'stream_event','event':{'type':'message_stop'}}))
print(json.dumps({'type':'result','subtype':'success','usage':{'input_tokens':0,'output_tokens':0}}))
'''


def _payload(content):
    return json.dumps({'model': 'sonnet', 'messages': [{'role': 'user', 'content': content}]}).encode()


def test_a_string_content_gains_its_text_then_the_reminder():
    out = json.loads(admission.nudge_skills(_payload('hello')))
    content = out['messages'][-1]['content']
    assert content[0] == {'type': 'text', 'text': 'hello'}
    assert content[1]['text'] == admission.SKILLS_REMINDER


def test_a_block_list_appends_the_reminder():
    out = json.loads(admission.nudge_skills(_payload([{'type': 'text', 'text': 'hello'}])))
    assert len(out['messages'][-1]['content']) == 2
    assert out['messages'][-1]['content'][1]['text'] == admission.SKILLS_REMINDER


def test_unknown_shapes_and_garbage_are_untouched():
    assert admission.nudge_skills(b'not json') == b'not json'
    odd = json.dumps({'messages': [{'role': 'user', 'content': 7}]}).encode()
    assert admission.nudge_skills(odd) == odd
    empty = json.dumps({'messages': []}).encode()
    assert admission.nudge_skills(empty) == empty


def _fake_turn(tmp_path, monkeypatch, nudged):
    captured = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            usage = {'input_tokens': 0, 'output_tokens': 0}
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
    monkeypatch.setattr(directsdk_setup, 'skills_nudge_enabled', lambda: nudged)
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    env = {'PATH': os.defpath, 'HOME': str(tmp_path),
           'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}'}
    client = directsdk.Client(command=[sys.executable, str(native)], env=env)
    try:
        result = client.create(model='sonnet', messages=[{'role': 'user', 'content': 'fixture'}])
        assert result.choices[0].message.content == 'FIRST'
    finally:
        client.close(); peer.shutdown(); thread.join(timeout=3); peer.server_close()
    return captured[0]


def test_the_setting_puts_the_reminder_on_the_wire(tmp_path, monkeypatch):
    body = _fake_turn(tmp_path, monkeypatch, nudged=True)
    content = body['messages'][-1]['content']
    assert content[-1]['text'] == admission.SKILLS_REMINDER
    assert body['messages'][-1]['role'] == 'user'


def test_off_keeps_the_wire_byte_shape(tmp_path, monkeypatch):
    body = _fake_turn(tmp_path, monkeypatch, nudged=False)
    assert all('Reminder' not in json.dumps(b) for b in body['messages'][-1]['content']
               if isinstance(b, dict) and b.get('type') == 'text')
