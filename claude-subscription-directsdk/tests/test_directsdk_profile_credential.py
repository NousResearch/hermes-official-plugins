"""A multiplexed gateway runs each profile's Claude Code on that profile's own login.

Multiplexing never loads a served profile's ``.env`` into ``os.environ``, and Claude Code prefers
``CLAUDE_CODE_OAUTH_TOKEN`` over the login in ``CLAUDE_CONFIG_DIR``. So the child gets the profile's
token from core's secret scope, or loses the inherited token when ``claude_config_dir`` picks the login.
"""
import json
import logging
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from conftest import popen_from_run

from test_directsdk import FAKE
from test_directsdk_config_dir import _set_config_dir
from test_directsdk_setup import PRO

TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
AMBIENT = "sk-ant-oat01-ambient-gateway-token"
PROFILE = "sk-ant-oat01-served-profile-token"
REQUEST = dict(model="sonnet", messages=[{"role": "user", "content": "go"}],
               tools=[{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                                        "parameters": {"type": "object", "properties": {}}}}])


@contextmanager
def multiplexed(scope):
    """One served turn: multiplex semantics, with ``scope`` bound (``None``: no scope bound)."""
    from agent.secret_scope import (reset_multiplex_context, reset_secret_scope, set_multiplex_context,
                                    set_secret_scope)
    mode = set_multiplex_context(True)
    bound = set_secret_scope(scope) if scope is not None else None
    try:
        yield
    finally:
        if bound is not None:
            reset_secret_scope(bound)
        reset_multiplex_context(mode)


@pytest.fixture
def ambient(profile, monkeypatch):
    """The gateway process carries its own token, as a systemd unit or launch .env would."""
    monkeypatch.setenv(TOKEN, AMBIENT)
    return profile


def _apply(env):
    import directsdk_setup
    before = dict(os.environ)
    assert directsdk_setup.apply_profile_credential(env) is env
    assert dict(os.environ) == before
    return env


def test_the_profile_token_wins_over_the_gateways(ambient, tmp_path):
    with multiplexed({TOKEN: PROFILE}):
        assert _apply({TOKEN: AMBIENT, "UNRELATED": "kept"}) == {TOKEN: PROFILE, "UNRELATED": "kept"}
        # It wins over a selected login directory too: Claude Code would prefer it anyway.
        _set_config_dir(str(tmp_path / "login"))
        assert _apply({TOKEN: AMBIENT}) == {TOKEN: PROFILE}
    assert os.environ[TOKEN] == AMBIENT


@pytest.mark.parametrize("scope", [{}, {TOKEN: ""}, {TOKEN: "  "}], ids=["miss", "empty", "blank"])
def test_a_served_profile_never_inherits_the_gateways_login(ambient, tmp_path, scope):
    """#313: without a token of its own a served profile loses the launch profile's token, gateway headers and
    config directory, and runs on the login its claude_config_dir names."""
    setting = str(tmp_path / "login")
    _set_config_dir(setting)
    inherited = {TOKEN: AMBIENT, "ANTHROPIC_CUSTOM_HEADERS": "X-Gateway: launch", "CLAUDE_CONFIG_DIR": setting,
                 "UNRELATED": "kept"}
    with multiplexed(scope):
        env = _apply(dict(inherited))
    assert env == {"UNRELATED": "kept", "CLAUDE_CONFIG_DIR": setting}


@pytest.mark.parametrize("scope", [{}, {TOKEN: "  "}], ids=["miss", "blank"])
def test_a_served_profile_with_no_login_of_its_own_is_refused(ambient, scope):
    """No token and no directory of its own: Claude Code would fall back to ~/.claude under the inherited HOME,
    the launch profile's login and allowance. Refuse, naming both ways to give the profile a login."""
    import directsdk
    with multiplexed(scope), pytest.raises(directsdk.ClaudeCodeLoggedOut) as raised:
        _apply({TOKEN: AMBIENT, "HOME": "/launch", "CLAUDE_CONFIG_DIR": "/launch/.claude"})
    assert "claude_config_dir" in str(raised.value) and TOKEN in str(raised.value)
    assert AMBIENT not in str(raised.value)


