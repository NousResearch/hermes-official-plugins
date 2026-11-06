"""An Anthropic API credential Hermes holds for its own ``anthropic`` provider never reaches Claude Code.

Hermes loads ``~/.hermes/.env`` into its process, so ``ANTHROPIC_API_KEY`` set for the ``anthropic``
provider is inherited by this plugin too. Claude Code would sign in with that key and bill API usage
instead of the subscription; the turn path refused it instead, so picking a subscription model failed
(and Hermes fell back to another provider) whenever the same Hermes also used the API-key provider.

Each test reads the environment the fake CLI actually received.
"""
import sys

import pytest

from test_directsdk import FAKE
from test_directsdk_telemetry import CAPTURE, _captured, _discovery_envs, _turn_env

CREDENTIALS = {
    "ANTHROPIC_API_KEY": "sk-ant-api-test",
    "ANTHROPIC_AUTH_TOKEN": "bearer-test",
    "ANTHROPIC_FOUNDRY_API_KEY": "foundry-test",
}


def _inherited_turn_env(profile, tmp_path, monkeypatch):
    """A turn as the gateway runs it: no explicit env, so the child inherits Hermes' own process env."""
    for key, value in CREDENTIALS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ENV_CAPTURE", str(tmp_path / "env.jsonl"))
    script = tmp_path / "native.py"
    script.write_text(FAKE.replace("rows=[]", CAPTURE + "rows=[]"))
    client = profile.create_client(command=[sys.executable, str(script)])
    try:
        client.create(model="sonnet", messages=[{"role": "user", "content": "go"}],
                      tools=[{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                                               "parameters": {"type": "object", "properties": {}}}}])
    finally:
        client.close()
    (env,) = _captured(tmp_path)
    return env


@pytest.mark.parametrize("child", ["discovery", "turn"])
def test_an_inherited_api_credential_never_reaches_the_child(profile, tmp_path, monkeypatch, child):
    """The turn used to raise on the inherited key; discovery passed it to `claude auth status`."""
    envs = (_discovery_envs(profile, tmp_path, **CREDENTIALS) if child == "discovery"
            else [_inherited_turn_env(profile, tmp_path, monkeypatch)])
    for env in envs:
        leaked = sorted(k for k in CREDENTIALS if k in env)
        assert leaked == [], f"API credential reached the child: {leaked}"


def test_an_explicit_env_keeps_its_api_credential(profile, tmp_path):
    """`Client(env=...)` is a controlled fixture (the offline evals log native in with a fixture key)."""
    env = _turn_env(profile, tmp_path, **CREDENTIALS)
    assert {k: env.get(k) for k in CREDENTIALS} == CREDENTIALS
