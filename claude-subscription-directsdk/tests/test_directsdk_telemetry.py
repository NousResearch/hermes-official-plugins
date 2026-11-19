"""#331: Claude Code's telemetry stays off unless the user turns the plugin's ``claude_code_telemetry``
setting on; the picker handshake still fetches feature flags (#86), and the CLI never updates itself.

Each test reads the environment the fake CLI actually received, on the setup probe and on a turn.
"""
import json
import os
import sys

import pytest

from conftest import PLUGIN_NAME
from test_directsdk import FAKE
from test_directsdk_setup import FAKE_CLI, PINNED_PICKER, PRO

QUIET = ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING")
# Dump the child's environment before the fake does anything else.
CAPTURE = "import json as _j, os as _o\nopen(_o.environ['ENV_CAPTURE'], 'a').write(_j.dumps(dict(_o.environ)) + '\\n')\n"


def _set_telemetry(value):
    """Write the setting where `hermes config set plugins.entries.<id>.settings.<key>` puts it."""
    from hermes_cli.config import get_config_path
    path = get_config_path()
    path.write_text("plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n        claude_code_telemetry: %s\n"
                    % (PLUGIN_NAME, value), encoding="utf-8")


def _base_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if k not in QUIET and k != "DISABLE_AUTOUPDATER"}
    return {**env, "PATH": os.defpath, "ENV_CAPTURE": str(tmp_path / "env.jsonl"), **extra}


def _captured(tmp_path):
    return [json.loads(line) for line in (tmp_path / "env.jsonl").read_text().splitlines()]


def _discovery_envs(profile, tmp_path, **extra):
    script = tmp_path / "claude.py"
    script.write_text(CAPTURE + FAKE_CLI)
    env = _base_env(tmp_path, FAKE_STATE=json.dumps({**PRO, "models": PINNED_PICKER}), **extra)
    assert profile.discover_models(command=[sys.executable, str(script)], env=env)
    envs = _captured(tmp_path)
    assert len(envs) == 2  # auth status, then the initialize handshake
    return envs


def _turn_env(profile, tmp_path, **extra):
    script = tmp_path / "native.py"
    script.write_text(FAKE.replace("rows=[]", CAPTURE + "rows=[]"))
    client = profile.create_client(command=[sys.executable, str(script)], env=_base_env(tmp_path, HOME=str(tmp_path), **extra))
    try:
        client.create(model="sonnet", messages=[{"role": "user", "content": "go"}],
                      tools=[{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                                               "parameters": {"type": "object", "properties": {}}}}])
    finally:
        client.close()
    (env,) = _captured(tmp_path)
    return env


@pytest.mark.parametrize("value", [None, "false", "'off'"])
def test_the_picker_handshake_fetches_feature_flags_with_telemetry_off(profile, tmp_path, value):
    """#86: DISABLE_TELEMETRY or the nonessential-traffic flag shrinks the CLI picker 13 -> 5 (2.1.319), so the
    zero-inference handshake leaves both unset; error reporting stays off and auth status stays quiet."""
    if value:
        _set_telemetry(value)
    status, handshake = _discovery_envs(profile, tmp_path)
    assert {k: status.get(k) for k in QUIET} == dict.fromkeys(QUIET, "1")
    assert "DISABLE_TELEMETRY" not in handshake and "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in handshake
    assert handshake["DISABLE_ERROR_REPORTING"] == "1" and handshake["DISABLE_AUTOUPDATER"] == "1"


@pytest.mark.parametrize("value", [None, "false"])
@pytest.mark.parametrize("child", ["discovery", "turn"])
def test_native_never_sends_its_own_session_title_request(profile, tmp_path, monkeypatch, child, value):
    """#116: inherited from an Agent SDK or a non-CLI entrypoint, native races the turn for the one admission with a
    session-title request. The plugin switches that request off on every child, whatever the setting."""
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_TERMINAL_TITLE", raising=False)  # a developer's own export must not pass it
    if value:
        _set_telemetry(value)
    inherited = {"CLAUDE_CODE_ENTRYPOINT": "sdk-py", "CLAUDE_AGENT_SDK_VERSION": "0.1.0"}
    envs = (_discovery_envs(profile, tmp_path, **inherited) if child == "discovery"
            else [_turn_env(profile, tmp_path, **inherited)])
    for env in envs:
        assert env.get("CLAUDE_CODE_DISABLE_TERMINAL_TITLE") == "1"


@pytest.mark.parametrize("child", ["discovery", "turn"])
def test_native_requests_go_out_under_hermes_own_name(profile, tmp_path, child):
    """anthropics/claude-code#97074: subscriptions meter the default `sdk-cli` name (and an inherited host's
    `sdk-py`) about 2x heavier per token, so every child runs as `hermes-agent`, without the artifact
    scratchpad line a non-SDK name would add to every request."""
    inherited = {"CLAUDE_CODE_ENTRYPOINT": "sdk-py"}
    envs = (_discovery_envs(profile, tmp_path, **inherited) if child == "discovery"
            else [_turn_env(profile, tmp_path, **inherited)])
    for env in envs:
        assert env["CLAUDE_CODE_ENTRYPOINT"] == "hermes-agent"
        assert env["CLAUDE_CODE_DISABLE_ARTIFACT"] == "1"


@pytest.mark.parametrize("child", ["discovery", "turn"])
def test_a_users_own_opt_out_is_never_stripped(profile, tmp_path, child):
    flags = {"DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}
    envs = (_discovery_envs(profile, tmp_path, **flags) if child == "discovery"
            else [_turn_env(profile, tmp_path, **flags)])
    for env in envs:
        assert {k: env.get(k) for k in flags} == flags


def test_the_setting_is_read_per_spawn_from_the_active_profile(profile, tmp_path):
    """No import-time caching: flipping the setting changes the very next spawn."""
    assert _turn_env(profile, tmp_path)["DISABLE_TELEMETRY"] == "1"
    _set_telemetry("true")
    (tmp_path / "env.jsonl").unlink()
    assert "DISABLE_TELEMETRY" not in _turn_env(profile, tmp_path)


def test_a_multi_profile_gateway_reads_each_profiles_own_setting(profile, tmp_path):
    """One process, two profiles: the context-local home override decides whose config is read."""
    import directsdk_setup
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    loud_home = tmp_path / "loud-profile"
    loud_home.mkdir()
    token = set_hermes_home_override(loud_home)
    try:
        _set_telemetry("true")
        assert "DISABLE_TELEMETRY" not in directsdk_setup.apply_traffic_policy({})
    finally:
        reset_hermes_home_override(token)
    assert directsdk_setup.apply_traffic_policy({})["DISABLE_TELEMETRY"] == "1"


def test_manifest_declares_the_setting_under_the_key_the_code_reads():
    """Hermes keys plugins.entries by the manifest name; the code must read that same entry."""
    from pathlib import Path
    from hermes_cli.plugins_settings import plugin_settings_fields
    import directsdk_setup
    root = Path(directsdk_setup.__file__).resolve().parent
    assert directsdk_setup.PLUGIN_ID == PLUGIN_NAME
    (field,) = [field for field in plugin_settings_fields(PLUGIN_NAME, root)
               if field["key"] == directsdk_setup.TELEMETRY_SETTING]
    assert field["key"] == directsdk_setup.TELEMETRY_SETTING
    assert field["type"] == "boolean" and field["default"] is False
