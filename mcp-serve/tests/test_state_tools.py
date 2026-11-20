"""The three read-only state tools on the MCP face.

``hermes_status`` (version, home, config digest, counts), ``hermes_config_get`` (an allowlist, a
secret-shaped key refused **by name**) and ``hermes_plugins_list`` (the installed plugins; skills
belong to ``skills_list``). No tool may read a credential, write anything, or become a general
config reader.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from mcp_serve_plugin import bridge, state_tools


def _home() -> Path:
    return Path(os.environ["HERMES_HOME"])


@pytest.fixture
def profile():
    """A profile home with a config, three plugins and one skill, and two planted secrets."""
    home = _home()
    (home / "config.yaml").write_text(
        "model:\n"
        "  default: test/model\n"
        "  provider: local-ollama\n"
        "  api_key: sk-should-never-be-read\n"
        "plugins:\n"
        "  enabled: [mcp-serve, dhi]\n"
        "  disabled: [alpha]\n"
        "delegation:\n"
        "  backend: dhi\n"
        "terminal:\n"
        "  backend: bubblewrap\n"
        "mcp_servers:\n"
        "  dhi_bridge:\n"
        "    headers:\n"
        "      Authorization: Bearer sekrit-never-read\n",
        encoding="utf-8",
    )
    for name, version, kind in (("mcp-serve", "1.0.0", "standalone"), ("dhi", "0.7.0", "standalone"),
                                ("alpha", "2.0.0", "provider")):
        directory = home / "plugins" / name
        directory.mkdir(parents=True)
        (directory / "plugin.yaml").write_text(f"name: {name}\nversion: {version}\nkind: {kind}\n",
                                               encoding="utf-8")
    skill = home / "skills" / "dhi-workspace"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: dhi-workspace\ndescription: drive the gated seat\nversion: 0.5.0\n---\n\n# body\n",
        encoding="utf-8",
    )
    return home


def _payload(text: str) -> dict:
    return json.loads(text)


def test_status_is_the_state_and_not_the_config(profile, monkeypatch):
    monkeypatch.setattr(state_tools, "_version", lambda: ("1.2.3+2.gabc", "git"))
    body = _payload(state_tools.hermes_status())
    assert body["tool"] == "hermes_status"
    assert body["version"] == "1.2.3+2.gabc" and body["version_source"] == "git"
    assert body["home"] == str(profile)
    assert body["config"]["present"] is True and body["config"]["bytes"] > 0
    assert len(body["config"]["sha256"]) == 64
    assert body["plugins"] == {"installed": 3, "enabled": 2}
    assert "skills" not in body  # skills are skills_list's surface, not a second copy here
    assert "sk-should-never-be-read" not in json.dumps(body)
    assert "sekrit-never-read" not in json.dumps(body)


def test_status_counts_a_default_profile_as_enabled(profile, monkeypatch):
    """No plugins.enabled list is the profile's default: every installed plugin is active, minus the
    disabled list. The old shape reported 0 enabled here."""
    monkeypatch.setattr(state_tools, "_version", lambda: ("1.2.3", "git"))
    (profile / "config.yaml").write_text(
        "model:\n  default: test/model\nplugins:\n  disabled: [alpha]\n", encoding="utf-8")
    body = _payload(state_tools.hermes_status())
    states = {p["name"]: p["state"] for p in _payload(state_tools.hermes_plugins_list())["plugins"]}
    assert states == {"alpha": "disabled", "dhi": "default", "mcp-serve": "default"}
    assert body["plugins"] == {"installed": 3, "enabled": 2}


def test_config_get_returns_an_allowlisted_value(profile):
    body = _payload(state_tools.hermes_config_get("model.default"))
    assert body == {"tool": "hermes_config_get", "key": "model.default", "value": "test/model"}
    assert _payload(state_tools.hermes_config_get("delegation.backend"))["value"] == "dhi"


@pytest.mark.parametrize("key", ["model.api_key", "mcp_servers.dhi_bridge.headers.Authorization",
                                 "plugins.entries.bubblewrap.settings.token"])
def test_config_get_refuses_a_secret_shaped_key_by_name(profile, key):
    text = state_tools.hermes_config_get(key)
    body = _payload(text)
    assert body["error"] == "secret_key"
    assert body["key"] == key  # refused BY NAME, never by value
    assert "sk-should-never-be-read" not in text and "sekrit-never-read" not in text


def test_config_get_refuses_an_unlisted_key_with_the_list(profile):
    body = _payload(state_tools.hermes_config_get("mcp_servers"))
    assert body["error"] == "not_allowlisted"
    assert body["allowed"] == sorted(state_tools.ALLOWED_CONFIG_KEYS)
    assert "value" not in body


def test_config_get_missing_allowlisted_key_is_not_found(profile):
    assert _payload(state_tools.hermes_config_get("model.fallback"))["error"] == "not_found"


def test_plugins_list_names_and_states_without_duplicating_skills(profile):
    body = _payload(state_tools.hermes_plugins_list())
    by_name = {plugin["name"]: plugin for plugin in body["plugins"]}
    assert set(by_name) == {"mcp-serve", "dhi", "alpha"}
    assert by_name["mcp-serve"] == {"name": "mcp-serve", "version": "1.0.0", "kind": "standalone",
                                    "state": "enabled"}
    assert by_name["alpha"]["state"] == "disabled"
    assert body["plugin_count"] == 3
    assert "skills" not in body and "skill_count" not in body


def test_tools_only_registers_the_three_state_tools(profile, monkeypatch, require_mcp_2_sdk):
    """The MCP wire, not just the handlers: --tools-only lists them, the SDK derives the schema, and
    a call reaches the handler."""
    import model_tools

    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_: [])
    server = bridge.create_mcp_server(tools_only=True)
    listed = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert {"hermes_status", "hermes_config_get", "hermes_plugins_list"} <= set(listed)
    assert not set(listed) & set(bridge._TOOL_NAMES)
    assert listed["hermes_config_get"].input_schema["properties"]["key"]["type"] == "string"

    result = asyncio.run(server.call_tool("hermes_config_get", {"key": "model.default"}))
    text = "".join(getattr(block, "text", "") for block in (getattr(result, "content", None) or []))
    assert json.loads(text)["value"] == "test/model"


def test_include_tools_does_not_register_state_tools(profile, monkeypatch, require_mcp_2_sdk):
    """The bridge's --tools surface is unchanged: the state tools belong to --tools-only."""
    import model_tools

    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_: [])
    server = bridge.create_mcp_server(include_tools=True)
    listed = {tool.name for tool in asyncio.run(server.list_tools())}
    assert not {"hermes_status", "hermes_config_get", "hermes_plugins_list"} & listed
