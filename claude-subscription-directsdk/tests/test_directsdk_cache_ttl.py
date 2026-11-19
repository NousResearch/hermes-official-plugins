"""A delegated child never uses the 1-hour cache tier.

Hermes core clamps a delegated child to 5m (``tools.delegate_tool._apply_child_cache_ttl``: a subagent calls every
few seconds for minutes and is gone, so the 2x write buys retention it never collects). Core can only do that to
the ``cache_control`` markers it builds, and on this route native places them: a ``claude -p`` turn is Claude
Code's "main conversation" bucket, which asks for one hour on a subscription within plan usage, so every child
request wrote its cache at 1h. Core hands the hook the child's cache scope (``sub_<hash>``); the hook turns it
into a 5m request and the spawn passes it to native as ``CLAUDE_CODE_PROMPT_CACHE_TTL`` (documented by Anthropic,
Claude Code 2.1.242+; the ``..._SUBAGENT_...`` variant only governs requests outside the main bucket).

Each turn test reads the environment the fake CLI actually received.
"""
import sys
from types import SimpleNamespace

import pytest

from test_directsdk import FAKE
from test_directsdk_telemetry import CAPTURE, _base_env, _captured

TTL = "CLAUDE_CODE_PROMPT_CACHE_TTL"
CHAT = dict(supports_reasoning=False, model="claude-opus-5-5[1m]", base_url="process://claude", session_id="child-sid")


def _child_scope():
    """The scope core's delegate_tool stamps on a child, minted by core itself."""
    from agent.prompt_cache_scope import sibling_cache_scope
    return sibling_cache_scope(SimpleNamespace(session_id="parent-sid", _session_db=None, _inherited_cache_scope=None))


def _turn_env(profile, tmp_path, monkeypatch, *, extras=None, **env):
    monkeypatch.delenv(TTL, raising=False)
    script = tmp_path / "native.py"
    script.write_text(FAKE.replace("rows=[]", CAPTURE + "rows=[]"))
    client = profile.create_client(command=[sys.executable, str(script)], env=_base_env(tmp_path, HOME=str(tmp_path), **env))
    tools = [{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                                "parameters": {"type": "object", "properties": {}}}}]
    try:
        client.create(model="sonnet", messages=[{"role": "user", "content": "go"}], tools=tools, **(extras or {}))
    finally:
        client.close()
    (captured,) = _captured(tmp_path)
    return captured


def test_the_hook_asks_for_5m_on_a_delegated_childs_requests_only(profile):
    scope = _child_scope()
    assert scope.startswith("sub_")  # core's own helper, not a string this test made up
    child = profile.build_api_kwargs_extras(reasoning_config=None, cache_scope_id=scope, **CHAT)
    parent = profile.build_api_kwargs_extras(reasoning_config=None, cache_scope_id="parent-sid", **CHAT)
    declared = profile.build_api_kwargs_extras(reasoning_config=None, cache_scope_id="gwk_" + "0" * 24, **CHAT)
    assert child == ({}, {"hermes_cache_ttl": "5m"})
    assert parent == declared == ({}, {})


def test_an_older_core_without_the_helper_is_read_by_its_prefix(profile, monkeypatch):
    """The ``sub_`` prefix is the contract; the helper is only core's name for it."""
    monkeypatch.setitem(sys.modules, "agent.prompt_cache_scope", SimpleNamespace())
    child = profile.build_api_kwargs_extras(reasoning_config=None, cache_scope_id="sub_" + "a" * 24, **CHAT)
    other = profile.build_api_kwargs_extras(reasoning_config=None, cache_scope_id="parent-sid", **CHAT)
    assert child == ({}, {"hermes_cache_ttl": "5m"}) and other == ({}, {})


def test_an_auxiliary_call_is_not_a_delegated_child(profile):
    # No conversation context at all (call_llm): marked auxiliary, and no cache scope to read.
    aux = profile.build_api_kwargs_extras(reasoning_config=None, supports_reasoning=False, model="claude-opus-5-5[1m]",
                                          base_url="process://claude")
    assert aux == ({}, {"hermes_auxiliary": True})


def test_a_child_turn_starts_native_with_a_5m_main_conversation_cache(profile, tmp_path, monkeypatch):
    env = _turn_env(profile, tmp_path, monkeypatch, extras={"hermes_cache_ttl": "5m"})
    assert env[TTL] == "5m"
    # Never the force switch (it would beat an exported choice) and never the subagent bucket's variable: a
    # `claude -p` turn is the main bucket, so that one governs nothing here.
    assert "FORCE_PROMPT_CACHING_5M" not in env and "ENABLE_PROMPT_CACHING_1H" not in env


def test_an_ordinary_turn_keeps_native_default(profile, tmp_path, monkeypatch):
    env = _turn_env(profile, tmp_path, monkeypatch)
    assert TTL not in env


def test_a_tier_the_user_exported_wins_on_a_child_too(profile, tmp_path, monkeypatch):
    env = _turn_env(profile, tmp_path, monkeypatch, extras={"hermes_cache_ttl": "5m"}, **{TTL: "1h"})
    assert env[TTL] == "1h"


@pytest.mark.parametrize("value", ["1h", "30m", "", None])
def test_only_5m_is_ever_acted_on(profile, tmp_path, monkeypatch, value):
    env = _turn_env(profile, tmp_path, monkeypatch, extras={"hermes_cache_ttl": value})
    assert TTL not in env
