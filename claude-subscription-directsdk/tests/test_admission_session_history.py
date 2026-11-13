"""A kept-alive native's history must line up with Hermes' frames before its request goes upstream.

Through the real relay; all traffic is loopback, no CLI. ``canonical_history`` rewrites the tool results native
holds in its own shape (text-block lists from MCP replies) to Hermes' bytes, by position. When the rows do not
line up (native holds a row Hermes does not, or a role is out of place) nothing can be rewritten, so the request
is refused before upstream and the step replays into a new process, as a request that does not extend the previous
one is. Before, it forwarded with native's shape in the replayed rows, and ``extends_previous`` passed it on the
stable prefix. A fresh native's first request is the replay itself and forwards as built, so a shape the relay
cannot read never loops a relaunch.
"""
import copy
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from admission import Admission, HistoryMisaligned, canonical_history

ENV = {'role': 'system', 'content': 'Fixture environment'}
DIVERGED = b'HERMES_SESSION_DIVERGED'


def frame(role, *blocks):
    return {'type': role, 'message': {'role': role, 'content': list(blocks)}}


def call(n):
    return frame('assistant', {'type': 'text', 'text': f'reading {n}'},
                 {'type': 'tool_use', 'id': f'toolu_{n:04d}', 'name': 'mcp__hermes__read_file', 'input': {'path': f'f{n}'}})


def result(n):
    return frame('user', {'type': 'tool_result', 'tool_use_id': f'toolu_{n:04d}', 'content': f'contents of f{n}'})


def native_result(n):
    """The same result as native holds it: content as a text-block list, built from the MCP reply."""
    return {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f'toolu_{n:04d}',
                                         'content': [{'type': 'text', 'text': f'contents of f{n}'}]}]}


OPEN = frame('user', {'type': 'text', 'text': 'go'})
FRAMES = [OPEN, call(1), result(1), call(2), result(2)]


def wire(messages, model='fixture'):
    return json.dumps({'model': model, 'messages': copy.deepcopy(messages)}, ensure_ascii=False, separators=(',', ':')).encode()


def native_rows(frames):
    """Native's request body for ``frames``: its environment row first, Hermes' rows as it holds them."""
    rows = [ENV]
    for f in frames:
        blocks = f['message']['content']
        rows.append(native_result(int(blocks[0]['tool_use_id'][-4:])) if blocks[0].get('type') == 'tool_result' else copy.deepcopy(f['message']))
    return rows


@pytest.fixture
def peer():
    received = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    server.received = received
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def post(gate, messages, model='fixture'):
    conn = http.client.HTTPConnection('127.0.0.1', gate.server.server_port, timeout=5)
    try:
        conn.request('POST', gate.prefix + '/v1/messages', wire(messages, model), {'Content-Type': 'application/json'})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def kept(peer, frames):
    """A relay serving a kept native that has forwarded every step of ``frames`` but the last, rows aligned."""
    gate = Admission(f'http://127.0.0.1:{peer.server_port}', 5, queried=frames[0]['message']['content'])
    gate.frames = frames[:1]
    assert post(gate, native_rows(frames[:1]))[0] == 200  # the launch's own request: the replay
    for n in range(3, len(frames), 2):
        gate.rearm(5, frames[n - 1]['message']['content'], frames[:n])
        assert post(gate, native_rows(frames[:n]))[0] == 200
    gate.rearm(5, frames[-1]['message']['content'], frames)
    return gate


def test_aligned_rows_are_rewritten_to_hermes_bytes_and_forwarded(peer):
    gate = kept(peer, FRAMES)
    try:
        status, _ = post(gate, native_rows(FRAMES))
        assert status == 200 and not gate.diverged and gate.failure is None
        sent = peer.received[-1]['messages']
        assert [m['role'] for m in sent] == ['system', 'user', 'assistant', 'user', 'assistant', 'user']
        # The replayed result (row 3) and the queried one (row 5) both carry Hermes' string content, not native's list.
        assert sent[3]['content'] == result(1)['message']['content']
        assert sent[5]['content'] == result(2)['message']['content']
    finally:
        gate.close()


