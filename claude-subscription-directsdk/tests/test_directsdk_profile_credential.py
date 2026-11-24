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


SERVED_HOME = os.path.join(os.sep, "hermes-test", "profiles", "served")


@contextmanager
def multiplexed(scope, home=SERVED_HOME):
    """One served turn: multiplex semantics, with ``scope`` bound (``None``: no scope bound). Core stamps a served
    profile's scope with its home; ``home=None`` binds it unstamped, which core uses for the launch profile's own."""
    from agent.secret_scope import (reset_multiplex_context, reset_secret_scope, set_multiplex_context,
                                    set_secret_scope)
    mode = set_multiplex_context(True)
    bound = set_secret_scope(scope, profile_home=home) if scope is not None else None
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


def _isolate(home=None, **settings):
    """Turn isolate_profile_logins on in ``home`` (default: this install's root profile), with other settings."""
    from hermes_constants import get_hermes_home
    _settings(home or get_hermes_home(), isolate_profile_logins=True, **settings)


@pytest.mark.parametrize("scope", [{}, {TOKEN: ""}, {TOKEN: "  "}], ids=["miss", "empty", "blank"])
def test_a_served_profile_shares_the_subscription_login_by_default(ambient, tmp_path, monkeypatch, scope):
    """#481: one person's profiles share one subscription. A served profile with no login of its own runs on the
    login this process inherited (its token or config directory), never on the launch profile's gateway headers."""
    client, seen = _native(tmp_path, monkeypatch)
    try:
        with multiplexed(scope):
            assert client.create(**REQUEST).choices[0].message.content == "hello\n"
    finally:
        client.close()
    assert seen() == [AMBIENT]
    inherited = {TOKEN: AMBIENT, "ANTHROPIC_CUSTOM_HEADERS": "X-Gateway: launch", "CLAUDE_CONFIG_DIR": "/launch-login",
                 "UNRELATED": "kept"}
    with multiplexed(scope):
        assert _apply(dict(inherited)) == {TOKEN: AMBIENT, "CLAUDE_CONFIG_DIR": "/launch-login", "UNRELATED": "kept"}
    monkeypatch.delenv(TOKEN)
    with multiplexed(scope):  # the shared ~/.claude: nothing to carry, nothing exported
        assert _apply({"UNRELATED": "kept"}) == {"UNRELATED": "kept"}


@pytest.mark.parametrize("scope", [{}, {TOKEN: ""}, {TOKEN: "  "}], ids=["miss", "empty", "blank"])
def test_an_isolated_install_never_runs_a_served_profile_on_the_gateways_login(ambient, tmp_path, monkeypatch, scope):
    """#313 under isolate_profile_logins: without a token of its own a served profile loses the launch profile's
    token, gateway headers and config directory. With no directory of its own either, Claude Code would fall back to
    ~/.claude under the inherited HOME (the launch profile's login), so the turn is refused before a spawn, naming
    the switch and both ways to give the profile a login. With a claude_config_dir it runs on that login."""
    import directsdk
    _isolate()
    client, seen = _native(tmp_path, monkeypatch)
    try:
        with multiplexed(scope), pytest.raises(directsdk.ClaudeCodeLoggedOut) as raised:
            client.create(**REQUEST)
    finally:
        client.close()
    assert not (tmp_path / "tokens.jsonl").exists()
    for named in ("claude_config_dir", TOKEN, "isolate_profile_logins", "CLAUDE_CONFIG_DIR=<dir> claude auth login"):
        assert named in str(raised.value)
    assert AMBIENT not in str(raised.value)
    setting = str(tmp_path / "login")
    _isolate(claude_config_dir=setting)
    inherited = {TOKEN: AMBIENT, "ANTHROPIC_CUSTOM_HEADERS": "X-Gateway: launch", "CLAUDE_CONFIG_DIR": setting,
                 "UNRELATED": "kept"}
    with multiplexed(scope):
        env = _apply(dict(inherited))
    assert env == {"UNRELATED": "kept", "CLAUDE_CONFIG_DIR": setting}


