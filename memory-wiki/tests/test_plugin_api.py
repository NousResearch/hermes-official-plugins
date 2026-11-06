"""Router tests for dashboard/plugin_api.py, driven through FastAPI's TestClient.

Two layers: the router in isolation (mounted on a bare app at the host's prefix), and one
end-to-end pass through the REAL Hermes web server — plugin copied into ``HERMES_HOME/plugins``,
enabled in config.yaml, discovered, API mounted, auth token required.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _seed_state_db(hermes_home: Path) -> None:
    import hermes_state

    db = hermes_state.SessionDB(db_path=hermes_home / "state.db")
    try:
        db.create_session("s-memory", "discord")
        db.append_message("s-memory", "user", "Build Memory Wiki dashboard in web/src/App.tsx")
        db.append_message("s-memory", "assistant", "Implemented Memory Wiki subject cards.")
        db.append_message("s-memory", "tool", "", tool_name="search_files")
        db.create_session("s-fastapi", "cli")
        db.append_message("s-fastapi", "user", "Debug FastAPI pytest failure")
        db.append_message("s-fastapi", "assistant", "Planned and fixed the FastAPI route bug.")
    finally:
        db.close()
    memories = hermes_home / "memories"
    memories.mkdir(exist_ok=True)
    (memories / "MEMORY.md").write_text("Prefers rebase merges\n§\nCI watcher is canonical\n", encoding="utf-8")


@pytest.fixture
def client(hermes_home, plugin_api):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    _seed_state_db(hermes_home)
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/memory-wiki")
    return TestClient(app)


def test_overview_reads_state_db_read_only_and_notes(client, hermes_home):
    before = (hermes_home / "state.db").stat().st_mtime_ns

    resp = client.get("/api/plugins/memory-wiki/overview")

    assert resp.status_code == 200
    data = resp.json()
    assert any(subject["slug"] == "memory-wiki" for subject in data["subjects"])
    assert data["daily_logs"] and data["recent_sessions"]
    assert data["notes"]["memory"] == ["Prefers rebase merges", "CI watcher is canonical"]
    assert data["notes"]["user_count"] == 0
    assert (hermes_home / "state.db").stat().st_mtime_ns == before, "GET must never write state.db"


def test_subjects_query_and_detail_lookups(client):
    subjects = client.get("/api/plugins/memory-wiki/subjects?q=fastapi&limit=10").json()
    assert subjects["query"] == "fastapi" and subjects["limit"] == 10
    assert any(subject["slug"] == "fastapi" for subject in subjects["subjects"])

    detail = client.get("/api/plugins/memory-wiki/subjects/memory-wiki")
    assert detail.status_code == 200
    assert detail.json()["subject"]["session_count"] >= 1
    assert client.get("/api/plugins/memory-wiki/subjects/not-a-real-subject").status_code == 404


def test_days_and_day_detail(client):
    days = client.get("/api/plugins/memory-wiki/days?limit=10").json()
    assert days["limit"] == 10 and days["daily_logs"]

    date = days["daily_logs"][0]["date"]
    day = client.get(f"/api/plugins/memory-wiki/days/{date}")
    assert day.status_code == 200
    assert day.json()["daily_log"]["work_items"]
    assert client.get("/api/plugins/memory-wiki/days/1999-01-01").status_code == 404


def test_fresh_profile_without_state_db_returns_empty_overview(hermes_home, plugin_api):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/memory-wiki")
    resp = TestClient(app).get("/api/plugins/memory-wiki/overview")

    assert resp.status_code == 200
    assert resp.json() == {
        "subjects": [], "daily_logs": [], "recent_sessions": [],
        "notes": {"memory": [], "user": [], "memory_count": 0, "user_count": 0},
    }
    assert not (hermes_home / "state.db").exists(), "a read-only plugin must not create state.db"


def test_real_web_server_discovers_enables_and_mounts_the_plugin(hermes_home, monkeypatch):
    """The dashboard host's own discovery + enable gate + API mount, with this repo installed."""
    import importlib
    import sys

    from starlette.testclient import TestClient

    plugin_dir = hermes_home / "plugins" / "hermes-memory-wiki"
    shutil.copytree(REPO_ROOT, plugin_dir, ignore=shutil.ignore_patterns(".git", "node_modules", "tests", "__pycache__"))
    (hermes_home / "config.yaml").write_text("plugins:\n  enabled:\n    - memory-wiki\n", encoding="utf-8")
    _seed_state_db(hermes_home)
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "test-token")
    # web_server discovers and mounts plugin APIs at import; make that import see this HERMES_HOME.
    for name in [m for m in list(sys.modules) if m.startswith("hermes_cli.web_server") or m.startswith("hermes_cli.web_routers")]:
        sys.modules.pop(name, None)
    sys.modules.pop("hermes_dashboard_plugin_memory-wiki", None)
    web_server = importlib.import_module("hermes_cli.web_server")

    client = TestClient(web_server.app)
    listed = client.get("/api/dashboard/plugins").json()
    names = [p["name"] for p in (listed.get("plugins") if isinstance(listed, dict) else listed)]
    assert "memory-wiki" in names

    assert client.get("/api/plugins/memory-wiki/overview").status_code == 401, "plugin routes sit behind auth"
    ok = client.get("/api/plugins/memory-wiki/overview", headers={"X-Hermes-Session-Token": "test-token"})
    assert ok.status_code == 200
    assert any(subject["slug"] == "memory-wiki" for subject in ok.json()["subjects"])


@pytest.mark.parametrize("path", ["/overview", "/subjects", "/subjects/wiki", "/days", "/days/2026-11-03"])
def test_a_slow_scan_leaves_the_host_event_loop_responsive(plugin_api, monkeypatch, path):
    """The scan runs off the loop: another request is served while it is still running (#216)."""
    import asyncio
    import threading
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI

    started, release = threading.Event(), threading.Event()

    def slow(*_args, **_kwargs):
        started.set()
        if not release.wait(2):
            raise RuntimeError("the scan blocked the event loop")
        return [{"slug": "wiki"}]

    fake_core = SimpleNamespace(build_memory_overview=slow, build_memory_subjects=slow, get_memory_subject=slow,
                                build_daily_logs=slow, get_daily_log=slow, build_memory_notes=lambda: {})
    monkeypatch.setattr(plugin_api, "_core", lambda: fake_core)
    monkeypatch.setattr(plugin_api, "_open_db", lambda: SimpleNamespace(close=lambda: None))
    app = FastAPI()
    app.include_router(plugin_api.router)

    @app.get("/ping")
    async def ping():
        release.set()
        return {"ok": True}

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            scan = asyncio.create_task(client.get(path))
            while not started.is_set() and not scan.done():
                await asyncio.sleep(0.01)
            assert (await asyncio.wait_for(client.get("/ping"), 1)).status_code == 200
            return await scan

    assert asyncio.run(run()).status_code == 200
