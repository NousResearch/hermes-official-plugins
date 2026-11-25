"""A cooldown_seconds that is not a number must not fail the connection.

Every other numeric setting in this adapter tolerates a bad value: _read_heartbeat
documents that "a non-numeric setting falls back to the default rather than failing
the connection". cooldown_seconds is read with a bare int(), so a quoted or blank
value in a platform config raises inside __init__ and the platform never comes up.
"""

from __future__ import annotations

import pytest

from gateway.config import PlatformConfig
from homeassistant_plugin.adapter import HomeAssistantAdapter


def _adapter(**extra) -> HomeAssistantAdapter:
    return HomeAssistantAdapter(PlatformConfig(enabled=True, token="tok", extra=extra))


@pytest.mark.parametrize("raw", ["abc", "", "   ", "12s", None, [], {}])
def test_a_non_numeric_cooldown_falls_back_to_the_default(raw):
    adapter = _adapter(cooldown_seconds=raw)
    assert adapter._cooldown_seconds == 30


@pytest.mark.parametrize("raw,expected", [("120", 120), (0, 0), (2.0, 2), (45, 45)])
def test_a_numeric_cooldown_is_still_honoured(raw, expected):
    adapter = _adapter(cooldown_seconds=raw)
    assert adapter._cooldown_seconds == expected


def test_a_negative_cooldown_is_clamped_to_no_delay():
    """A negative window would make every event pass the 'not too soon' test."""
    adapter = _adapter(cooldown_seconds=-30)
    assert adapter._cooldown_seconds == 0


def test_the_default_cooldown_is_used_when_the_key_is_absent():
    adapter = _adapter()
    assert adapter._cooldown_seconds == 30
