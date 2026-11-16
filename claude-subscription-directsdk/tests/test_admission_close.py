"""A step's relay closes at once: close() wakes the relay's loop instead of waiting for its next poll."""
from pathlib import Path
import socket
import statistics
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admission import Admission


def test_a_relay_closes_without_waiting_for_a_poll():
    """Before, close() waited for serve_forever's next 50 ms poll: about 50 ms on every step."""
    took = []
    for _ in range(20):
        gate = Admission('http://127.0.0.1:9', 5)
        started = time.perf_counter()
        gate.close()
        took.append(time.perf_counter() - started)
        assert not gate.thread.is_alive()
    assert statistics.median(took) < .01, f'median close {statistics.median(took) * 1000:.1f} ms'


def test_a_closed_relay_stops_listening_and_a_second_close_is_harmless():
    gate = Admission('http://127.0.0.1:9', 5)
    port = gate.server.server_port
    gate.close()
    gate.close()
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', port), timeout=1).close()
