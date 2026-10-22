"""Real discovery: the plugin, installed into a temp HERMES_HOME and enabled in its config.yaml, is
loaded by core's PluginManager, extends the real ``delegate_task`` schema, and its routes reach child
construction through core's real credential resolution. Only child construction and the child run are
stubbed, to read what they were handed.
"""

from __future__ import annotations

import importlib
import json
import shutil
import types

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

import hermes_yaml as yaml  # noqa: E402

PLUGIN = "delegation-model-map"
OPTIONS = [
    {"name": "local", "description": "Runs on this machine.", "base_url": "http://localhost:9/v1",
     "model": "local-model", "api_key": "local-key"},
    {"name": "deep", "description": "Slow and careful.", "provider": "openrouter", "model": "deep-model",
     "reasoning_effort": "high"},
]
#: A route that offers a fast tier (the same gate as /fast), and one that does not.
CODEX_FAST = {"name": "codex", "description": "Codex, priority tier.", "model": "gpt-6-astra",
              "base_url": "https://chatgpt.com/backend-api/codex", "api_key": "codex-key", "service_tier": "fast"}
LOCAL_FAST = {"name": "local-fast", "description": "Local, asking for a tier it lacks.",
              "base_url": "http://localhost:9/v1", "model": "local-model", "api_key": "local-key", "service_tier": "fast"}


def _delegation(**settings):
    return {"backend": "model_map", "backends": {"model_map": settings}}


def _load(tmp_path, monkeypatch, plugin_root, delegation):
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager

    home = tmp_path / "os-home" / ".hermes"  # HOME/HERMES_HOME come from the conftest fixture
    shutil.copytree(plugin_root, home / "plugins" / PLUGIN,
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"enabled": [PLUGIN]}, "delegation": delegation}), encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    manager = PluginManager()
    manager.discover_and_load()
    return manager


@pytest.fixture
def loaded(tmp_path, monkeypatch, plugin_root):
    manager = _load(tmp_path, monkeypatch, plugin_root, _delegation(options=OPTIONS))
    try:
        yield
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
    return types.SimpleNamespace(session_id="root", model="parent-model", provider="nous",
                                 tool_progress_callback=None, _delegate_spinner=None, request_overrides={},
                                 _safe_print=lambda line: None)


def _definition():
    from tools.registry import registry

    importlib.import_module("tools.delegate_tool")  # importing it registers delegate_task
    return registry.get_definitions({"delegate_task"})[0]["function"]


def test_discovered_plugin_is_the_active_backend_and_extends_the_schema(loaded):
    from agent.delegation_registry import get_active_provider
    from tools.delegate_tool_config import _load_config

    assert get_active_provider(_load_config()).name == "model_map"
    definition = _definition()
    task_props = definition["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert task_props["option"]["enum"] == ["local", "deep"]
    assert "deep: Slow and careful." in task_props["option"]["description"]
    assert "`option`" in definition["description"]
    assert "localhost" not in json.dumps(definition) and "local-key" not in json.dumps(definition)
    assert json.dumps(_definition()) == json.dumps(definition)  # byte-stable within a session


def test_each_option_reaches_child_construction_on_its_route(loaded, children):
    from tools.delegate_tool import delegate_task

    out = json.loads(delegate_task(tasks=[
        {"goal": "grep the local repository", "option": "local"},
        {"goal": "design the migration plan", "option": "deep"},
        {"goal": "a task with no option set"},
    ], parent_agent=_parent()))
    assert [r["status"] for r in out["results"]] == ["completed"] * 3
    (local, local_child), (deep, deep_child), (plain, plain_child) = children
    assert (local["model"], local["override_base_url"], local["override_api_key"]) == (
        "local-model", "http://localhost:9/v1", "local-key")
    assert (deep["model"], deep["override_provider"], deep["override_api_key"]) == (
        "deep-model", "openrouter", "openrouter-key")
    assert deep["override_reasoning_effort"] == "high"
    # Core reports these as backend-routed (the result's `route` starts from this label).
    assert (local_child._delegate_route, deep_child._delegate_route) == ({"label": "local"}, {"label": "deep"})
    # No option and no default: today's delegation route (inherit the parent).
    assert plain["model"] is None and plain["override_provider"] is None and plain["override_base_url"] is None
    assert plain["override_service_tier"] is None and not hasattr(plain_child, "_delegate_route")


def test_unknown_option_is_refused_before_any_child_is_built(loaded, children):
    from tools.delegate_tool import delegate_task

    out = json.loads(delegate_task(tasks=[{"goal": "a task naming an unknown option", "option": "huge"}],
                                   parent_agent=_parent()))
    assert "must be one of local, deep" in out["error"] and not children


def _delegate_with(tmp_path, monkeypatch, plugin_root, delegation, tasks):
    from tools.delegate_tool import delegate_task

    manager = _load(tmp_path, monkeypatch, plugin_root, delegation)
    try:
        return json.loads(delegate_task(tasks=tasks, parent_agent=_parent()))
    finally:
        manager.unload()


def test_configured_default_routes_tasks_without_an_option(tmp_path, monkeypatch, plugin_root, children):
    _delegate_with(tmp_path, monkeypatch, plugin_root, _delegation(options=OPTIONS, default="local"),
                   [{"goal": "a task with no option set"}])
    ((built, child),) = children
    assert (built["model"], built["override_base_url"], child._delegate_route) == (
        "local-model", "http://localhost:9/v1", {"label": "local"})


def test_an_options_service_tier_reaches_the_child_after_cores_tier_check(tmp_path, monkeypatch, plugin_root,
                                                                         children):
    out = _delegate_with(tmp_path, monkeypatch, plugin_root, _delegation(options=[*OPTIONS, CODEX_FAST]),
                         [{"goal": "a task on the priority tier", "option": "codex"},
                          {"goal": "a sibling on the normal tier", "option": "local"}])
    assert [r["status"] for r in out["results"]] == ["completed", "completed"]
    (codex, codex_child), (local, local_child) = children
    assert codex["override_service_tier"] == "priority"
    assert codex_child.request_overrides["service_tier"] == "priority"  # re-applied after the child is built
    assert local["override_service_tier"] is None
    assert "service_tier" not in (getattr(local_child, "request_overrides", None) or {})


def test_a_tier_the_options_route_does_not_offer_refuses_the_spawn(tmp_path, monkeypatch, plugin_root, children):
    out = _delegate_with(tmp_path, monkeypatch, plugin_root, _delegation(options=[*OPTIONS, LOCAL_FAST]),
                         [{"goal": "a task asking for a tier its route lacks", "option": "local-fast"}])
    assert "service_tier 'fast' is not available" in out["error"] and not children


def test_settings_at_the_old_location_are_not_read(tmp_path, monkeypatch, plugin_root, children):
    manager = _load(tmp_path, monkeypatch, plugin_root,
                    {"backend": "model_map", "model_map": OPTIONS, "model_map_default": "local"})
    try:
        task_props = _definition()["parameters"]["properties"]["tasks"]["items"]["properties"]
        from tools.delegate_tool import delegate_task
        delegate_task(tasks=[{"goal": "a task with no option set"}], parent_agent=_parent())
    finally:
        manager.unload()
    assert "option" not in task_props
    ((plain, _),) = children
    assert plain["model"] is None and plain["override_base_url"] is None  # today's route, not the old default
