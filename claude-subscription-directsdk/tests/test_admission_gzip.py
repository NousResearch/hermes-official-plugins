"""Request gzip and rejection fallback through the real loopback relay (no credentials)."""
from email.message import Message
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import zlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admission

PAYLOAD = json.dumps({'messages': [{'role': 'user', 'content': 'fixture λ ' * 600}]}).encode()
CA_ENV = ('SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS')


def error(message):
    return json.dumps({'type': 'error', 'error': {'type': 'invalid_request_error', 'message': message}}).encode()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (*CA_ENV, 'CLAUDE_CODE_GZIP_REQUEST_BODIES'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(admission, '_gzip_latched', None)


@pytest.fixture
def relay(monkeypatch):
    calls, replies, gates = [], [], []
    original = http.client.HTTPConnection

    class Peer(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            calls.append((self.headers, body, self.client_address, self.path))
            reply = replies[min(len(calls) - 1, len(replies) - 1)]
            if callable(reply):
                reply(self)
                return
            status, headers, body = reply
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    monkeypatch.setattr(admission, 'GZIP_HOSTS', ('127.0.0.1',))

    def connect(host, port=None, **kwargs):
        # Exercise the production host/port gate, with a real HTTP peer on an ephemeral port.
        return original('127.0.0.1', peer.server_port, **kwargs)

    monkeypatch.setattr(admission.http.client, 'HTTPConnection', connect)

    def post(payload=PAYLOAD, headers=None, port=None, configure=None):
        gate = admission.Admission('http://127.0.0.1', 3)
        gates.append(gate)
        if port is not None:
            gate.upstream = urlsplit(f'http://127.0.0.1:{port}')
        if configure:
            configure(gate)
        client = original('127.0.0.1', gate.server.server_port, timeout=4)
        try:
            client.request('POST', gate.prefix + '/v1/messages?beta=true', payload,
                           {'Content-Type': 'application/json', **(headers or {})})
            response = client.getresponse()
            return response.status, response.read(), gate
        finally:
            client.close()

    try:
        yield SimpleNamespace(post=post, calls=calls, replies=replies, gates=gates, client=original, peer_type=Peer)
    finally:
        for gate in gates:
            gate.close()
        peer.shutdown()
        thread.join()
        peer.server_close()


@pytest.mark.parametrize('url,proxy,env,latched,encoding,size,expected', [
    ('https://api.anthropic.com', None, {}, None, None, 4096, True),
    ('https://api.anthropic.com:443', None, {}, None, None, 4096, True),
    ('https://api.anthropic.com:444', None, {}, None, None, 4096, False),
    ('https://api.anthropic.com.evil.test', None, {}, None, None, 4096, False),
    ('https://other.test', None, {'CLAUDE_CODE_GZIP_REQUEST_BODIES': '1'}, None, None, 4096, False),
    ('https://api.anthropic.com', ('proxy', 80, None), {}, None, None, 4096, False),
    ('https://api.anthropic.com', ('proxy', 80, None), {'CLAUDE_CODE_GZIP_REQUEST_BODIES': '1'}, None, None, 4096, False),
    ('https://api.anthropic.com', None, {}, 'rejected', None, 4096, False),
    ('https://api.anthropic.com', None, {}, None, 'identity', 4096, False),
    ('https://api.anthropic.com', None, {}, None, None, 4095, False),
    *[('https://api.anthropic.com', None, {key: '/fixture'}, None, None, 4096, False) for key in CA_ENV],
    ('https://api.anthropic.com', None, {'SSL_CERT_FILE': ''}, None, None, 4096, False),
    *[('https://api.anthropic.com', None, {'CLAUDE_CODE_GZIP_REQUEST_BODIES': val}, None, None, 4096, False)
      for val in ('0', 'FaLsE', 'NO', 'off')],
])
def test_eligibility(url, proxy, env, latched, encoding, size, expected, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(admission, '_gzip_latched', latched)
    headers = Message()
    if encoding:
        headers['cOnTeNt-EnCoDiNg'] = encoding
    gate = SimpleNamespace(upstream=urlsplit(url), proxy=proxy, one_shot=True)
    assert admission._gzip_eligible(gate, b'x' * size, headers) is expected


@pytest.mark.parametrize('size,port,encoding,latched,compressed', [
    (4095, None, None, None, False), (4096, None, None, None, True),
    (4096, 443, None, None, True), (4096, 444, None, None, False),
    (4096, None, 'identity', None, False), (4096, None, None, 'rejected', False),
])
def test_wire_gate(relay, monkeypatch, size, port, encoding, latched, compressed):
    monkeypatch.setattr(admission, '_gzip_latched', latched)
    payload = b'{"text":"' + b'x' * (size - 11) + b'"}'
    headers = {'cOnTeNt-EnCoDiNg': encoding} if encoding else {}
    relay.replies.append((200, {}, b''))
    status, body, gate = relay.post(payload, headers, port, lambda gate: setattr(gate, 'one_shot', True))
    sent_headers, sent, _, route = relay.calls[0]
    assert status == 200 and body == b'' and gate.failure is None
    assert route == '/v1/messages?beta=true'
    assert int(sent_headers['Content-Length']) == len(sent)
    assert len(sent_headers.get_all('Content-Length')) == 1 and sent_headers['Accept-Encoding'] == 'identity'
    if compressed:
        assert sent_headers['Content-Encoding'] == 'gzip'
        assert sent_headers.get_all('Content-Encoding') == ['gzip']
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(sent) + decoder.flush()
        assert decoder.eof and decoder.unused_data == b''  # Exactly one gzip member.
        assert decoded[:len(payload)] == payload and decoded[len(payload):].startswith(b'\n')
        pad = decoded[len(payload) + 1:]
        assert 0 <= len(pad) <= 256 and set(pad) <= set(b' \t')
        assert json.loads(decoded) == json.loads(payload)
        assert sent[4:8] == b'\0' * 4  # mtime=0
    else:
        assert sent == payload and sent_headers.get('Content-Encoding') == encoding


@pytest.mark.parametrize('pad_size', [0, 256])
def test_padding_endpoints_and_level(relay, monkeypatch, pad_size):
    original, levels = gzip.compress, []
    def compress(data, *, compresslevel, mtime):
        levels.append((compresslevel, mtime))
        return original(data, compresslevel=compresslevel, mtime=mtime)
    monkeypatch.setattr(admission.gzip, 'compress', compress)
    monkeypatch.setattr(admission.secrets, 'randbelow', lambda upper: pad_size)
    relay.replies.append((200, {}, b''))
    relay.post()
    decoded = gzip.decompress(relay.calls[0][1])
    assert decoded.startswith(PAYLOAD + b'\n') and set(decoded[len(PAYLOAD) + 1:]) <= set(b' \t')
    assert len(decoded) == len(PAYLOAD) + 1 + pad_size
    assert levels == [(3, 0)]


REJECTIONS = [
    *[(status, {}, b'not JSON') for status in (400, 403, 415)],
    (400, {'Request-ID': 'other'}, b'not JSON'),
    (415, {'rEqUeSt-Id': 'req_fixture'}, b'unsupported'),
    (415, {'CF-Ray': ''}, b'unsupported'),
    (400, {'request-id': 'req_fixture'}, error('Unsupported Content-Encoding: gzip')),
    (400, {'cf-ray': 'fixture'}, b'{"error":{"details":{"error_code":"request_body_encoding_unsupported"}}}'),
    *[(400, {'REQUEST-ID': 'req_fixture'}, error(message)) for message in (
        'The request body is not valid JSON: fixture',
        'Failed to parse request: could not parse request body as JSON: fixture')],
    *[(400, {'cF-rAy': 'fixture'}, body) for go in (
        'bad json: invalid character \'x\' looking for beginning of value', 'bad json: unexpected EOF',
        'bad json: unexpected end of JSON input', 'bad json: EOF') for body in (
            (go + ' \n').encode(), json.dumps({'reason': 'bad_json', 'error': go}).encode())],
    (400, {'cf-ray': 'fixture'}, 'bad json: EOF\ufeff'.encode()),  # JavaScript trimEnd() removes U+FEFF
]


@pytest.mark.parametrize('first', REJECTIONS)
def test_rejection_resends_original_once_and_latches(relay, first, caplog):
    # The raw resend is itself a rejection: it must be forwarded, never retried again.
    relay.replies.extend([first, (415, {'Request-ID': 'req_final'}, b'final rejection')])
    status, body, gate = relay.post(headers={'X-Fixture': 'preserved', 'Accept-Encoding': 'br'})
    assert status == 415 and body == gate.error_body == b'final rejection'
    assert gate.status == 415 and gate.request_id == 'req_final' and gate.failure is None
    assert len(relay.calls) == 2
    first_headers, compressed, first_addr, _ = relay.calls[0]
    raw_headers, raw, raw_addr, _ = relay.calls[1]
    assert first_headers['Content-Encoding'] == 'gzip' and gzip.decompress(compressed).startswith(PAYLOAD + b'\n')
    assert raw == PAYLOAD and 'Content-Encoding' not in raw_headers and first_addr != raw_addr
    assert int(raw_headers['Content-Length']) == len(PAYLOAD)
    assert raw_headers['X-Fixture'] == 'preserved' and raw_headers['Accept-Encoding'] == 'identity'
    assert admission._gzip_latched is not None
    relay.post()  # A new Admission still sees the process-wide latch.
    assert len(relay.calls) == 3 and relay.calls[2][1] == PAYLOAD
    warnings = [r for r in caplog.records if 'request gzip disabled' in r.message]
    assert len(warnings) == 1 and warnings[0].levelname == 'WARNING'
    assert 'status=' in warnings[0].message and 'final rejection' not in warnings[0].message



def test_failed_raw_resend_does_not_report_the_rejected_answer(relay):
    relay.replies.extend([(415, {'Request-ID': 'req_rejected'}, b'rejected'),
                          lambda peer: setattr(peer, 'close_connection', True)])  # resend: no answer at all
    with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError)):
        relay.post()
    gate = relay.gates[-1]
    assert len(relay.calls) == 2 and gate.failure and gate.status is None and gate.request_id is None

NOT_REJECTIONS = [
    *[(status, {}, error('The request body is not valid JSON')) for status in (401, 429, 500)],
    (400, {'request-id': 'req_fixture'}, error('prompt is too long')),
    (400, {'CF-RAY': ''}, b'not JSON'),
    (403, {'request-id': 'req_fixture'}, error('The request body is not valid JSON')),
    (403, {'cf-ray': 'fixture'}, b'forbidden'),
    *[(400, {'request-id': 'req_fixture'}, body) for body in (
        b'[]', b'null', b'{"error":null}', b'{"error":{"message":12,"details":[]}}',
        b'The request body is not valid JSON', error('prefix The request body is not valid JSON'),
        b'prefix bad json: EOF', b'bad json: EOF extra', b'bad json: invalid characters',
        b'{"reason":"other","error":"bad json: EOF"}',
        b'{"reason":"bad_json","error":"prefix bad json: EOF"}',
        b' ' * 8192 + b'bad json: EOF',
        b' bad json: EOF',  # Claude Code trims only the end of a raw body
        b'\x0c' + error('The request body is not valid JSON'),  # not JSON whitespace: no parse
        'bad json: EOF\u0085'.encode(),  # JavaScript trimEnd() keeps U+0085
        b'{"reason":"bad_json","error":"bad json: EOF\\n"}',  # untrimmed; JavaScript $ is end of input
        b'{"error":{"message":"The request body is not valid JSON"}}',  # no Anthropic error envelope
        b'{"type":"error","error":{"type":1,"message":"The request body is not valid JSON"}}')],
]


@pytest.mark.parametrize('reply', NOT_REJECTIONS)
def test_other_errors_forward_without_resend(relay, reply):
    relay.replies.append(reply)
    status, body, gate = relay.post()
    assert status == gate.status == reply[0] and body == gate.error_body == reply[2]
    assert len(relay.calls) == 1 and admission._gzip_latched is None and gate.failure is None


def test_large_error_prefix_and_remainder_forwarded(relay):
    body = error('unrelated ' + 'x' * 100000)
    relay.replies.append((400, {'request-id': 'req_fixture'}, body))
    status, received, gate = relay.post()
    assert status == 400 and received == body and gate.error_body == body[:65536]
    assert len(relay.calls) == 1 and gate.failure is None


@pytest.mark.parametrize('headers,body', [
    ({'request-id': 'req_fixture'}, error('prompt is too long')),
    ({'request-id': 'req_fixture'}, b''),
    ({'Connection': 'close', 'cf-ray': 'fixture'}, b'not JSON'),
])
def test_fully_read_connection_close_error_is_forwarded(relay, headers, body):
    relay.peer_type.protocol_version = 'HTTP/1.0'
    relay.replies.append((400, headers, body))
    status, received, gate = relay.post()
    assert status == 400 and received == gate.error_body == body and gate.failure is None
    assert len(relay.calls) == 1


def test_partly_read_rejection_socket_closed_before_resend(relay):
    relay.peer_type.protocol_version = 'HTTP/1.0'
    closed = threading.Event()
    def reject(peer):
        peer.send_response(415)
        peer.send_header('Content-Length', '70000')
        peer.end_headers()
        peer.wfile.write(b'x' * 70000); peer.wfile.flush()
        try:
            if peer.rfile.read(1) == b'':
                closed.set()
        except ConnectionResetError:
            closed.set()
    def raw(peer):
        peer.send_response(200 if closed.wait(1) else 500)
        peer.send_header('Content-Length', '0')
        peer.end_headers()
    relay.replies.extend([reject, raw])
    status, body, gate = relay.post()
    assert status == 200 and body == b'' and gate.failure is None and closed.is_set()
    assert len(relay.calls) == 2


def test_ineligible_rejection_never_retries_or_latches(relay):
    relay.replies.append((415, {}, b'fixture'))
    assert relay.post(b'{}')[:2] == (415, b'fixture')
    assert len(relay.calls) == 1 and admission._gzip_latched is None


def test_compression_and_raw_resend_follow_all_payload_transforms(relay, monkeypatch):
    transforms = []
    def transform(name):
        def apply(payload, queried):
            transforms.append(name)
            return payload + b' '
        return apply
    for name in ('restore_queried_turn', 'stable_request_context', 'pin_message_breakpoint'):
        monkeypatch.setattr(admission, name, transform(name))
    relay.replies.extend([(415, {}, b'rejected'), (200, {}, b'')])
    relay.post()
    assert transforms == ['restore_queried_turn', 'stable_request_context', 'pin_message_breakpoint']
    original = PAYLOAD + b'   '
    assert gzip.decompress(relay.calls[0][1]).startswith(original + b'\n')
    assert relay.calls[1][1] == original


def test_rejection_body_read_obeys_request_timeout(relay):
    release = threading.Event()
    def stall(peer):
        peer.send_response(400)
        peer.send_header('Request-ID', 'req_fixture')
        peer.send_header('Content-Length', '100')
        peer.end_headers()
        peer.wfile.flush()
        release.wait(2)
    relay.replies.append(stall)
    try:
        with pytest.raises(http.client.RemoteDisconnected):
            relay.post(configure=lambda gate: setattr(gate, 'timeout', .1))
        assert relay.gates[-1].failure == 'TimeoutError' and len(relay.calls) == 1
        assert relay.gates[-1].status == 400 and relay.gates[-1].request_id == 'req_fixture'
        assert admission._gzip_latched is None
    finally:
        release.set()


def test_streaming_200_reaches_native_before_upstream_finishes(relay):
    release, started = threading.Event(), threading.Event()
    chunks = [b'data: {"type":"message_start","message":{"id":"fixture","content":[],"usage":{}}}\n\n',
              b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{}}\n\n'
              b'data: {"type":"message_stop"}\n\n']
    def stream(peer):
        peer.send_response(200)
        peer.send_header('Content-Type', 'text/event-stream')
        peer.send_header('Content-Length', str(sum(map(len, chunks))))
        peer.end_headers()
        peer.wfile.write(chunks[0]); peer.wfile.flush(); started.set()
        if release.wait(3):
            peer.wfile.write(chunks[1]); peer.wfile.flush()
    relay.replies.append(stream)
    gate = admission.Admission('http://127.0.0.1', 3)
    relay.gates.append(gate)
    client = relay.client('127.0.0.1', gate.server.server_port, timeout=2)
    try:
        client.request('POST', gate.prefix + '/v1/messages', PAYLOAD)
        response = client.getresponse()
        assert started.wait(1) and response.status == 200
        assert response.read(len(chunks[0])) == chunks[0]
        assert not release.is_set()
        release.set()
        assert response.read() == chunks[1]
    finally:
        release.set(); client.close()
    assert gate.capture.complete and gate.capture.message['id'] == 'fixture'
    assert len(relay.calls) == 1 and gate.failure is None