def test_a_single_profile_install_keeps_its_own_token(ambient, tmp_path):
    """Not multiplexed: get_secret reads os.environ, the token the child inherited anyway."""
    _set_config_dir(str(tmp_path / "login"))
    assert _apply({TOKEN: AMBIENT}) == {TOKEN: AMBIENT}


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


def _settings(home, **settings):
    """Write ``settings`` as this plugin's settings in ``home``'s config.yaml."""
    from conftest import PLUGIN_NAME
    lines = "".join("        %s: %s\n" % (key, json.dumps(value)) for key, value in settings.items())
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n%s"
                                      % (PLUGIN_NAME, lines), encoding="utf-8")


def test_the_launch_profiles_own_turn_keeps_its_login_on_an_isolated_install(ambient, tmp_path, monkeypatch):
    """A Desktop backend serving several profiles multiplexes, so core counts every task as routed, the launch
    profile's own turns too (#409). Core binds that profile's scope unstamped, and its login is the process's own:
    isolation never refuses it."""
    _isolate()
    client, seen = _native(tmp_path, monkeypatch)
    try:
        with multiplexed({}, home=None):
            assert client.create(**REQUEST).choices[0].message.content == "hello\n"
    finally:
        client.close()
    assert seen() == [AMBIENT]
    with multiplexed({}, home=None):
        assert _apply({TOKEN: AMBIENT, "UNRELATED": "kept"}) == {TOKEN: AMBIENT, "UNRELATED": "kept"}
    with multiplexed({}), pytest.raises(Exception) as raised:  # a served profile in the same process
        _apply({TOKEN: AMBIENT})
    assert type(raised.value).__name__ == "ClaudeCodeLoggedOut"


@pytest.mark.parametrize("isolated", [False, True], ids=["shared", "isolated"])
def test_cores_own_launch_turn_bind_keeps_the_launch_login(profile, tmp_path, monkeypatch, isolated):
    """#409's repro through core's binder for a launch-profile turn on a multi-profile host
    (tui_gateway.launch_profile_policy.launch_profile_runtime_scope): no token anywhere and no claude_config_dir,
    the login lives in ~/.claude. The turn runs on it, isolated or not."""
    monkeypatch.delenv(TOKEN, raising=False)
    try:
        from tui_gateway.launch_profile_policy import launch_profile_runtime_scope
    except ImportError:
        pytest.skip("core without launch_profile_runtime_scope")
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from hermes_constants import get_routing_process_hermes_home
    if isolated:
        _isolate()
    client, seen = _native(tmp_path, monkeypatch)
    mode = set_multiplex_context(True)
    try:
        with launch_profile_runtime_scope(get_routing_process_hermes_home()):
            assert client.create(**REQUEST).choices[0].message.content == "hello\n"
    finally:
        reset_multiplex_context(mode)
        client.close()
    assert seen() == [None]


@contextmanager
def _own_cron_fire():
    """The launch profile's own job fired in process by a multiplexing gateway, bound by core's own
    cron/scheduler.py _fire_secret_scope: its scope is stamped with the process home, as a multiplexing messaging
    gateway stamps that profile's own turns (gateway/run.py _profile_runtime_scope)."""
    try:
        from cron.scheduler import _fire_secret_scope
    except ImportError:
        pytest.skip("core without cron.scheduler._fire_secret_scope")
    from agent.secret_scope import current_secret_scope_home, reset_multiplex_context, set_multiplex_context
    mode = set_multiplex_context(True)
    try:
        with _fire_secret_scope():
            assert current_secret_scope_home() is not None  # stamped: the unstamped launch-profile case misses it
            yield
    finally:
        reset_multiplex_context(mode)