@pytest.mark.parametrize('misalign', ['extra_row', 'role'])
def test_a_misaligned_tail_is_refused_before_upstream(peer, misalign):
    """The first resumed step: the forwarded prefix is the opening turn alone, so ``extends_previous`` has no replayed
    result there to catch a tail that does not line up. Native's new rows drift from Hermes' frames (a row Hermes
    never saw, or a user row where Hermes holds the call): refused as diverged, never forwarded."""
    gate = kept(peer, FRAMES[:3])
    forwarded = len(peer.received)
    try:
        rows = native_rows(FRAMES[:3])
        if misalign == 'extra_row':
            rows.insert(3, {'role': 'user', 'content': [{'type': 'text', 'text': 'a row Hermes never saw'}]})
        else:
            rows[2] = {'role': 'user', 'content': [{'type': 'text', 'text': 'a user row where Hermes holds a call'}]}
        status, body = post(gate, rows)
        assert status == 400 and DIVERGED in body
        assert gate.diverged and len(peer.received) == forwarded
    finally:
        gate.close()


def test_a_misaligned_prefix_is_refused_as_not_extending_the_previous_request(peer):
    """Later steps: a replayed result already sits in the forwarded prefix in Hermes' bytes, so a request whose rows
    shifted there fails ``extends_previous`` whatever ``canonical_history`` does. Both gates refuse the same way."""
    gate = kept(peer, FRAMES)
    forwarded = len(peer.received)
    try:
        rows = native_rows(FRAMES)
        rows.insert(3, {'role': 'assistant', 'content': [{'type': 'text', 'text': 'a row Hermes never saw'}]})
        status, body = post(gate, rows)
        assert status == 400 and DIVERGED in body
        assert gate.diverged and len(peer.received) == forwarded
    finally:
        gate.close()


def test_a_changed_model_on_aligned_rows_is_refused_as_not_extending_the_previous_request(peer):
    """Every row lines up, so the extension check alone stands before upstream: the kept native asks for another
    model than its previous request did. ``test_admission_session_drift`` drifts the model against no frames at all,
    which the alignment refusal now answers first; this case keeps the extension check itself under test."""
    gate = kept(peer, FRAMES)
    forwarded = len(peer.received)
    try:
        status, body = post(gate, native_rows(FRAMES), model='another')
        assert status == 400 and DIVERGED in body
        assert gate.diverged and len(peer.received) == forwarded
    finally:
        gate.close()


def test_a_fresh_natives_first_request_forwards_as_built(peer):
    """No request has been forwarded on this relay yet: the launch's replay forwards as native built it, as per-call
    does, so a shape the relay cannot read costs an uncached row, never a relaunch that repeats itself."""
    gate = Admission(f'http://127.0.0.1:{peer.server_port}', 5, queried=FRAMES[-1]['message']['content'])
    gate.frames = FRAMES
    try:
        rows = native_rows(FRAMES)
        rows.insert(3, {'role': 'assistant', 'content': [{'type': 'text', 'text': 'a row Hermes never saw'}]})
        status, _ = post(gate, rows)
        assert status == 200 and not gate.diverged and len(peer.received) == 1
    finally:
        gate.close()


@pytest.mark.parametrize('payload', [
    wire(native_rows(FRAMES)[:-1]),  # one row fewer
    wire(native_rows(FRAMES) + [{'role': 'user', 'content': [{'type': 'text', 'text': 'one more'}]}]),
    wire([ENV, {'role': 'assistant', 'content': [{'type': 'text', 'text': 'x'}]}] + native_rows(FRAMES)[2:]),  # role out of place
    b'not json', b'{"messages": null}', b'{"messages": [null]}', b'{}'])
def test_canonical_history_names_what_does_not_line_up(payload):
    with pytest.raises(HistoryMisaligned):
        canonical_history(payload, FRAMES)


def test_canonical_history_leaves_an_aligned_body_alone_but_for_the_results():
    out = json.loads(canonical_history(wire(native_rows(FRAMES)), FRAMES))['messages']
    assert out[0] == ENV and out[1] == OPEN['message'] and out[2] == call(1)['message'] and out[4] == call(2)['message']
    assert out[3] == result(1)['message'] and out[5] == result(2)['message']