def test_cancellation_between_attempts_prevents_resend(relay, monkeypatch):
    original = admission._rejection_kind
    def reject(response, body):
        relay.gates[-1].abort()
        return original(response, body)
    monkeypatch.setattr(admission, '_rejection_kind', reject)
    relay.replies.append((415, {}, b'rejected'))
    with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError)):
        relay.post()
    gate, deadline = relay.gates[-1], time.monotonic() + 3
    while gate.sockets and time.monotonic() < deadline:
        time.sleep(.01)
    assert not gate.sockets  # the relay handler is done, so a resend would already have reached the peer
    assert len(relay.calls) == 1


def test_cancellation_closes_resend_socket(relay):
    started, closed = threading.Event(), threading.Event()
    def wait_for_abort(peer):
        started.set()
        if peer.rfile.read(1) == b'':
            closed.set()
    relay.replies.extend([(415, {}, b'rejected'), wait_for_abort])
    def run():
        try:
            relay.post()
        except (http.client.RemoteDisconnected, ConnectionResetError):
            pass
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert started.wait(2)
        gate = relay.gates[-1]
        with gate.lock:
            assert len(gate.sockets) == 2  # Native and the new upstream; first upstream removed.
        gate.abort()
        assert closed.wait(2)
    finally:
        worker.join(4)
    assert not worker.is_alive() and len(relay.calls) == 2
