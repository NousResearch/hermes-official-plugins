"""Unexpected plugin errors stay hidden from the chat but reach the host log."""
import logging

from hermes_security.tools import error_envelope


def test_unexpected_error_is_hidden_but_logged_with_traceback(caplog):
    try:
        raise RuntimeError("NOT NULL constraint failed: findings.detectors")
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR, logger="hermes_security.tools"):
            envelope = error_envelope(exc)
    assert envelope == {"ok": False, "error": {"code": "internal_error", "message": "Security operation failed"}}
    records = [r for r in caplog.records if r.name == "hermes_security.tools" and r.exc_info]
    assert records, "the traceback must be logged so the cause is visible without debugging in chat"
    assert "findings.detectors" in "".join(logging.Formatter().formatException(records[0].exc_info))


def test_expected_errors_are_not_logged_as_failures(caplog):
    from hermes_security.errors import NotFound
    with caplog.at_level(logging.ERROR, logger="hermes_security.tools"):
        envelope = error_envelope(NotFound("Missing scan"))
    assert envelope["error"]["code"] == "not_found"
    assert not [r for r in caplog.records if r.name == "hermes_security.tools"]
