"""#378: closing a cached client must not cancel another call's in-flight request."""

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from directsdk import Client


class _Request:
    """Hashable stand-in for Request with only the fields close()/_register() use."""

    def __init__(self, cancelled=None):
        self.cancelled = cancelled if cancelled is not None else []
        self.stream = None

    def cancel(self):
        self.cancelled.append(1)


def _client_with(fake_requests):
    """A Client shell with just the fields close()/_register() touch."""
    c = object.__new__(Client)
    c._lock = threading.Lock()
    c._requests = set(fake_requests)
    return c


def test_close_does_not_cancel_in_flight_requests():
    cancelled = []
    reqs = [_Request() for _ in range(2)]
    client = _client_with(reqs)
    client.close()
    assert all(r.cancelled == [] for r in reqs)


def test_a_closed_client_still_takes_new_requests():
    req = _Request()
    client = _client_with([req])
    client.close()
    # Hermes reuses cached clients across concurrent tool calls: create() must still work.
    assert client._register() in client._requests
