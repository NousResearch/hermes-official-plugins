"""Tests that ha_call_service rejects non-object data even when no entity filter is configured."""
import json

import pytest

from homeassistant_plugin import tools as ha_tools


@pytest.mark.parametrize("bad_data", ["[]", "123", '"hello"', [1, 2], 42])
def test_ha_call_service_rejects_non_dict_data_without_entity_filter(monkeypatch, bad_data):
    monkeypatch.setattr(ha_tools, "_get_entity_filter_config", lambda: ([], []))
    called = False

    def fail_dispatch(*_args, **_kwargs):
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(ha_tools, "_dispatch", fail_dispatch)

    raw = ha_tools._handle_call_service({"domain": "light", "service": "turn_on", "data": bad_data})
    payload = json.loads(raw)
    assert "error" in payload
    assert "expected a JSON object" in payload["error"]
    assert called is False
