"""Service-policy regressions using synthetic targets and no Home Assistant server."""

import json
from unittest.mock import AsyncMock

import pytest

import homeassistant_plugin.tools as ha_tool


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch):
    monkeypatch.setattr(ha_tool, "_HASS_ENTITY_ALLOWLIST", "")
    monkeypatch.setattr(ha_tool, "_HASS_ENTITY_DENYLIST", "")
    monkeypatch.setattr(ha_tool, "_get_entity_filter_config", lambda: ([], []))


def set_policy(monkeypatch, allow=(), deny=()):
    monkeypatch.setattr(ha_tool, "_get_entity_filter_config", lambda: (list(allow), list(deny)))


@pytest.mark.parametrize("domain", sorted(ha_tool._BLOCKED_DOMAINS))
def test_blocked_domain_with_terminal_linefeed_never_dispatches(monkeypatch, domain):
    dispatch = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(ha_tool, "_async_call_service", dispatch)

    result = json.loads(ha_tool._handle_call_service({
        "domain": domain + "\n", "service": "test_service",
    }))

    assert "error" in result
    assert "Invalid domain format" in result["error"]
    dispatch.assert_not_called()


@pytest.mark.parametrize("field", ["domain", "service"])
@pytest.mark.parametrize("suffix", ["\n", "\r", "\t", "\x00", "\x7f", " "])
def test_noncanonical_service_selectors_never_dispatch(monkeypatch, field, suffix):
    dispatch = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(ha_tool, "_async_call_service", dispatch)
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.test"}
    args[field] += suffix

    result = json.loads(ha_tool._handle_call_service(args))

    assert "error" in result
    assert f"Invalid {field} format" in result["error"]
    dispatch.assert_not_called()


@pytest.mark.parametrize("policy", ["allow", "deny"])
@pytest.mark.parametrize("targets", [
    {},
    {"entity_id": "sensor.allowed"},
    {"data": {"entity_id": "sensor.allowed"}},
    {"data": {"variables": {"source_entity_id": "sensor.allowed"}}},
])
def test_custom_script_identity_is_checked_with_explicit_targets(monkeypatch, policy, targets):
    if policy == "allow":
        set_policy(monkeypatch, allow=["sensor.allowed"])
    else:
        set_policy(monkeypatch, deny=["script.denied"])
    dispatch = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(ha_tool, "_async_call_service", dispatch)

    result = json.loads(ha_tool._handle_call_service({
        "domain": "script", "service": "denied", **targets,
    }))

    assert "error" in result
    assert "script.denied" in result["error"]
    assert "HASS_ENTITY_" in result["error"]
    dispatch.assert_not_called()


def test_custom_script_and_explicit_target_must_both_be_permitted(monkeypatch):
    set_policy(monkeypatch, allow=["script.allowed", "sensor.allowed"])
    dispatch = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(ha_tool, "_async_call_service", dispatch)

    result = json.loads(ha_tool._handle_call_service({
        "domain": "script", "service": "allowed", "entity_id": "sensor.allowed",
    }))

    assert result["result"]["success"] is True
    dispatch.assert_awaited_once_with("script", "allowed", "sensor.allowed", None)


def test_custom_script_permission_does_not_hide_denied_explicit_target(monkeypatch):
    set_policy(monkeypatch, allow=["script.allowed", "sensor.*"], deny=["sensor.denied"])
    dispatch = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(ha_tool, "_async_call_service", dispatch)

    result = json.loads(ha_tool._handle_call_service({
        "domain": "script", "service": "allowed", "entity_id": "sensor.denied",
    }))

    assert "sensor.denied" in result["error"]
    assert "HASS_ENTITY_DENYLIST" in result["error"]
    dispatch.assert_not_called()


@pytest.mark.parametrize("service", sorted(ha_tool._SCRIPT_BUILTIN_SERVICES))
def test_builtin_script_services_only_check_payload_entities(monkeypatch, service):
    set_policy(monkeypatch, allow=["script.allowed"])
    dispatch = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(ha_tool, "_async_call_service", dispatch)

    result = json.loads(ha_tool._handle_call_service({
        "domain": "script", "service": service, "entity_id": "script.allowed",
    }))

    assert result["result"]["success"] is True
    dispatch.assert_awaited_once_with("script", service, "script.allowed", None)


def test_custom_script_without_entity_filters_preserves_dispatch(monkeypatch):
    dispatch = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(ha_tool, "_async_call_service", dispatch)

    result = json.loads(ha_tool._handle_call_service({
        "domain": "script", "service": "unrestricted", "entity_id": "sensor.allowed",
    }))

    assert result["result"]["success"] is True
    dispatch.assert_awaited_once_with("script", "unrestricted", "sensor.allowed", None)