def test_a_served_profile_with_no_login_never_spawns(ambient, tmp_path, monkeypatch):
    import directsdk
    client, seen = _native(tmp_path, monkeypatch)
    try:
        with multiplexed({}), pytest.raises(directsdk.ClaudeCodeLoggedOut):
            client.create(**REQUEST)
    finally:
        client.close()
    assert not (tmp_path / "tokens.jsonl").exists()


def test_a_routed_profile_with_no_bound_scope_names_the_scope_error(ambient, monkeypatch, tmp_path):
    """Routed by a HERMES_HOME override alone (no scope, no multiplexing): core's own scope error, not a TypeError."""
    from agent.secret_scope import UnscopedSecretError
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(tmp_path / "served-profile"))
    try:
        with pytest.raises(UnscopedSecretError):
            _apply({TOKEN: AMBIENT})
    finally:
        reset_hermes_home_override(token)


def test_a_served_profile_uses_its_own_gateway_headers_and_login_dir(ambient):
    scope = {"ANTHROPIC_CUSTOM_HEADERS": "X-Gateway: served", "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR": "/served/.claude"}
    with multiplexed(scope):
        env = _apply({TOKEN: AMBIENT, "ANTHROPIC_CUSTOM_HEADERS": "X-Gateway: launch", "CLAUDE_CONFIG_DIR": "/launch/.claude"})
    assert env == {"ANTHROPIC_CUSTOM_HEADERS": "X-Gateway: served", "CLAUDE_CONFIG_DIR": "/served/.claude"}


def test_a_single_profile_install_keeps_its_own_token(ambient, tmp_path):
    """Not multiplexed: get_secret reads os.environ, the token the child inherited anyway."""
    _set_config_dir(str(tmp_path / "login"))
    assert _apply({TOKEN: AMBIENT}) == {TOKEN: AMBIENT}


def test_an_unscoped_read_under_multiplexing_fails_closed(ambient):
    """No scope bound while multiplexing: no child may start on the launch profile's token."""
    from agent.secret_scope import UnscopedSecretError
    with multiplexed(None), pytest.raises(UnscopedSecretError):
        _apply({TOKEN: AMBIENT})


