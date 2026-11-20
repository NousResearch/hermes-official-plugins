"""A relay starts without asking DNS for its own name.

``http.server.HTTPServer.server_bind`` looks up the bound address's name (``socket.getfqdn``) only to fill
``server_name``, which the relay never reads. Every model call starts a relay, so on a host whose resolver stalls on
127.0.0.1 the first call of each process waited out that lookup (35 s on one macOS host).
"""
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admission import Admission


def test_a_relay_starts_without_a_reverse_lookup(monkeypatch):
    asked = []

    def stalled(name=''):
        asked.append(name)
        time.sleep(3)
        return name
    monkeypatch.setattr(socket, 'getfqdn', stalled)
    started = time.monotonic()
    gate = Admission('https://api.anthropic.com', 30)
    try:
        assert time.monotonic() - started < 1, 'the relay waited for a reverse DNS lookup'
        assert asked == []
        assert gate.url.startswith(f'http://127.0.0.1:{gate.server.server_port}/admit/')
    finally:
        gate.close()
