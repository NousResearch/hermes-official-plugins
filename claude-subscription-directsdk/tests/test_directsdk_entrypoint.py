"""#296: choose the native caller per profile, consistently across every child."""
import json
import os
from pathlib import Path
import sys

import pytest

from conftest import PLUGIN_NAME
from test_directsdk import FAKE
from test_directsdk_telemetry import CAPTURE, _base_env, _captured, _discovery_envs, _turn_env

SETTING = "claude_code_entrypoint"
NATIVE_KEY = "CLAUDE_CODE_ENTRYPOINT"


def _set_entrypoint(value):
    from hermes_cli.config import get_config_path
    get_config_path().write_text(json.dumps({"plugins": {"enabled": [], "entries": {
        PLUGIN_NAME: {"settings": {SETTING: value}}}}}), encoding="utf-8")


def _usage_env(tmp_path):
    import directsdk_setup
    script = tmp_path / "usage.py"
    reply = {"type": "control_response", "response": {"subtype": "success",
             "request_id": "hermes-usage", "response": {"rate_limits": {"limits": []}}}}
    script.write_text(CAPTURE + "import sys\nsys.stdin.readline()\nprint(" + repr(json.dumps(reply)) + ")\n",
                      encoding="utf-8")
    limits, error = directsdk_setup.fetch_usage(command=[sys.executable, str(script)],
                                               env=_base_env(tmp_path, **{NATIVE_KEY: "sdk-py"}))
    assert limits == {"limits": []} and error is None
    return _captured(tmp_path)


@pytest.mark.parametrize("choice", ["hermes-agent", "sdk-cli"])
@pytest.mark.parametrize("child", ["discovery", "turn", "usage"])
def test_choice_reaches_every_native_child(profile, tmp_path, child, choice):
    _set_entrypoint(choice)
    if child == "usage":
        envs = _usage_env(tmp_path)
    elif child == "discovery":
        # Discovery includes both auth status and the model-picker handshake.
        envs = _discovery_envs(profile, tmp_path, **{NATIVE_KEY: "sdk-py"})
    else:
        envs = [_turn_env(profile, tmp_path, **{NATIVE_KEY: "sdk-py"})]
    for env in envs:
        assert env[NATIVE_KEY] == choice
        assert env["CLAUDE_CODE_DISABLE_ARTIFACT"] == "1"
        assert env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "1"
        assert env["DISABLE_AUTOUPDATER"] == "1"
        assert env["DISABLE_FEEDBACK_COMMAND"] == "1"


@pytest.mark.parametrize("value", ["", " sdk-cli ", "SDK-CLI", "cli", "sdk-py", None, False, 1, [], {}])
def test_invalid_choice_rejected_before_native_spawn(profile, tmp_path, value):
    _set_entrypoint(value)
    with pytest.raises(ValueError, match="claude_code_entrypoint.*hermes-agent.*sdk-cli"):
        _turn_env(profile, tmp_path)
    assert not (tmp_path / "env.jsonl").exists()


@pytest.mark.parametrize("probe", ["setup_status", "discover_models", "fetch_usage"])
def test_invalid_choice_does_not_probe_or_report_a_login_failure(profile, tmp_path, monkeypatch, probe):
    import directsdk_setup

    _set_entrypoint("unsupported")

    def unexpected_spawn(*args, **kwargs):
        raise AssertionError("Invalid settings must be rejected before spawning Claude Code")

    monkeypatch.setattr(directsdk_setup.subprocess, "run", unexpected_spawn)
    with pytest.raises(ValueError, match="claude_code_entrypoint.*hermes-agent.*sdk-cli"):
        getattr(directsdk_setup, probe)(command=[sys.executable], env={"PATH": os.defpath})


def test_reused_client_reads_profile_and_changed_setting_per_spawn(profile, tmp_path):
    import directsdk
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    script = tmp_path / "native.py"
    script.write_text(FAKE.replace("rows=[]", CAPTURE + "rows=[]"), encoding="utf-8")
    env = _base_env(tmp_path, HOME=str(tmp_path), **{NATIVE_KEY: "sdk-py"})
    parent_caller = os.environ.get(NATIVE_KEY)
    client = directsdk.Client(command=[sys.executable, str(script)], env=env)
    request = dict(model="sonnet", messages=[{"role": "user", "content": "go"}],
                   tools=[{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                                            "parameters": {"type": "object", "properties": {}}}}])
    try:
        _set_entrypoint("sdk-cli")
        client.create(**request)
        second_home = tmp_path / "second-profile"
        second_home.mkdir()
        token = set_hermes_home_override(second_home)
        try:
            _set_entrypoint("hermes-agent")
            client.create(**request)
        finally:
            reset_hermes_home_override(token)
        client.create(**request)
        _set_entrypoint("hermes-agent")
        client.create(**request)
    finally:
        client.close()
    assert [env[NATIVE_KEY] for env in _captured(tmp_path)] == [
        "sdk-cli", "hermes-agent", "sdk-cli", "hermes-agent"]
    assert client.env == env  # Only each child's copy changes.
    assert os.environ.get(NATIVE_KEY) == parent_caller


def test_missing_or_unreadable_config_keeps_default(profile, monkeypatch):
    import directsdk_setup
    import hermes_cli.config

    assert directsdk_setup.apply_traffic_policy({NATIVE_KEY: "sdk-cli"})[NATIVE_KEY] == "hermes-agent"

    def unreadable():
        raise OSError("fixture config is unreadable")

    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", unreadable)
    assert directsdk_setup.apply_traffic_policy({NATIVE_KEY: "sdk-py"})[NATIVE_KEY] == "hermes-agent"


def test_manifest_exposes_and_validates_only_supported_choices(profile):
    import directsdk_setup
    from hermes_cli.plugins_settings import plugin_settings_fields, save_plugin_settings

    root = Path(directsdk_setup.__file__).resolve().parent
    fields = {f["key"]: f for f in plugin_settings_fields(PLUGIN_NAME, root)}
    field = fields[SETTING]
    assert field["type"] == "enum"
    assert field["default"] == "hermes-agent"
    assert field["choices"] == ["hermes-agent", "sdk-cli"]
    save_plugin_settings(PLUGIN_NAME, root, {SETTING: "sdk-cli"})
    assert directsdk_setup.apply_traffic_policy({})[NATIVE_KEY] == "sdk-cli"
    with pytest.raises(ValueError, match="declared choices"):
        save_plugin_settings(PLUGIN_NAME, root, {SETTING: "cli"})
