"""A call waiting for a ``max_concurrent`` slot never starts after its own deadline or its cancellation."""
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import concurrency


def wait_for_slot(cancelled, timeout):
    """Run ``acquire`` to its end: ('slot', slot) or ('error', exception)."""
    try:
        gen = concurrency.acquire(1, cancelled, timeout, lambda: None)
        while True:
            next(gen)
    except StopIteration as done:
        return 'slot', done.value
    except Exception as error:  # noqa: BLE001
        return 'error', error


@pytest.mark.parametrize('why', ['timeout', 'cancel'])
def test_a_slot_freed_after_the_call_gave_up_is_not_taken(why, monkeypatch):
    """The waiter sleeps in a long poll; its timeout passes (50 ms), or it is cancelled, and only then is the only
    slot freed, which wakes it. Before, it took the slot and started anyway."""
    monkeypatch.setattr(concurrency, '_POLL_S', 3.0)
    deadline = time.monotonic() + 5
    while concurrency._pool.held and time.monotonic() < deadline:
        time.sleep(.01)
    assert concurrency._pool.held == 0
    holder = wait_for_slot(threading.Event(), 5)[1]
    cancelled, out = threading.Event(), []
    waiter = threading.Thread(target=lambda: out.append(wait_for_slot(cancelled, .05 if why == 'timeout' else 5)))
    waiter.start()
    while not concurrency._pool.waiting and waiter.is_alive():
        time.sleep(.005)
    time.sleep(.3)  # well past the waiter's 50 ms deadline, inside its 3 s poll
    if why == 'cancel':
        cancelled.set()
    holder.release()
    waiter.join(5)
    try:
        kind, value = out[0]
        assert kind == 'error', 'the call took a slot freed after it had given up'
        assert isinstance(value, TimeoutError if why == 'timeout' else RuntimeError)
        assert concurrency._pool.held == 0
    finally:
        if out and out[0][0] == 'slot':
            out[0][1].release()
