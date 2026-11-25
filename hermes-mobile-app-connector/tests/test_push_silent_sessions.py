"""The push plugin's `GET /sessions/silent`: which automated runs ended on a silence marker."""
from contextlib import contextmanager
import os
import re
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from push_plugin import load_dashboard_api


def load_api():
    return load_dashboard_api("hermes_mobile_push_dashboard_api")


RUNS = {
    "cron_bills_1": ("cron", [("user", "Sort bills"), ("assistant", "[SILENT]")], 10),
    "cron_bills_2": ("cron", [("user", "Sort bills"), ("assistant", "[SILENT] nothing new")], 9),
    "cron_report": ("cron", [("user", "Report"), ("assistant", "I would have said [SILENT] but found 2 bills.")], 8),
    "cron_tool": ("cron", [("user", "Run"), ("assistant", "[SILENT]"), ("tool", '{"output": "late"}')], 7),
    "webhook_1": ("webhook", [("user", "Event"), ("assistant", "NO_REPLY")], 6),
    "graph_1": ("msgraph_webhook", [("user", "Event"), ("assistant", "Mail from Bob")], 5),
    "chat_1": ("web", [("user", "Say nothing"), ("assistant", "NO_REPLY")], 4),
    "cron_blank": ("cron", [("user", "Run"), ("assistant", "[SILENT]"), ("assistant", "   ")], 3),
    "cron_empty": ("cron", [], 2),
}


class _FakePersistenceError(Exception):
    pass