@pytest.mark.parametrize("isolated", [False, True], ids=["shared", "isolated"])
def test_the_launch_profiles_stamped_tasks_keep_its_login(profile, tmp_path, monkeypatch, isolated):
    """The messaging-gateway case 0.4.0 refused (erosika on #420, thebergerking91 on #409): the launch profile's own
    stamped tasks run on the inherited ~/.claude login without any setting, isolated or not."""
    monkeypatch.delenv(TOKEN, raising=False)
    if isolated:
        _isolate()
    client, seen = _native(tmp_path, monkeypatch)
    try:
        with _own_cron_fire():
            assert _apply({"UNRELATED": "kept"}) == {"UNRELATED": "kept"}
            assert client.create(**REQUEST).choices[0].message.content == "hello\n"
    finally:
        client.close()
    assert seen() == [None]


@contextmanager
def _process_started_for(home):
    """This process as one started for ``home`` (a restart-safe cron worker, or a Desktop backend for that profile):
    core pins it as the routing process home."""
    import hermes_constants
    if not hasattr(hermes_constants, "pin_process_hermes_home"):
        pytest.skip("core without hermes_constants.pin_process_hermes_home")
    previous = getattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None)
    hermes_constants.pin_process_hermes_home(home)
    try:
        yield
    finally:
        hermes_constants.pin_process_hermes_home(previous)


