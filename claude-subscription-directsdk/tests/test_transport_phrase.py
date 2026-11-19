"""#396: a status-less relay failure must stay classifiable as a transport error."""

import pytest

from directsdk import ClaudeAPIError, transport_phrase


def _wrapped(label):
    """The message the client raises for an HTTP-200 capture cut short by ``label``."""
    return ClaudeAPIError(f"Incomplete upstream response (first upstream attempt: status 200, capture incomplete, "
                          f"relay failure {label}, native retries denied: 2) [{transport_phrase(label)}]", None)


@pytest.mark.parametrize("label", ["UpstreamIdle", "ConnectionResetError", "BrokenPipeError",
                                   "RemoteProtocolError", "IncompleteRead"])
@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-opus-5-5[1m]"])
def test_mid_stream_failure_is_a_connection_retry_never_a_compression(label, model):
    """Through core's real classifier: a transport failure, retried, and never read as an overflow, even on a
    large session with a non-reasoning model (core compresses on its server-disconnect wording there)."""
    from agent.error_classifier import FailoverReason, classify_api_error
    verdict = classify_api_error(_wrapped(label), provider="claude-subscription-directsdk-experimental", model=model,
                                 approx_tokens=180000, context_length=200000, num_messages=300)
    assert verdict.reason is FailoverReason.timeout
    assert verdict.error_context.get("transport") == "connection"
    assert verdict.retryable and not verdict.should_compress


def test_qualified_name_is_normalised():
    assert transport_phrase("http.client.IncompleteRead") == transport_phrase("IncompleteRead") != ""


def test_unknown_or_empty_failure_adds_nothing():
    assert transport_phrase("KeyError") == ""
    assert transport_phrase(None) == ""
    assert transport_phrase("") == ""
