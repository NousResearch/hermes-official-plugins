"""#475: a queued call dropped by the slot cap is not an interrupt."""
import threading

import pytest

import concurrency


def test_slot_wait_cancel_has_a_distinct_error_type():
    cancelled = threading.Event()
    cancelled.set()
    wait = concurrency.acquire(4, cancelled, 30, lambda: None)
    with pytest.raises(Exception) as exc:
        next(wait)
    assert type(exc.value).__name__ == "SlotCapacityCancelled"
    assert "max_concurrent" in str(exc.value)
    assert "4" in str(exc.value)
    assert "Claude request cancelled" not in str(exc.value)


def test_timeout_still_reports_the_limit():
    # Occupy the pool's single slot through its own counter, then release it.
    with concurrency._pool.cond:
        concurrency._pool.held += 1
    try:
        wait = concurrency.acquire(1, threading.Event(), 0.01, lambda: None)
        with pytest.raises(TimeoutError) as exc:
            next(wait)
        assert "max_concurrent 1" in str(exc.value)
    finally:
        with concurrency._pool.cond:
            concurrency._pool.held -= 1