@contextmanager
def _served_turn(home, scope=None, stamp=None):
    """A task of the profile at ``home`` (a stand-in for core's binders): home override plus a scope stamped with it
    (or with ``stamp``)."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        with multiplexed(scope or {}, home=str(stamp or home)):
            yield
    finally:
        reset_hermes_home_override(token)


def _profile_home(tmp_path, name, **settings):
    home = tmp_path / name
    home.mkdir()
    if settings:
        _settings(home, **settings)
    else:
        (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    return home


def test_a_served_profile_cannot_switch_isolation_for_itself(profile, tmp_path, monkeypatch):
    """Isolation is the install owner's: a served profile's own ``false`` does not lift the root's ``true``, and its
    own ``true`` does not isolate an install whose owner left it off."""
    import directsdk_setup
    from hermes_constants import get_hermes_home
    monkeypatch.delenv(TOKEN, raising=False)
    lifted = _profile_home(tmp_path, "lifted", isolate_profile_logins=False)
    raised = _profile_home(tmp_path, "raised", isolate_profile_logins=True)
    _isolate()
    with _served_turn(lifted), pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
        _apply({"UNRELATED": "kept"})
    _settings(get_hermes_home())
    with _served_turn(raised):
        assert _apply({"UNRELATED": "kept"}) == {"UNRELATED": "kept"}


def test_refusal_names_both_owner_config_locations(profile, tmp_path):
    import directsdk_setup

    launch = _profile_home(tmp_path, "named-launch", isolate_profile_logins=True)
    served = _profile_home(tmp_path, "served")
    with _process_started_for(launch), _served_turn(served):
        with pytest.raises(directsdk_setup.ClaudeCodeLoggedOut) as raised:
            _apply({})
    assert "the install's default profile and the profile Hermes was started for" in str(raised.value)


def test_the_profile_the_process_was_started_for_can_isolate(profile, tmp_path, monkeypatch):
    """A gateway started for profile X (``hermes -p X gateway``) reads X's switch as well as the root's: X's owner can
    isolate the profiles X serves. X's own tasks still keep X's login."""
    import directsdk_setup
    monkeypatch.delenv(TOKEN, raising=False)
    launch = _profile_home(tmp_path, "launch", isolate_profile_logins=True)
    served = _profile_home(tmp_path, "served")
    with _process_started_for(launch):
        with _served_turn(served), pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
            _apply({"UNRELATED": "kept"})
        with _served_turn(launch):
            assert _apply({"UNRELATED": "kept"}) == {"UNRELATED": "kept"}


@pytest.mark.parametrize("value, isolated", [(True, True), ("true", True), ("on", True), (False, False),
                                             ("no", False), (None, False)])
def test_isolation_values(profile, monkeypatch, value, isolated):
    """A restricting switch: a value that reads as on isolates (failing toward the guard), anything else shares."""
    import directsdk_setup
    from hermes_constants import get_hermes_home
    _settings(get_hermes_home(), isolate_profile_logins=value)
    assert directsdk_setup.isolate_profile_logins() is isolated


def test_an_unreadable_owner_config_isolates(profile, monkeypatch, caplog):
    """If the owner's config can't be read, the guard holds (warned once per home)."""
    import directsdk_setup
    import hermes_cli.config
    monkeypatch.setattr(directsdk_setup, "_unreadable_isolation_homes", set())

    def unreadable():
        raise OSError("fixture config is unreadable")
    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", unreadable)
    caplog.set_level(logging.WARNING)
    assert directsdk_setup.isolate_profile_logins() is True
    assert directsdk_setup.isolate_profile_logins() is True
    assert caplog.text.count("isolate_profile_logins") == 1


def test_a_managed_true_isolates(profile, tmp_path, monkeypatch):
    """An administrator's managed config is the owner's policy too: its ``true`` isolates the install."""
    import directsdk_setup
    from hermes_cli import managed_scope
    managed = _profile_home(tmp_path, "managed", isolate_profile_logins=True)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        assert directsdk_setup.isolate_profile_logins() is True
    finally:
        monkeypatch.delenv("HERMES_MANAGED_DIR")
        managed_scope.invalidate_managed_cache()


def test_a_backend_started_for_another_profile_follows_the_install_policy(profile, tmp_path, monkeypatch):
    """A Desktop backend started for profile X runs profile Y's job (Run now): Y is served. Shared by default;
    refused under isolation."""
    import directsdk_setup
    monkeypatch.delenv(TOKEN, raising=False)
    served = _profile_home(tmp_path, "served-home")
    other = _profile_home(tmp_path, "other-home")
    with _process_started_for(other), _served_turn(served):
        assert _apply({"UNRELATED": "kept"}) == {"UNRELATED": "kept"}
    _isolate()
    with _process_started_for(other), _served_turn(served), pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
        _apply({"UNRELATED": "kept"})


def test_core_cron_worker_of_an_isolated_install_refuses_a_profile_without_a_login(profile, tmp_path, monkeypatch):
    """Core's restart-safe worker process runs with the fired profile's home as its process home, whichever gateway
    started it, so a stamp equal to it doesn't show the inherited login is that profile's. Core's real worker entry
    (cron/scheduler.py _run_external_worker_payload) binds the scope; the execution claim
    (cron.executions.adopt_claimed_execution) and the job body (run_one_job) are stubs."""
    import directsdk_setup
    try:
        from cron import executions, scheduler
    except ImportError:
        pytest.skip("core without cron.scheduler / cron.executions")
    for module, name in ((scheduler, "_run_external_worker_payload"), (executions, "adopt_claimed_execution")):
        if not hasattr(module, name):
            pytest.skip("core without %s.%s" % (module.__name__, name))
    monkeypatch.delenv(TOKEN, raising=False)
    from hermes_constants import get_hermes_home
    _isolate()
    (get_hermes_home() / "profiles").mkdir()
    fired = _profile_home(get_hermes_home() / "profiles", "fired")  # core's layout: <root>/profiles/<name>
    monkeypatch.setenv("HERMES_HOME", str(fired))  # the worker's env: HERMES_HOME is the fired profile's
    payload, ack = tmp_path / "payload.json", tmp_path / "exec-1.ready"
    payload.write_text(json.dumps({"job": {"id": "job-1", "execution_id": "exec-1"}, "profile_home": str(fired),
                                   "multiplex_active": True}), encoding="utf-8")
    outcome = []

    def run(*_args, **_kwargs):
        try:
            _apply({"UNRELATED": "kept"})
            outcome.append("kept the inherited login")
        except directsdk_setup.ClaudeCodeLoggedOut:
            outcome.append("refused")
        return True

    monkeypatch.setattr(executions, "adopt_claimed_execution",
                        lambda execution_id: {"id": execution_id, "status": "running"})
    monkeypatch.setattr(scheduler, "run_one_job", run)
    assert scheduler._run_external_worker_payload(payload, ack) is True
    assert outcome == ["refused"]


INHERITED = {TOKEN: AMBIENT, "ANTHROPIC_CUSTOM_HEADERS": "x-inherited: 1", "CLAUDE_CONFIG_DIR": "/inherited-login"}
OWN_LOGINS = {"scoped-dir": ({"CLAUDE_CONFIG_DIR": "/profile-login"}, {}),
              "scoped-token": ({TOKEN: PROFILE}, {}),
              "setting-dir": ({}, {"claude_config_dir": "/profile-login"})}


@pytest.mark.parametrize("path", ["unstamped", "process-stamp", "shared"])
@pytest.mark.parametrize("own", sorted(OWN_LOGINS))
def test_a_profile_with_its_own_login_gets_exactly_that_login(profile, monkeypatch, path, own):
    """Inheriting only replaces the refusal. A profile with a token or directory of its own gets exactly what an
    isolated served profile with that login gets; it never falls back to the inherited token, headers or
    directory."""
    from hermes_constants import get_hermes_home
    monkeypatch.delenv(TOKEN, raising=False)
    home = get_hermes_home()
    scope, settings = OWN_LOGINS[own]
    stamp = {"unstamped": None, "process-stamp": str(home), "shared": SERVED_HOME}[path]
    owner = {} if path == "shared" else {"isolate_profile_logins": True}
    _isolate(**settings)
    with multiplexed(scope):
        served = _apply(dict(INHERITED))
    assert served.get(TOKEN) != AMBIENT and "ANTHROPIC_CUSTOM_HEADERS" not in served
    _settings(home, **owner, **settings)
    with multiplexed(scope, home=stamp):
        assert _apply(dict(INHERITED)) == served


@pytest.mark.parametrize("stamped", [False, True], ids=["unstamped", "process-stamp"])
@pytest.mark.parametrize("worker", ["env-marker", "worker-main"])
def test_the_launch_exemption_never_applies_inside_cores_cron_worker(profile, monkeypatch, stamped, worker):
    """Inside core's restart-safe cron worker (its env marker, or ``python -m cron.scheduler`` as __main__) an
    isolated install exempts no task as the launch profile's."""
    import directsdk_setup
    from hermes_constants import get_hermes_home
    monkeypatch.delenv(TOKEN, raising=False)
    _isolate()
    stamp = str(get_hermes_home()) if stamped else None
    with multiplexed({}, home=stamp):
        assert _apply({"UNRELATED": "kept"}) == {"UNRELATED": "kept"}
    if worker == "env-marker":
        monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", "exec-1")
    else:
        monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__spec__=SimpleNamespace(name="cron.scheduler")))
    with multiplexed({}, home=stamp), pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
        _apply({"UNRELATED": "kept"})


