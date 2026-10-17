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


@pytest.fixture
def real_host_client(hermes_home, monkeypatch):
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

    return TestClient(web_server.app)


def test_real_web_server_discovers_enables_and_mounts_the_plugin(real_host_client):
    client = real_host_client
    listed = client.get("/api/dashboard/plugins").json()
    names = [p["name"] for p in (listed.get("plugins") if isinstance(listed, dict) else listed)]
    assert "memory-wiki" in names

    assert client.get("/api/plugins/memory-wiki/overview").status_code == 401, "plugin routes sit behind auth"
    ok = client.get("/api/plugins/memory-wiki/overview", headers={"X-Hermes-Session-Token": "test-token"})
    assert ok.status_code == 200
    assert any(subject["slug"] == "memory-wiki" for subject in ok.json()["subjects"])


def test_search_reuses_complete_subjects_before_filtering(client, memory_wiki_core, monkeypatch):
    calls = []
    original = memory_wiki_core.extract_subject_candidates

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(memory_wiki_core, "extract_subject_candidates", counted)
    first = client.get("/api/plugins/memory-wiki/subjects?limit=1").json()
    count = len(calls)
    result = client.get("/api/plugins/memory-wiki/subjects?q=memory-wiki&limit=1").json()
    assert result["subjects"]
    assert result["subjects"] != first["subjects"]
    assert len(calls) == count


def test_routes_do_not_block_event_loop(hermes_home, plugin_api, memory_wiki_core, monkeypatch):
    import asyncio
    import threading

    _seed_state_db(hermes_home)
    entered = threading.Event()
    release = threading.Event()
    original = memory_wiki_core.build_memory_subjects

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(2), "aggregation blocked the event loop"
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_wiki_core, "build_memory_subjects", slow)

    async def run():
        task = asyncio.create_task(plugin_api.get_subject("memory-wiki"))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            assert entered.is_set()
        finally:
            release.set()
        assert (await task)["subject"]

    asyncio.run(run())


@pytest.mark.xfail(strict=True, reason="Host does not scope plugin routes for ?profile= (pre-existing integration gap)")
def test_real_host_selected_profile(real_host_client, hermes_home):
    profile = hermes_home / "profiles" / "beta"
    profile.mkdir(parents=True)
    _seed_state_db(profile)
    (profile / "memories" / "MEMORY.md").write_text("Beta profile note")
    response = real_host_client.get(
        "/api/plugins/memory-wiki/overview?profile=beta",
        headers={"X-Hermes-Session-Token": "test-token"},
    )
    assert response.status_code == 200
    assert response.json()["notes"]["memory"] == ["Beta profile note"]
