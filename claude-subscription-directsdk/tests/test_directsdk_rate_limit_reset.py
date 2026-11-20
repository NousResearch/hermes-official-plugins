"""A used-up plan window tells Hermes when it reopens.

Native's own limit text names a clock time (`resets 3:45pm`) that Hermes cannot read, so the 429 has to carry
the window's reset as data: `body['resets_at']`, the field core reads for its fallback bench and limit notice.
"""
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import directsdk
from test_directsdk_admission import NATIVE

REQUEST = dict(model='sonnet', messages=[{'role': 'user', 'content': 'fixture'}])
RATE_LIMIT_BODY = (b'{"type":"error","error":{"type":"rate_limit_error",'
                   b'"message":"This request would exceed your account\'s rate limit. Please try again later."}}')


def _window(reset, status='rejected'):
    return {'anthropic-ratelimit-unified-status': status, 'anthropic-ratelimit-unified-reset': str(reset),
            'anthropic-ratelimit-unified-representative-claim': 'five_hour'}


def _rejected_through_relay(tmp_path, headers, status=429):
    """Answer native's one admitted request with ``status`` carrying ``headers``; return the error Hermes gets."""
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(RATE_LIMIT_BODY)))
            self.end_headers()
            self.wfile.write(RATE_LIMIT_BODY)

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    client = directsdk.Client(command=[sys.executable, str(native)],
                              env={'PATH': os.defpath, 'HOME': str(tmp_path),
                                   'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}'})
    try:
        with pytest.raises(directsdk.ClaudeAPIError) as raised:
            client.create(**REQUEST)
        return raised.value
    finally:
        client.close()
        peer.shutdown()
        thread.join()
        peer.server_close()


def test_a_used_up_plan_window_carries_its_reset(tmp_path):
    reset = int(time.time()) + 3 * 3600
    error = _rejected_through_relay(tmp_path, _window(reset))
    assert error.status_code == 429
    assert getattr(error, 'body', None) == {'resets_at': reset}


@pytest.mark.parametrize('headers', [
    {},
    _window(4102444800, status='allowed'),
    {'anthropic-ratelimit-unified-status': 'rejected'},
    {'anthropic-ratelimit-unified-status': 'rejected', 'anthropic-ratelimit-unified-reset': 'soon'},
    {'anthropic-ratelimit-unified-status': 'rejected', 'anthropic-ratelimit-unified-reset': '-5'},
    {'anthropic-ratelimit-unified-status': 'rejected', 'anthropic-ratelimit-unified-reset': 'inf'},
], ids=['throttle', 'window-open', 'no-reset', 'not-a-number', 'negative', 'infinite'])
def test_a_429_that_names_no_used_up_window_carries_no_reset(tmp_path, headers):
    # Every answer carries the window's reset; only a rejected window makes it this 429's reset, so a
    # throttle while the window is open never benches the route for hours.
    error = _rejected_through_relay(tmp_path, headers)
    assert error.status_code == 429
    assert getattr(error, 'body', None) is None


def test_a_rejected_window_on_another_failure_carries_no_reset(tmp_path):
    # An overload that still reports the used-up window is not a limit answer: its retry belongs to the 529.
    error = _rejected_through_relay(tmp_path, _window(int(time.time()) + 3600), status=529)
    assert error.status_code == 529
    assert getattr(error, 'body', None) is None


def test_hermes_reads_the_reset_for_its_fallback_bench_and_limit_notice(profile, tmp_path):
    from agent.agent_runtime_helpers import extract_api_error_context
    from agent.error_classifier import FailoverReason, classify_api_error

    reset = int(time.time()) + 3 * 3600
    error = _rejected_through_relay(tmp_path, _window(reset))
    verdict = classify_api_error(error, provider=profile.name, model='sonnet')
    assert verdict.reason == FailoverReason.rate_limit and verdict.should_fallback
    # The fallback bench (classifier) and the limit notice / Desktop retry card (error context) both see it.
    assert (verdict.error_context or {}).get('reset_at') == pytest.approx(reset, abs=5)
    assert extract_api_error_context(error).get('reset_at') == reset