def test_the_unstamped_exemption_needs_multiplexing(profile, tmp_path, monkeypatch):
    """Outside multiplexing an unstamped scope under another profile's home override (a per-profile backend) is a
    served profile as before, and an isolated install refuses it without a login of its own."""
    import directsdk_setup
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    monkeypatch.delenv(TOKEN, raising=False)
    _isolate()
    override = set_hermes_home_override(_profile_home(tmp_path, "served"))
    bound = set_secret_scope({})
    try:
        with pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
            _apply({"UNRELATED": "kept"})
    finally:
        reset_secret_scope(bound)
        reset_hermes_home_override(override)


def test_a_profile_dir_naming_the_default_login_is_not_exported(profile, tmp_path, monkeypatch):
    """thebergerking91 on #409: on macOS Claude Code keys its Keychain login by config directory, so an exported
    CLAUDE_CONFIG_DIR=~/.claude reads as logged out. A profile's own directory that is the CLI default stays
    unexported; any other is exported."""
    monkeypatch.delenv(TOKEN, raising=False)
    _isolate()
    home = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    with multiplexed({"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude")}):
        assert _apply({**home, "CLAUDE_CONFIG_DIR": "/launch-login"}) == home
    with multiplexed({"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-work")}):
        assert _apply(dict(home)) == {**home, "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-work")}


