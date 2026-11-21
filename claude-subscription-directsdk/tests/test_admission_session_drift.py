"""Regression for session admission drift, portable to the pre-session relay."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import threading
from admission import Admission


def test_session_drift_is_rejected_before_upstream():
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            self.server.calls += 1
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'{}')
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer); peer.calls = 0
    thread = threading.Thread(target=peer.serve_forever, daemon=True); thread.start()
    try:
        for drift in (False, True):
            gate = Admission(f'http://127.0.0.1:{peer.server_port}', 2)
            gate.frames = []
            gate.last_forwarded = {'model': 'old', 'messages': []} if drift else None
            conn = http.client.HTTPConnection('127.0.0.1', gate.server.server_port, timeout=3)
            before = peer.calls
            try:
                body = json.dumps({'model': 'new', 'messages': [{'role': 'user', 'content': 'hello'}]})
                conn.request('POST', gate.prefix + '/v1/messages', body, {'Content-Type': 'application/json'})
                response = conn.getresponse(); response.read()
                assert (response.status, peer.calls - before) == ((400, 0) if drift else (200, 1))
            finally:
                conn.close(); gate.close()
    finally:
        peer.shutdown(); peer.server_close(); thread.join()
