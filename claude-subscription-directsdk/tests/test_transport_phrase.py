"""#396: a status-less relay failure must stay classifiable as a transport error."""

import pytest

from directsdk import transport_phrase


@pytest.mark.parametrize("label,phrase", [
    ("UpstreamIdle", "server disconnected"),
    ("ConnectionResetError", "connection reset by peer"),
    ("RemoteProtocolError", "response ended prematurely"),
    ("IncompleteRead", "incomplete chunked read"),
    ("TimeoutError", "timed out"),
])
def test_known_failures_map_to_canonical_wording(label, phrase):
    assert transport_phrase(label) == phrase


def test_qualified_name_is_normalised():
    assert transport_phrase("http.client.IncompleteRead") == "incomplete chunked read"


def test_unknown_or_empty_failure_adds_nothing():
    assert transport_phrase("KeyError") == ""
    assert transport_phrase(None) == ""
    assert transport_phrase("") == ""


def test_phrase_is_recognised_by_core_classifier():
    """The wording must be one core already matches, so no core change is needed."""
    from agent.error_classifier import (
        _SERVER_DISCONNECT_PATTERNS, _PREMATURE_CLOSE_PATTERNS, _CONNECTION_MESSAGE_PATTERNS,
    )
    known = set(_SERVER_DISCONNECT_PATTERNS) | set(_PREMATURE_CLOSE_PATTERNS) | set(_CONNECTION_MESSAGE_PATTERNS)
    for label in ("UpstreamIdle", "ConnectionResetError", "RemoteProtocolError", "IncompleteRead"):
        assert transport_phrase(label) in known