def test_unscoped_setup_probes_degrade_without_spawning(ambient, monkeypatch):
    import directsdk_setup
    spawned = []
    monkeypatch.setattr(directsdk_setup.subprocess, "run", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(directsdk_setup.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    env = {"PATH": os.defpath, TOKEN: AMBIENT}
    with multiplexed(None):
        status = directsdk_setup.setup_status(command=[sys.executable], env=env)
        assert directsdk_setup.discover_models(command=[sys.executable], env=env) is None
        limits, reason = directsdk_setup.fetch_usage(command=[sys.executable], env=env)
    assert spawned == [] and not status["logged_in"] and "could not read" in status["detail"]
    assert limits is None and "could not read" in reason


def test_an_older_core_without_secret_scope_is_no_profile_token(ambient, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)  # import raises ImportError
    assert _apply({TOKEN: AMBIENT}) == {TOKEN: AMBIENT}
    _set_config_dir(str(tmp_path / "login"))
    assert _apply({TOKEN: AMBIENT}) == {}


def test_the_token_is_never_logged_or_printed(ambient, tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    with multiplexed({TOKEN: PROFILE}):
        _apply({})
    _set_config_dir(str(tmp_path / "login"))
    with multiplexed({}):
        _apply({TOKEN: AMBIENT})
    out, err = capsys.readouterr()
    for text in (caplog.text, out, err):
        assert PROFILE not in text and AMBIENT not in text


def _native(tmp_path, monkeypatch, **extra):
    """A fake native that records the token it was started with, run from Hermes' own environment."""
    import directsdk
    capture = tmp_path / "tokens.jsonl"
    probe = ("open(os.environ['TOKEN_CAPTURE'], 'a').write(json.dumps(os.environ.get(%r)) + '\\n')\n" % TOKEN)
    script = tmp_path / "native.py"
    script.write_text(FAKE.replace("rows=[]", probe + "rows=[]"), encoding="utf-8")
    for key in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDE_CONFIG_DIR", "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TOKEN_CAPTURE", str(capture))
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    client = directsdk.Client(command=[sys.executable, str(script)])
    return client, lambda: [json.loads(line) for line in capture.read_text().splitlines()]


def test_native_spawns_run_on_the_serving_profiles_login(ambient, tmp_path, monkeypatch):
    client, seen = _native(tmp_path, monkeypatch)
    try:
        with multiplexed({TOKEN: PROFILE}):
            assert client.create(**REQUEST).choices[0].message.content == "hello\n"
        _set_config_dir(str(tmp_path / "login"))
        with multiplexed({}):
            client.create(**REQUEST)
    finally:
        client.close()
    assert seen() == [PROFILE, None]
    assert os.environ[TOKEN] == AMBIENT


def test_an_explicit_client_env_keeps_its_login(ambient, tmp_path):
    import directsdk
    capture = tmp_path / "tokens.jsonl"
    script = tmp_path / "native.py"
    script.write_text(FAKE.replace("rows=[]", "open(os.environ['TOKEN_CAPTURE'], 'a').write(json.dumps("
                                   "os.environ.get(%r)) + '\\n')\nrows=[]" % TOKEN), encoding="utf-8")
    _set_config_dir(str(tmp_path / "login"))
    client = directsdk.Client(command=[sys.executable, str(script)],
                              env={"PATH": os.defpath, "HOME": str(tmp_path), "TOKEN_CAPTURE": str(capture), TOKEN: "fixture"})
    try:
        with multiplexed({TOKEN: PROFILE}):
            client.create(**REQUEST)
    finally:
        client.close()
    assert [json.loads(line) for line in capture.read_text().splitlines()] == ["fixture"]


def test_the_logged_out_error_does_not_carry_the_token(ambient, tmp_path, monkeypatch):
    import directsdk
    client, _ = _native(tmp_path, monkeypatch, NATIVE_ERROR="authentication_failed:Not logged in")
    try:
        with multiplexed({TOKEN: PROFILE}), pytest.raises(directsdk.ClaudeCodeLoggedOut) as raised:
            client.create(**REQUEST)
    finally:
        client.close()
    assert PROFILE not in str(raised.value) and PROFILE not in repr(raised.value)


@pytest.mark.parametrize("probe", ["setup_status", "discover_models"])
def test_setup_probes_use_the_serving_profiles_login(ambient, tmp_path, monkeypatch, probe):
    import directsdk_setup
    captured = []
    def run(argv, **kwargs):
        captured.append(kwargs["env"].get(TOKEN))
        if argv[1:3] == ["auth", "status"]:
            output = PRO["auth"]
        else:
            output = {"type": "control_response", "response": {"response": {"models": [], "account": PRO["account"]}}}
        return SimpleNamespace(stdout=json.dumps(output), returncode=0)
    monkeypatch.setattr(directsdk_setup.subprocess, "run", run)
    monkeypatch.setattr(directsdk_setup.subprocess, "Popen", popen_from_run(run))
    env = {"PATH": os.defpath, "HOME": str(tmp_path), TOKEN: AMBIENT}
    with multiplexed({TOKEN: PROFILE}):
        getattr(directsdk_setup, probe)(command=[sys.executable], env=env)
    _set_config_dir(str(tmp_path / "login"))
    with multiplexed({}):
        getattr(directsdk_setup, probe)(command=[sys.executable], env=env)
    per_call = 1 if probe == "setup_status" else 2
    assert captured == [PROFILE] * per_call + [None] * per_call
    assert env[TOKEN] == AMBIENT
