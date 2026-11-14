"""#34: every native spawn selects the active profile's Claude login directory."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from conftest import PLUGIN_NAME
from test_directsdk import FAKE
from test_directsdk_setup import PINNED_PICKER, PRO

OVERRIDE = "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR"
NATIVE_DIR = "CLAUDE_CONFIG_DIR"
SETTING = "claude_config_dir"


def _set_config_dir(value):
    from hermes_cli.config import get_config_path
    get_config_path().write_text(
        "plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n        %s: %s\n"
        % (PLUGIN_NAME, SETTING, json.dumps(value)), encoding="utf-8")


@pytest.mark.parametrize("setting,override,inherited,expected", [
    ("profile-login", "override-login", "inherited-login", "profile-login"),
    (None, "override-login", "inherited-login", "override-login"),
    (None, None, "inherited-login", "inherited-login"),
    (None, None, None, None),
])
def test_config_dir_precedence(profile, tmp_path, setting, override, inherited, expected):
    import directsdk_setup
    if setting is not None:
        _set_config_dir(str(tmp_path / setting))
    env = {"UNRELATED": "preserved"}
    if override is not None:
        env[OVERRIDE] = str(tmp_path / override)
    if inherited is not None:
        env[NATIVE_DIR] = str(tmp_path / inherited)
    assert directsdk_setup.apply_config_dir(env) is env
    assert env.get(NATIVE_DIR) == (str(tmp_path / expected) if expected else None)
    assert OVERRIDE not in env
    assert env["UNRELATED"] == "preserved"


@pytest.mark.parametrize("value", ["", " \t ", None, 42])
def test_empty_or_non_string_setting_falls_through(profile, tmp_path, value):
    import directsdk_setup
    _set_config_dir(value)
    fallback = str(tmp_path / "fallback")
    assert directsdk_setup.apply_config_dir({OVERRIDE: fallback}) == {NATIVE_DIR: fallback}


@pytest.mark.parametrize("source", ["setting", "override", "inherited"])
def test_chosen_directory_expands_user(profile, tmp_path, monkeypatch, source):
    import directsdk_setup
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    env = {}
    if source == "setting":
        _set_config_dir("  ~/login  ")
    else:
        env[OVERRIDE if source == "override" else NATIVE_DIR] = "~/login"
    chosen = directsdk_setup.apply_config_dir(env)
    # expanduser keeps the separator it was given; Windows accepts either.
    assert list(chosen) == [NATIVE_DIR] and os.path.normpath(chosen[NATIVE_DIR]) == str(tmp_path / "login")


def test_empty_override_is_removed_and_falls_through(profile, tmp_path):
    import directsdk_setup
    inherited = str(tmp_path / "inherited")
    assert directsdk_setup.apply_config_dir({OVERRIDE: "", NATIVE_DIR: inherited}) == {NATIVE_DIR: inherited}


def test_unreadable_config_means_not_set(profile, tmp_path, monkeypatch):
    import directsdk_setup
    import hermes_cli.config
    def unreadable():
        raise OSError("fixture config is unreadable")
    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", unreadable)
    fallback = str(tmp_path / "fallback")
    assert directsdk_setup.apply_config_dir({OVERRIDE: fallback}) == {NATIVE_DIR: fallback}
    assert directsdk_setup.telemetry_enabled() is False


def test_a_multi_profile_gateway_reads_each_profiles_own_setting(profile, tmp_path):
    import directsdk_setup
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    first = str(tmp_path / "first-login")
    second = str(tmp_path / "second-login")
    _set_config_dir(first)
    second_home = tmp_path / "second-profile"
    second_home.mkdir()
    token = set_hermes_home_override(second_home)
    try:
        _set_config_dir(second)
        assert directsdk_setup.apply_config_dir({})[NATIVE_DIR] == second
    finally:
        reset_hermes_home_override(token)
    assert directsdk_setup.apply_config_dir({})[NATIVE_DIR] == first


def test_native_requests_read_the_active_profile_on_every_spawn(profile, tmp_path):
    import directsdk
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    capture = tmp_path / "child-dirs.jsonl"
    script = tmp_path / "native.py"
    probe = ("open(os.environ['DIR_CAPTURE'], 'a').write(json.dumps({k: os.environ[k] "
             "for k in ('CLAUDE_CONFIG_DIR', 'CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR') "
             "if k in os.environ}) + '\\n')\n")
    script.write_text(FAKE.replace("rows=[]", probe + "rows=[]"), encoding="utf-8")
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "DIR_CAPTURE": str(capture),
           OVERRIDE: str(tmp_path / "override"), NATIVE_DIR: str(tmp_path / "inherited")}
    client = directsdk.Client(command=[sys.executable, str(script)], env=env)
    request = dict(model="sonnet", messages=[{"role": "user", "content": "go"}],
                   tools=[{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                                            "parameters": {"type": "object", "properties": {}}}}])
    first, second, changed = (str(tmp_path / name) for name in ("first-login", "second-login", "changed-login"))
    try:
        _set_config_dir(first)
        assert client.create(**request).choices[0].message.content == "hello\n"
        second_home = tmp_path / "second-profile"
        second_home.mkdir()
        token = set_hermes_home_override(second_home)
        try:
            _set_config_dir(second)
            client.create(**request)
        finally:
            reset_hermes_home_override(token)
        _set_config_dir(changed)
        client.create(**request)
    finally:
        client.close()
    assert [json.loads(line) for line in capture.read_text().splitlines()] == [
        {NATIVE_DIR: first}, {NATIVE_DIR: second}, {NATIVE_DIR: changed}]
    assert client.env == env  # Only the child's copy changes.


@pytest.mark.parametrize("probe", ["setup_status", "discover_models"])
def test_setup_probes_pass_the_profile_directory(profile, tmp_path, monkeypatch, probe):
    import directsdk_setup
    selected = str(tmp_path / "profile-login")
    _set_config_dir(selected)
    captured = []
    def run(argv, **kwargs):
        captured.append(kwargs["env"])
        if argv[1:3] == ["auth", "status"]:
            output = PRO["auth"]
        else:
            output = {"type": "control_response", "response": {"response": {
                "models": PINNED_PICKER, "account": PRO["account"]}}}
        return SimpleNamespace(stdout=json.dumps(output), returncode=0)
    monkeypatch.setattr(directsdk_setup.subprocess, "run", run)
    env = {"PATH": os.defpath, "HOME": str(tmp_path), OVERRIDE: str(tmp_path / "override")}
    result = getattr(directsdk_setup, probe)(command=[sys.executable], env=env)
    assert result["logged_in"] if probe == "setup_status" else result
    assert len(captured) == (1 if probe == "setup_status" else 2)
    assert all(child[NATIVE_DIR] == selected and OVERRIDE not in child for child in captured)
    assert NATIVE_DIR not in env and OVERRIDE in env


def test_manifest_declares_the_setting_under_the_key_the_code_reads(profile):
    from hermes_cli.plugins_settings import plugin_settings_fields
    import directsdk_setup
    root = Path(directsdk_setup.__file__).resolve().parent
    assert directsdk_setup.PLUGIN_ID == PLUGIN_NAME
    fields = {field["key"]: field for field in plugin_settings_fields(PLUGIN_NAME, root)}
    field = fields[SETTING]
    assert directsdk_setup.CONFIG_DIR_SETTING == SETTING
    assert field["type"] == "string" and field["default"] == ""
    assert field["label"] and field["description"]
