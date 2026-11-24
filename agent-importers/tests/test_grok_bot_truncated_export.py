"""A Grok Bot export that arrives cut off must be reported as cut off.

A long export gets split by the Bot across several messages, so a user pasting the first half is a
routine case -- and the parser has a message written for it ("ask the Bot to send the rest"). That
message is only reachable if the recovery slice is skipped when there is no closing brace.
"""

from __future__ import annotations

import pytest

from agent_importers_plugin.grok_bot import ExportFormatError, parse_export


@pytest.mark.parametrize(
    "text",
    [
        '{"format": "grok-bot-export", "version": 1, "bots": [{"name": "Account Health"',
        '{ "format": "grok-bot-export", "bots": []',
    ],
    ids=["half-pasted-reply", "missing-closing-brace"],
)
def test_a_cut_off_export_is_reported_as_cut_off(text: str) -> None:
    with pytest.raises(ExportFormatError) as raised:
        parse_export(text)
    assert "cut off" in str(raised.value)


def test_an_unterminated_document_does_not_escape_as_a_bare_value_error() -> None:
    """ExportFormatError is a ValueError, so the caller cannot tell the two apart by type: an
    str.rindex failure would be reported to the user as a raw "substring not found"."""
    with pytest.raises(ExportFormatError):
        parse_export('{ "format": "grok-bot-export", "bots": []')
