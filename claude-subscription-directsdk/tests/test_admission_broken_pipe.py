"""Native's throwaway pre-flight probe must not spam stderr with a traceback.

Before the real `/v1/messages` POST, native (Bun's HTTP client) fires a warm-up probe at an
`/api/hello`-style path on every turn. Handler only implements do_POST, so http.server's default
dispatch answers any other method with a 501 via send_error(); native never reads that body and
disconnects immediately, so the write raises BrokenPipeError/ConnectionResetError while
send_error() is still writing it. The 501 itself is expected and harmless to the real request
(hermes-plugin-claude-subscription-directsdk#17, archived); the bug is socketserver's default
handle_error() printing a scary "Exception occurred during processing of request..." traceback to
stderr for this exact, harmless, every-turn race.
"""
import contextlib
import io
import json
import socket
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admission import Admission


def _rst_probe(host, port, path):
    """Send a throwaway GET, then slam the socket shut with RST (not a clean FIN) before
    reading the response -- the exact shape of native's pre-flight that races send_error()."""
    s = socket.create_connection((host, port), timeout=3)
    s.send(f'GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
    s.close()


def test_probe_disconnect_does_not_print_a_traceback():
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.dumps({'ok': True}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    threading.Thread(target=peer.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
    try:
        gate = Admission(f'http://127.0.0.1:{peer.server_port}', timeout=5)
        try:
            host, port = '127.0.0.1', gate.server.server_port

            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                _rst_probe(host, port, gate.prefix + '/api/hello')
                import time
                time.sleep(.3)  # give the handler thread time to hit the race and recover
            assert captured.getvalue() == '', (
                'the harmless pre-flight 501/broken-pipe race must not reach stderr:\n' + captured.getvalue())

            # The relay must still be alive and able to serve the real request after the probe.
            import http.client
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request('POST', gate.prefix + '/v1/messages', b'{}', {'Content-Length': '2'})
            response = conn.getresponse()
            assert response.status == 200
            conn.close()
        finally:
            gate.close()
    finally:
        peer.shutdown()
        peer.server_close()


def test_a_real_handler_bug_still_prints(monkeypatch):
    """The suppression is narrow: anything other than a benign disconnect still gets the traceback,
    so a real regression in Handler is never silently swallowed."""
    gate = Admission('https://example.invalid', timeout=5)
    try:
        def boom(self, request, client_address):
            raise RuntimeError('not a broken pipe')
        monkeypatch.setattr(type(gate.server).__mro__[1], 'process_request', boom, raising=False)

        host, port = '127.0.0.1', gate.server.server_port
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            s = socket.create_connection((host, port), timeout=3)
            s.send(f'GET {gate.prefix}/api/hello HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())
            import time
            time.sleep(.3)
            s.close()
        assert 'RuntimeError' in captured.getvalue()
    finally:
        gate.close()
