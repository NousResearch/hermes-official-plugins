"""Real discovery: the plugin, installed into a temp HERMES_HOME and enabled in its config.yaml, is
loaded by core's PluginManager, extends the real ``delegate_task`` schema, and its routes reach child
construction. Only child construction and the child run are stubbed, to read what they were handed.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import types

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

import hermes_yaml as yaml  # noqa: E402

PLUGIN = "delegation-profiles"
PROFILES = [
    {"name": "coder", "description": "Writes and refactors code."},
    {"name": "ghost", "description": "Listed, but no such profile exists."},
]


def _write_profile(home, *, model, key):
    # A hosted endpoint, so the key must come from the profile's own .env, never the launch env.
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "custom", "base_url": f"https://{model}.invalid/v1", "default": model,
                  "key_env": "PROFILE_KEY"},
        "agent": {"reasoning_effort": "low"},
    }), encoding="utf-8")
    (home / ".env").write_text(f"PROFILE_KEY={key}\n", encoding="utf-8")


@pytest.fixture
def loaded(tmp_path, monkeypatch, plugin_root):
    """Launch profile + a ``coder`` profile, the plugin installed and enabled, discovery run."""
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager

    launch = tmp_path / "os-home" / ".hermes"  # HOME/HERMES_HOME come from the conftest fixture
    _write_profile(launch / "profiles" / "coder", model="coder-model", key="coder-key")
    shutil.copytree(plugin_root, launch / "plugins" / PLUGIN,
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    (launch / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": [PLUGIN]},
        "delegation": {"backend": "profiles", "profiles": PROFILES},
    }), encoding="utf-8")
    monkeypatch.setenv("PROFILE_KEY", "launch-key")  # the launch profile's secret, as after a dotenv load
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    manager = PluginManager()
    manager.discover_and_load()
    try:
        yield launch
    finally:
        manager.unload()


@pytest.fixture
def children(monkeypatch):
    """Record every child core builds (kwargs + the child object); children complete at once."""
    from tools import delegate_tool as dt
    built = []

    def build(**kwargs):
        child = types.SimpleNamespace(tool_progress_callback=None, model=kwargs.get("model"))
        built.append((kwargs, child))
        return child

    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(dt, "_run_single_child", lambda task_index, goal, child=None, parent_agent=None, **kw: {
        "task_index": task_index, "status": "completed", "summary": "ok", "api_calls": 1, "duration_seconds": 0})
    return built


def _parent():
    return types.SimpleNamespace(session_id="root", model="parent-model", provider="openrouter",
                                 tool_progress_callback=None, _delegate_spinner=None, request_overrides={},
                                 _safe_print=lambda line: None)


def _definition():
    from tools.registry import registry

    importlib.import_module("tools.delegate_tool")  # importing it registers delegate_task
    return registry.get_definitions({"delegate_task"})[0]["function"]


def test_discovered_plugin_is_the_active_backend_and_extends_the_schema(loaded):
    from agent.delegation_registry import get_active_provider
    from tools.delegate_tool_config import _load_config

    assert get_active_provider(_load_config()).name == "profiles"
    definition = _definition()
    profile = definition["parameters"]["properties"]["tasks"]["items"]["properties"]["profile"]
    assert profile["enum"] == ["coder", "ghost"]
    assert "coder: Writes and refactors code." in profile["description"]
    assert "`profile`" in definition["description"]
    assert json.dumps(_definition()) == json.dumps(definition)  # byte-stable within a session


def test_profile_task_is_built_as_that_profile_and_plain_task_inherits(loaded, children):
    from tools.delegate_tool import delegate_task

    out = json.loads(delegate_task(tasks=[
        {"goal": "refactor the parser module", "profile": "coder"},
        {"goal": "a task with no profile set"},
    ], parent_agent=_parent()))
    assert [r["status"] for r in out["results"]] == ["completed", "completed"]
    (coder, coder_child), (plain, plain_child) = children
    assert os.path.samefile(coder["profile_home"], loaded / "profiles" / "coder")
    assert (coder["model"], coder["override_base_url"], coder["override_api_key"]) == (
        "coder-model", "https://coder-model.invalid/v1", "coder-key")
    # The target profile's own reasoning, resolved by core to a final value (never the parent's level).
    assert coder["override_reasoning_effort"].config == {"enabled": True, "effort": "low"}
    assert coder_child._delegate_route_label == "coder"
    # No profile and no profiles_default: today's delegation route (inherit the parent).
    assert plain["profile_home"] is None and plain["model"] is None and plain["override_provider"] is None
    assert not hasattr(plain_child, "_delegate_route_label")


def test_out_of_list_or_missing_profiles_are_refused_before_any_child_is_built(loaded, children):
    from tools.delegate_tool import delegate_task

    typo = json.loads(delegate_task(tasks=[{"goal": "a task naming an unlisted profile", "profile": "admin"}],
                                    parent_agent=_parent()))
    assert "must be one of coder, ghost" in typo["error"]
    ghost = json.loads(delegate_task(tasks=[{"goal": "a task naming a listed but absent profile",
                                             "profile": "ghost"}], parent_agent=_parent()))
    assert "hermes profile list" in ghost["error"]
    assert not children