class _FakeStoreRegistry:
    """In-memory stand-in for Hermes' published storage interface (runs in stubbed mode too)."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.stores: dict[Path, dict] = {}
        self.broken: dict[Path, Exception] = {}

    def seed(self, home: Path, sessions: dict):
        """`sessions` maps an id to `(source, [(role, content[, active]), ...], recency)`."""
        self.stores[Path(home).resolve()] = sessions

    def break_store(self, home: Path, exc: Exception):
        key = Path(home).resolve()
        self.stores.setdefault(key, {})
        self.broken[key] = exc

    def _resolve_scope(self, profile=None, *, home=None):
        if home is not None:
            target = Path(home)
        elif profile is not None and profile != "default":
            target = self.root / "profiles" / profile
        else:
            target = self.root
        return types.SimpleNamespace(profile_dir=target.resolve())

    def store_exists(self, scope):
        return Path(scope.profile_dir).resolve() in self.stores

    @contextmanager
    def open_reader(self, target):
        key = Path(target).resolve()
        if key in self.broken:
            raise self.broken[key]
        sessions_map = self.stores.get(key)
        if sessions_map is None:
            raise _FakePersistenceError("unable to open database file")

        class _SessionsRepo:
            def search_sessions(self, *, sources=None, window=None):
                wanted = set(sources) if sources is not None else None
                rows = [
                    (recency, sid, source)
                    for sid, (source, _msgs, recency) in sessions_map.items()
                    if wanted is None or source in wanted
                ]
                rows.sort(key=lambda item: item[0], reverse=True)
                if window is not None and window.limit is not None:
                    rows = rows[: window.limit]
                return [
                    types.SimpleNamespace(session=types.SimpleNamespace(session_id=sid, source=source))
                    for _recency, sid, source in rows
                ]

        class _MessagesRepo:
            def read_transcript(self, session_id, read=None):
                _source, msgs, _recency = sessions_map[session_id]
                active_rows = []
                for entry in msgs:
                    if len(entry) == 3:
                        role, content, active = entry
                    else:
                        role, content, active = entry[0], entry[1], True
                    if active:
                        active_rows.append(types.SimpleNamespace(role=role, content=content))
                if read is None:
                    return active_rows
                if read.latest:
                    desc = list(reversed(active_rows))
                    sliced = desc[read.offset : read.offset + read.limit]
                    return list(reversed(sliced))
                return active_rows[read.offset : read.offset + read.limit]

        yield types.SimpleNamespace(sessions=_SessionsRepo(), messages=_MessagesRepo())


@pytest.fixture
def fake_storage(tmp_path, monkeypatch):
    registry = _FakeStoreRegistry(tmp_path)

    persistence_mod = types.ModuleType("persistence")
    persistence_mod.PersistenceError = _FakePersistenceError

    conv_mod = types.ModuleType("persistence.conversation")
    sessions_mod = types.ModuleType("persistence.conversation.sessions")
    sessions_mod.SessionWindow = lambda *, limit=None, offset=0: types.SimpleNamespace(limit=limit, offset=offset)
    messages_mod = types.ModuleType("persistence.conversation.messages")
    messages_mod.TranscriptRead = lambda *, limit=None, offset=0, latest=False: types.SimpleNamespace(
        limit=limit, offset=offset, latest=latest,
    )

    assembly_mod = types.ModuleType("persistence_assembly")
    assembly_mod.profile_scope = registry._resolve_scope
    assembly_mod.store_exists = registry.store_exists
    assembly_mod.open_persistence_reader = registry.open_reader

    constants_mod = types.ModuleType("hermes_constants")
    constants_mod.PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    constants_mod.get_default_hermes_root = lambda: tmp_path
    constants_mod.get_hermes_home = lambda: Path(os.getenv("HERMES_HOME", str(tmp_path)))
    constants_mod.named_profile_home = lambda p: Path(p) if Path(p).parent.name == "profiles" else None
    constants_mod.profile_name_for_home = lambda p: (
        Path(p).name if Path(p).parent.name == "profiles" else "default"
    )
    constants_mod.named_profile_is_live = lambda p: (Path(p) / "config.yaml").is_file()

    monkeypatch.setitem(sys.modules, "persistence", persistence_mod)
    monkeypatch.setitem(sys.modules, "persistence.conversation", conv_mod)
    monkeypatch.setitem(sys.modules, "persistence.conversation.sessions", sessions_mod)
    monkeypatch.setitem(sys.modules, "persistence.conversation.messages", messages_mod)
    monkeypatch.setitem(sys.modules, "persistence_assembly", assembly_mod)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants_mod)
    return registry


@pytest.fixture
def api_without_hermes(monkeypatch, fake_storage):
    # No `gateway` package: the plugin falls back to the original sentinel.
    monkeypatch.setitem(sys.modules, "gateway", None)
    monkeypatch.setitem(sys.modules, "gateway.response_filters", None)
    return load_api()


@pytest.fixture
def api_with_hermes_rule(monkeypatch, fake_storage):
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


def test_hermes_rule_decides_which_automated_runs_were_silent(tmp_path, fake_storage, api_with_hermes_rule):
    fake_storage.seed(tmp_path, RUNS)
    # A marker mid-sentence, a run that stopped on a tool row, a blank trailing assistant row,
    # a webhook that answered and any chat (silence there is not the autonomous rule) are all left out.
    assert api_with_hermes_rule.push_api.silent_sessions(tmp_path) == ["cron_bills_1", "cron_bills_2", "webhook_1"]


def test_older_hermes_falls_back_to_the_bracketed_sentinel(tmp_path, fake_storage, api_without_hermes):
    fake_storage.seed(tmp_path, RUNS)
    assert api_without_hermes.push_api.silent_sessions(tmp_path) == ["cron_bills_1", "cron_bills_2"]


def test_limit_takes_the_newest_runs(tmp_path, fake_storage, api_with_hermes_rule):
    fake_storage.seed(tmp_path, RUNS)
    assert api_with_hermes_rule.push_api.silent_sessions(tmp_path, limit=1) == ["cron_bills_1"]


def test_rewound_rows_do_not_count(tmp_path, fake_storage, api_with_hermes_rule):
    fake_storage.seed(tmp_path, {
        "cron_x": ("cron", [("user", "Run", True), ("assistant", "[SILENT]", True), ("assistant", "Found it", False)], 1),
    })
    assert api_with_hermes_rule.push_api.silent_sessions(tmp_path) == ["cron_x"]


def test_trailing_system_rows_page_back_to_last_conversation_row(tmp_path, fake_storage, api_with_hermes_rule):
    # Six trailing system rows force a second page (page size = 4) before reaching [SILENT].
    messages = [("user", "Run"), ("assistant", "[SILENT]")] + [("system", f"note {i}") for i in range(6)]
    fake_storage.seed(tmp_path, {"cron_paged": ("cron", messages, 1)})
    assert api_with_hermes_rule.push_api.silent_sessions(tmp_path) == ["cron_paged"]


def test_missing_store_is_empty(tmp_path, fake_storage, api_with_hermes_rule):
    assert api_with_hermes_rule.push_api.silent_sessions(tmp_path / "missing") == []


def test_route_validates_the_profile_and_reads_its_store(tmp_path, monkeypatch, fake_storage, api_with_hermes_rule):
    aria = tmp_path / "profiles" / "aria"
    aria.mkdir(parents=True)
    (aria / "config.yaml").write_text("{}\n")
    fake_storage.seed(tmp_path, RUNS)
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
    assert client.get("/sessions/silent", params={"profile": "root"}).status_code == 400


def test_an_unreadable_store_answers_503_without_store_error_text(
    tmp_path, monkeypatch, caplog, fake_storage, api_with_hermes_rule,
):
    fake_storage.break_store(tmp_path, _FakePersistenceError("file is not a database"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    app = FastAPI()
    app.include_router(api_with_hermes_rule.router)
    response = TestClient(app).get("/sessions/silent")
    assert response.status_code == 503
    assert response.json() == {"detail": "The session store could not be read; try again."}
    assert "could not be read" in caplog.text
    assert "file is not a database" in caplog.text
