"""Tool calls through the real relay: streamed as they are written, and never run when their arguments are broken."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
from collections import OrderedDict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

# Forwards one request through the relay and re-emits the upstream SSE as native's stream events, as Claude Code does.
NATIVE = r'''
import json, os, pathlib, sys, urllib.request
for line in sys.stdin:
    if json.loads(line).get('shouldQuery', True):
        break
    print(json.dumps({'type': 'result', 'num_turns': 0}), flush=True)
settings = json.loads(pathlib.Path(sys.argv[sys.argv.index('--settings') + 1]).read_text())
body = json.loads(settings['env']['CLAUDE_CODE_EXTRA_BODY'])
body.update(model='sonnet', stream=True, messages=[{'role': 'user', 'content': 'fixture'}])
request = urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
tool = False
for raw in urllib.request.urlopen(request, timeout=5):
    raw = raw.decode().strip()
    if raw.startswith('data:'):
        event = json.loads(raw[5:])
        tool = tool or (event.get('content_block') or {}).get('type') == 'tool_use'
        print(json.dumps({'type': 'stream_event', 'event': event}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'error_max_turns' if tool else 'success', 'is_error': tool,
                  'usage': {'input_tokens': 1, 'output_tokens': 1}}), flush=True)
sys.exit(1 if tool else 0)
'''

USAGE = {'input_tokens': 3, 'output_tokens': 2}
TOOLS = [{'type': 'function', 'function': {'name': 'read_file', 'description': 'Read',
          'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}]
REQUEST = {'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'read a.txt'}], 'tools': TOOLS}


def reply(fragments, stop, details=None):
    """One assistant turn: a short text, then a read_file call whose arguments stream as ``fragments``."""
    delta = {'stop_reason': stop, **({'stop_details': details} if details else {})}
    return [{'type': 'message_start', 'message': {'id': 'msg_1', 'role': 'assistant', 'content': [], 'usage': USAGE}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Reading.'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'toolu_1', 'name': 'mcp__hermes__read_file', 'input': {}}},
            *({'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': f}} for f in fragments),
            {'type': 'content_block_stop', 'index': 1},
            {'type': 'message_delta', 'delta': delta, 'usage': USAGE},
            {'type': 'message_stop'}]


@pytest.fixture(autouse=True)
def fresh_conversations(monkeypatch):
    # Every test reuses one conversation; a buffered one from an earlier test must not carry over.
    monkeypatch.setattr(directsdk, '_BUFFER_TOOL_INPUT', OrderedDict(), raising=False)


@contextmanager
def upstream(tmp_path, *replies):
    bodies = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            events = replies[min(len(bodies), len(replies)) - 1]
            self.wfile.write(''.join(f'data: {json.dumps(e)}\n\n' for e in events).encode())

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    script = tmp_path / 'native.py'
    script.write_text(NATIVE)
    client = directsdk.Client(command=[sys.executable, str(script)], env={
        'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}'})
    try:
        yield client, bodies
    finally:
        client.close()
        peer.shutdown()
        peer.server_close()


def eager(body):
    return [tool.get('eager_input_streaming') for tool in body['tools']]


def test_invalid_tool_json_returns_length_and_buffers_the_next_request(tmp_path):
    # #221: the API streamed a complete but invalid argument (stop tool_use). The call must not run and must not vanish.
    invalid = reply(['{"path": /tmp/a.txt}'], 'tool_use')
    with upstream(tmp_path, invalid, reply(['{"path": "/tmp/a.txt"}'], 'tool_use')) as (client, bodies):
        result = client.create(**REQUEST)
        retry = client.create(**REQUEST)
    assert result.choices[0].finish_reason == 'length'
    assert [(c.id, c.function.name, c.function.arguments) for c in result.choices[0].message.tool_calls] == [
        ('toolu_1', 'read_file', '{"path": /tmp/a.txt}')]
    assert retry.choices[0].finish_reason == 'tool_calls'
    # The re-sent request asks the API to buffer and validate the arguments.
    assert eager(bodies[0]) == [True] and eager(bodies[1]) == [None]


def test_cut_tool_call_keeps_streaming_tool_input(tmp_path):
    with upstream(tmp_path, reply(['{"path": "/tm'], 'max_tokens'), reply(['{"path": "/tmp/a.txt"}'], 'tool_use')) as (client, bodies):
        result = client.create(**REQUEST)
        client.create(**REQUEST)
    assert result.choices[0].finish_reason == 'length'
    assert result.choices[0].message.tool_calls[0].function.arguments == '{"path": "/tm'
    assert eager(bodies[1]) == [True]


def tool_deltas(chunks):
    return [tc for c in chunks for tc in (c.choices[0].delta.tool_calls or [])]


def test_tool_header_streams_before_its_arguments(tmp_path):
    # #326: Hermes shows the tool being prepared from its first named delta; the arguments follow as they are written.
    with upstream(tmp_path, reply(['{"path": ', '"/tmp/a.txt"}'], 'tool_use')) as (client, _):
        chunks = list(client.create(**REQUEST, stream=True))
    deltas = tool_deltas(chunks)
    first = deltas[0].model_dump()
    assert (first['id'], first['function']) == ('toolu_1', {'name': 'read_file', 'arguments': ''})
    assert [d.function.arguments for d in deltas[1:]] == ['{"path": ', '"/tmp/a.txt"}']
    assert chunks[-1].choices[0].finish_reason == 'tool_calls'


def test_refusal_mid_call_leaves_streamed_arguments_that_parse(tmp_path):
    # Hermes cannot take back a streamed fragment. One it cannot repair turns content_filter into a truncation it
    # retries; a refusal must stay one terminal request.
    details = {'type': 'refusal', 'category': 'cyber', 'explanation': 'Flagged'}
    with upstream(tmp_path, reply(['{"path": "/tm'], 'refusal', details)) as (client, bodies):
        chunks = list(client.create(**REQUEST, stream=True))
    arguments = ''.join(d.function.arguments or '' for d in tool_deltas(chunks))
    assert json.loads(arguments) == {'path': '/tm'}
    assert chunks[-1].choices[0].finish_reason == 'content_filter' and len(bodies) == 1


def test_json_suffix_closes_every_prefix_of_a_tool_argument():
    text = json.dumps({'path': 'a\\"b\u00e9', 'n': [1, -2.5e3, True, False, None, {}], 'k': {'x': ''}})
    for cut in range(1, len(text)):
        suffix = directsdk._json_suffix(text[:cut])
        assert suffix is not None, text[:cut]
        json.loads(text[:cut] + suffix)
