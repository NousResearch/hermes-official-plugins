"""#150: the host session's Claude Code identity never reaches the child.

Hermes can itself run inside Claude Code (the CLI, the desktop app, an Agent SDK host). Inherited
there, ``CLAUDE_CODE_ENTRYPOINT`` makes native inject a per-process scratchpad path - a random
UUID - into the request at ``messages[1]``. The plugin spawns a fresh CLI per request, so the
UUID changes every time: the cached prefix diverges after the first user turn and the whole
conversation is re-written as cache creation on each call.

The identity that describes the *host* session is dropped before the child starts. The account the
plugin drives still comes from ``CLAUDE_CONFIG_DIR``, and a user's own opt-outs are untouched.
"""

import pytest

from test_directsdk_telemetry import _discovery_envs, _turn_env

# The host session's identity, as a Claude Code / Agent SDK host exports it.
HOST_IDENTITY = {
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
    "CLAUDE_CODE_ENTRYPOINT_ON_REQUESTS": "1",
    "CLAUDE_CODE_SESSION_ID": "session-uuid",
    "CLAUDE_CODE_HOST_SESSION_ID": "host-session-uuid",
    "CLAUDE_CODE_CHILD_SESSION": "child-session-uuid",
    "CLAUDE_CODE_SESSION_ATTENDED": "1",
    "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/socket",
    "CLAUDE_CODE_MESSAGING_TOKEN": "token",
    "CLAUDE_CODE_DESKTOP_APP_VERSION": "1.2.3",
    "CLAUDE_CODE_EXECPATH": "/path/to/claude",
    "CLAUDE_CODE_ACCOUNT_UUID": "account-uuid",
    "CLAUDE_CODE_ORGANIZATION_UUID": "org-uuid",
    "CLAUDE_CODE_USER_EMAIL": "user@example.com",
    "CLAUDE_CODE_OAUTH_SCOPES": "user:inference",
    "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH": "1",
    "CLAUDE_PID": "1234",
    "CLAUDE_AGENT_SDK_VERSION": "0.1.0",
}


@pytest.mark.parametrize("child", ["discovery", "turn"])
def test_the_host_session_identity_never_reaches_the_child(profile, tmp_path, child):
    """#150: the inherited identity is what makes native inject a per-process scratchpad path."""
    envs = (_discovery_envs(profile, tmp_path, **HOST_IDENTITY) if child == "discovery"
            else [_turn_env(profile, tmp_path, **HOST_IDENTITY)])
    for env in envs:
        leaked = sorted(k for k in HOST_IDENTITY if k in env)
        assert leaked == [], f"host session identity reached the child: {leaked}"


@pytest.mark.parametrize("child", ["discovery", "turn"])
def test_a_users_own_settings_and_config_dir_still_reach_the_child(profile, tmp_path, child):
    """Only the host session's identity goes: the account and the opt-outs stay."""
    keep = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude-profile"), "DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}
    envs = (_discovery_envs(profile, tmp_path, **keep, **HOST_IDENTITY) if child == "discovery"
            else [_turn_env(profile, tmp_path, **keep, **HOST_IDENTITY)])
    for env in envs:
        assert env.get("CLAUDE_CONFIG_DIR") == keep["CLAUDE_CONFIG_DIR"]
        assert env.get("DISABLE_TELEMETRY") == "1"
        assert env.get("DO_NOT_TRACK") == "1"


def test_the_env_helper_drops_only_the_host_identity():
    """The helper is in-place, returns the env, and leaves everything else alone."""
    from directsdk_setup import drop_host_session_env

    env = {"CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/usr/bin", "MY_SETTING": "1"}
    result = drop_host_session_env(env)
    assert result is env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["PATH"] == "/usr/bin" and env["MY_SETTING"] == "1"


def test_the_env_helper_is_a_no_op_without_a_host_identity():
    from directsdk_setup import drop_host_session_env

    env = {"PATH": "/usr/bin"}
    assert drop_host_session_env(env) == {"PATH": "/usr/bin"}
