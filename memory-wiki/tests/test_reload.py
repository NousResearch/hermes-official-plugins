"""Reload the cached backend without remounting routes or writing session data."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def plugin_entrypoint():
    path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("memory_wiki_reload_entrypoint", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_register_only_evicts_own_core_and_is_idempotent(
    plugin_entrypoint, plugin_api, hermes_home, monkeypatch
):
    class NoContributions:
        def __getattr__(self, name):
            raise AssertionError(f"register must not use context contributions: {name}")

    core_name = plugin_api._CORE_MODULE
    monkeypatch.setitem(sys.modules, core_name, ModuleType(core_name))
    for name in (
        "hermes_dashboard_plugin_other_core",
        core_name + "_unrelated",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    expected = dict(sys.modules)
    expected.pop(core_name)

    for _ in range(2):
        assert plugin_entrypoint.register(NoContributions()) is None
        assert core_name not in sys.modules
        assert dict(sys.modules) == expected


def test_existing_router_uses_fresh_core_after_register(
    plugin_entrypoint, plugin_api, hermes_home, monkeypatch
):
    from fastapi import FastAPI
    from hermes_state import SessionDB
    from starlette.testclient import TestClient

    database = hermes_home / "state.db"
    db = SessionDB(db_path=database)
    try:
        db.create_session("chinese-reload", "cli")
        db.append_message("chinese-reload", "user", "查看示例文档")
    finally:
        db.close()
    before_bytes = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns

    stale_core = ModuleType(plugin_api._CORE_MODULE)
    stale_queries = []

    def old_subjects(db, *, limit, query):
        stale_queries.append(query)
        return []

    stale_core.build_memory_subjects = old_subjects
    monkeypatch.setitem(sys.modules, plugin_api._CORE_MODULE, stale_core)
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/memory-wiki")
    mounted_routes = tuple(app.routes)
    with TestClient(app) as client:
        url = "/api/plugins/memory-wiki/subjects"
        before = client.get(url, params={"q": "文档"})
        assert before.status_code == 200
        assert before.json()["subjects"] == []
        assert stale_queries == ["文档"]

        assert plugin_entrypoint.register(None) is None

        after = client.get(url, params={"q": "文档"})
        assert after.status_code == 200
        subjects = after.json()["subjects"]
        assert subjects, "existing router still uses the stale core after register"
        assert any(
            session["id"] == "chinese-reload"
            for subject in subjects
            for session in subject["sessions"]
        )
        assert stale_queries == ["文档"]
        assert sys.modules[plugin_api._CORE_MODULE] is not stale_core
        assert tuple(app.routes) == mounted_routes

    assert database.read_bytes() == before_bytes
    assert database.stat().st_mtime_ns == before_mtime
