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
MODEL_MAP = [
    {"name": "local", "description": "Runs on this machine.", "base_url": "http://localhost:9/v1",
     "model": "local-model", "api_key": "local-key"},
    {"name": "deep", "description": "Slow and careful.", "provider": "openrouter", "model": "deep-model",
     "reasoning_effort": "high"},
]


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
    manager = _load(tmp_path, monkeypatch, plugin_root, {"backend": "model-map", "model_map": MODEL_MAP})
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

    assert get_active_provider(_load_config()).name == "model-map"
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
    assert (local_child._delegate_route_label, deep_child._delegate_route_label) == ("local", "deep")
    # No option and no model_map_default: today's delegation route (inherit the parent).
    assert plain["model"] is None and plain["override_provider"] is None and plain["override_base_url"] is None
    assert not hasattr(plain_child, "_delegate_route_label")


def test_unknown_option_is_refused_before_any_child_is_built(loaded, children):
    from tools.delegate_tool import delegate_task

    out = json.loads(delegate_task(tasks=[{"goal": "a task naming an unknown option", "option": "huge"}],
                                   parent_agent=_parent()))
    assert "must be one of local, deep" in out["error"] and not children


def test_model_map_default_routes_tasks_without_an_option(tmp_path, monkeypatch, plugin_root, children):
    from tools.delegate_tool import delegate_task

    manager = _load(tmp_path, monkeypatch, plugin_root,
                    {"backend": "model-map", "model_map": MODEL_MAP, "model_map_default": "local"})
    try:
        delegate_task(tasks=[{"goal": "a task with no option set"}], parent_agent=_parent())
    finally:
        manager.unload()
    ((built, child),) = children
    assert (built["model"], built["override_base_url"], child._delegate_route_label) == (
        "local-model", "http://localhost:9/v1", "local")
