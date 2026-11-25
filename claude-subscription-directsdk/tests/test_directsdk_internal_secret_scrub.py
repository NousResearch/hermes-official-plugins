"""#514: the credential names a Hermes subprocess never carries never reach a Claude Code child.

Hermes loads the launch profile's ``.env`` into its process, and a multiplexed gateway serves every
profile from it: any other credential in that environment - a custom endpoint's ``key_env``,
``HERMES_CUSTOM_*`` keys, ``GATEWAY_RELAY_*`` auth, Tier-1 infrastructure tokens, other providers'
keys - would reach every child this plugin spawns. The turn and setup spawn paths scrub with core's
subprocess rules (``tools/environments/local_env_policy.py``); only an explicit ``Client(env=...)``
fixture keeps its values. Each test reads the environment the fake CLI actually received.
"""
import sys

import pytest

from test_directsdk import FAKE
from test_directsdk_telemetry import CAPTURE, _captured, _discovery_envs, _turn_env

SECRETS = dict(
    HERMES_CUSTOM_RELAY_API_KEY="hk1",
    HERMES_PROVIDER_ACME_API_KEY="hk2",
    HERMES_MODEL_API_KEY="hk3",
    HERMES_DELEGATION_API_KEY="hk4",
    AUXILIARY_RESEARCH_API_KEY="hk5",
    AUXILIARY_RESEARCH_BASE_URL="https://aux.example",
    GATEWAY_RELAY_EDGE_SECRET="gs1",
    GATEWAY_RELAY_EDGE_DELIVERY_KEY="gs2",
    GITHUB_TOKEN="ght",
    TELEGRAM_BOT_TOKEN="tbt",
    HERMES_DASHBOARD_BASIC_AUTH_PASSWORD="dpw",
    OPENAI_API_KEY="oak",
)
KEPT = dict(HTTP_PROXY="http://127.0.0.1:9", MY_UNRELATED_VALUE="stay")


def _merged():
    out = dict(SECRETS)
    out.update(KEPT)
    return out


def _picked(env, names):
    return dict((k, env.get(k)) for k in names)


def _probe_tool():
    return [dict(type="function", function=dict(name="probe", description="TAIL",
                                                parameters=dict(type="object", properties=dict())))]


def _inherited_turn_env(profile, tmp_path, monkeypatch):
    """A turn as the gateway runs it: no explicit env, so the child inherits Hermes' own process env."""
    for key, value in _merged().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ENV_CAPTURE", str(tmp_path / "env.jsonl"))
    script = tmp_path / "native.py"
    script.write_text(FAKE.replace("rows=[]", CAPTURE + "rows=[]"))
    client = profile.create_client(command=[sys.executable, str(script)])
    try:
        client.create(model="sonnet", messages=[dict(role="user", content="go")], tools=_probe_tool())
    finally:
        client.close()
    (env,) = _captured(tmp_path)
    return env


@pytest.mark.parametrize("child", ["discovery", "turn"])
def test_hermes_internal_secrets_never_reach_the_child(profile, tmp_path, monkeypatch, child):
    envs = (_discovery_envs(profile, tmp_path, **_merged()) if child == "discovery"
            else [_inherited_turn_env(profile, tmp_path, monkeypatch)])
    for env in envs:
        leaked = sorted(k for k in SECRETS if k in env)
        assert leaked == [], "Hermes-internal credential reached the child: " + repr(leaked)
        assert _picked(env, KEPT) == KEPT  # proxy settings and unrelated values still pass


def test_an_explicit_env_keeps_its_values(profile, tmp_path):
    """Client(env=...) is a controlled fixture (the offline evals log native in with a fixture key)."""
    env = _turn_env(profile, tmp_path, **_merged())
    assert _picked(env, _merged()) == _merged()


def test_without_the_core_policy_module_the_env_passes_through(monkeypatch):
    """A core without the spawn-policy module keeps today's behavior instead of failing the spawn."""
    import directsdk_setup

    monkeypatch.setitem(sys.modules, "tools.environments.local_env_policy", None)
    env = dict(HERMES_CUSTOM_X_API_KEY="hx", OPENAI_API_KEY="oak", KEEP="1")
    assert directsdk_setup.scrub_hermes_internal_env(env) == env
