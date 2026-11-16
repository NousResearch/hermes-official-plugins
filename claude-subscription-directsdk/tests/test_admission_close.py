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


@pytest.mark.parametrize('missing', ['socketpair', 'selector'])
def test_a_relay_that_cannot_start_its_loop_closes_its_listener(monkeypatch, missing):
    """Out of descriptors: the wake pair, or the loop's selector, cannot be made. The relay raises at once (not later,
    from a dead thread), and its listening socket is closed."""
    import admission
    made = []
    real = admission._Server

    def server(*args, **kwargs):
        made.append(real(*args, **kwargs))
        return made[-1]

    def no_pair():
        raise OSError(24, 'Too many open files')
    monkeypatch.setattr(admission, '_Server', server)
    if missing == 'socketpair':
        monkeypatch.setattr(admission.socket, 'socketpair', no_pair)
    else:
        monkeypatch.setattr(admission.selectors, 'DefaultSelector', no_pair)
    with pytest.raises(OSError, match='Too many open files'):
        Admission('http://127.0.0.1:9', 5)
    assert made and made[0].socket.fileno() == -1


def test_a_connection_gone_before_it_is_accepted_does_not_hold_close():
    """The loop saw the listener readable, then the connection went (the client was killed). handle_request() selects
    again before it accepts; it must not wait there for another connection, or close() would wait with it."""
    gate = Admission('http://127.0.0.1:9', 5)
    try:
        started = time.perf_counter()
        gate.server.handle_request()  # nothing pending: returns at once instead of waiting for a connection
        assert time.perf_counter() - started < 1
    finally:
        gate.close()
