"""The push plugin's `GET /sessions/silent`: which automated runs ended on a silence marker."""
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from push_plugin import load_dashboard_api


def load_api():
    return load_dashboard_api("hermes_mobile_push_dashboard_api")


def make_store(path, sessions):
    """`sessions` maps an id to `(source, [(role, content), …], recency)`."""
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL,
                               last_activity_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                               role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL,
                               active INTEGER NOT NULL DEFAULT 1);
    """)
    for session_id, (source, messages, recency) in sessions.items():
        connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (session_id, source, recency, recency))
        for role, content in messages:
            connection.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                               (session_id, role, content, recency))
    connection.commit()
    connection.close()


RUNS = {
    "cron_bills_1": ("cron", [("user", "Sort bills"), ("assistant", "[SILENT]")], 10),
    "cron_bills_2": ("cron", [("user", "Sort bills"), ("assistant", "[SILENT] nothing new")], 9),
    "cron_report": ("cron", [("user", "Report"), ("assistant", "I would have said [SILENT] but found 2 bills.")], 8),
    "cron_tool": ("cron", [("user", "Run"), ("assistant", "[SILENT]"), ("tool", '{"output": "late"}')], 7),
    "webhook_1": ("webhook", [("user", "Event"), ("assistant", "NO_REPLY")], 6),
    "graph_1": ("msgraph_webhook", [("user", "Event"), ("assistant", "Mail from Bob")], 5),
    "chat_1": ("web", [("user", "Say nothing"), ("assistant", "NO_REPLY")], 4),
    "cron_empty": ("cron:daily", [], 3),
}


@pytest.fixture
def api_without_hermes(monkeypatch):
    # No `gateway` package: the plugin falls back to the original sentinel.
    monkeypatch.setitem(sys.modules, "gateway", None)
    monkeypatch.setitem(sys.modules, "gateway.response_filters", None)
    return load_api()


@pytest.fixture
def api_with_hermes_rule(monkeypatch):
    filters = types.ModuleType("gateway.response_filters")
    markers = ("[SILENT]", "SILENT", "NO_REPLY", "NO REPLY")

    def is_autonomous_silence_response(text):
        stripped = text.strip() if isinstance(text, str) else ""
        if not stripped:
            return False
        lines = [line for line in stripped.splitlines() if line.strip()]
        return stripped.upper().startswith("[SILENT]") or any(
            candidate.strip().upper() in markers for candidate in (stripped, lines[0], lines[-1]))

    filters.is_autonomous_silence_response = is_autonomous_silence_response
    gateway = types.ModuleType("gateway")
    gateway.response_filters = filters
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.response_filters", filters)
    return load_api()


def test_hermes_rule_decides_which_automated_runs_were_silent(tmp_path, api_with_hermes_rule):
    make_store(tmp_path / "state.db", RUNS)
    # A marker mid-sentence, a run that stopped on a tool row, a webhook that answered and any
    # chat (silence there is not the autonomous rule) are all left out. Newest first.
    assert api_with_hermes_rule.push_api.silent_sessions(tmp_path / "state.db") == ["cron_bills_1", "cron_bills_2", "webhook_1"]


def test_older_hermes_falls_back_to_the_bracketed_sentinel(tmp_path, api_without_hermes):
    make_store(tmp_path / "state.db", RUNS)
    assert api_without_hermes.push_api.silent_sessions(tmp_path / "state.db") == ["cron_bills_1", "cron_bills_2"]


def test_limit_takes_the_newest_runs(tmp_path, api_with_hermes_rule):
    make_store(tmp_path / "state.db", RUNS)
    assert api_with_hermes_rule.push_api.silent_sessions(tmp_path / "state.db", limit=1) == ["cron_bills_1"]


def test_rewound_rows_do_not_count(tmp_path, api_with_hermes_rule):
    make_store(tmp_path / "state.db", {"cron_x": ("cron", [("user", "Run"), ("assistant", "[SILENT]")], 1)})
    connection = sqlite3.connect(tmp_path / "state.db")
    connection.execute("INSERT INTO messages (session_id, role, content, timestamp, active) VALUES ('cron_x', 'assistant', 'Found it', 2, 0)")
    connection.commit()
    connection.close()
    assert api_with_hermes_rule.push_api.silent_sessions(tmp_path / "state.db") == ["cron_x"]


def test_missing_store_is_empty(tmp_path, api_with_hermes_rule):
    assert api_with_hermes_rule.push_api.silent_sessions(tmp_path / "missing.db") == []


def test_route_validates_the_profile_and_reads_its_store(tmp_path, monkeypatch, api_with_hermes_rule):
    profiles = types.ModuleType("hermes_cli.profiles")

    def validate_profile_name(name):
        if "/" in name or name.startswith("."):
            raise ValueError(f"Invalid profile name: {name}")

    profiles.validate_profile_name = validate_profile_name
    profiles.profile_exists = lambda name: name in ("default", "aria")
    profiles.get_profile_dir = lambda name: tmp_path if name == "default" else tmp_path / "profiles" / name
    package = types.ModuleType("hermes_cli")
    package.profiles = profiles
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles)
    (tmp_path / "profiles/aria").mkdir(parents=True)
    make_store(tmp_path / "state.db", RUNS)
    # The request is bound to the default profile; `?profile=` asks for another (older Hermes).
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    app = FastAPI()
    app.include_router(api_with_hermes_rule.router)
    client = TestClient(app)
    assert client.get("/sessions/silent").json() == {"silent": ["cron_bills_1", "cron_bills_2", "webhook_1"]}
    # A profile with no store yet has no silent runs.
    assert client.get("/sessions/silent", params={"profile": "aria"}).json() == {"silent": []}
    assert client.get("/sessions/silent", params={"profile": "missing"}).status_code == 404
    assert client.get("/sessions/silent", params={"profile": "../escape"}).status_code == 400


def test_an_unreadable_store_answers_503_without_sqlites_text(tmp_path, monkeypatch, caplog, api_with_hermes_rule):
    (tmp_path / "state.db").write_bytes(b"not a database " * 100)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    app = FastAPI()
    app.include_router(api_with_hermes_rule.router)
    response = TestClient(app).get("/sessions/silent")
    assert response.status_code == 503
    assert response.json() == {"detail": "The session store could not be read; try again."}
    assert "could not be read" in caplog.text
    assert "file is not a database" in caplog.text
