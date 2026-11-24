"""`GET /sessions/silent` reads the store of the profile Hermes binds the request to.

Runs against a real Hermes checkout on PYTHONPATH through the published storage interface;
skips without one.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_push_silent_sessions import load_api

hermes_constants = pytest.importorskip("hermes_constants")
persistence_assembly = pytest.importorskip("persistence_assembly")
sessions_mod = pytest.importorskip("persistence.conversation.sessions")
messages_mod = pytest.importorskip("persistence.conversation.messages")


def seed_real_store(home, sessions):
    """Build a real session store at *home* using only the published storage interface.

    `sessions` maps an id to `(source, [(role, content[, active]), ...], timestamp)`.
    """
    with persistence_assembly.open_persistence(persistence_assembly.profile_scope(home=home)) as store:
        for session_id, (source, msgs, ts) in sessions.items():
            store.sessions.create(sessions_mod.NewSession(session_id=session_id, source=source))
            for idx, entry in enumerate(msgs):
                if len(entry) == 3:
                    role, content, active = entry
                else:
                    role, content, active = entry[0], entry[1], True
                msg_id = store.messages.append(
                    session_id,
                    messages_mod.NewMessage(role=role, content=content, timestamp=ts + idx * 0.001),
                )
                if not active:
                    store.messages.deactivate_message(session_id, msg_id)


DEFAULT_RUNS = {
    "cron_default": ("cron", [("user", "Run"), ("assistant", "[SILENT]")], 1_700_000_020.0),
    "cron_sys_tail": (
        "cron",
        [("user", "Run"), ("assistant", "[SILENT]"), ("system", "post-run note")],
        1_700_000_015.0,
    ),
    "cron_rewound": (
        "cron",
        [("user", "Run"), ("assistant", "[SILENT]", True), ("assistant", "Rewound reply", False)],
        1_700_000_012.0,
    ),
    "cron_blank": (
        "cron",
        [("user", "Run"), ("assistant", "[SILENT]"), ("assistant", "")],
        1_700_000_011.0,
    ),
    "webhook_silent": ("webhook", [("user", "Hook"), ("assistant", "NO_REPLY")], 1_700_000_010.0),
    "cron_tool_last": (
        "cron",
        [("user", "Run"), ("assistant", "[SILENT]"), ("tool", '{"ok": true}')],
        1_700_000_009.0,
    ),
    "chat_silent": ("web", [("user", "Quiet"), ("assistant", "[SILENT]")], 1_700_000_030.0),
}
WORK_RUNS = {"cron_work": ("cron", [("user", "Run"), ("assistant", "[SILENT]")], 1_700_000_005.0)}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root, work = tmp_path / "hermes", tmp_path / "hermes/profiles/work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("{}\n")
    seed_real_store(root, DEFAULT_RUNS)
    seed_real_store(work, WORK_RUNS)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root, work


def test_a_secondary_profiles_plugin_host_reads_its_own_store(homes):
    # Hermes strips `?profile=` before forwarding to the work profile's plugin host,
    # which runs with that profile's home bound.
    api = load_api().push_api
    _root, work = homes
    token = hermes_constants.set_hermes_home_override(work)
    try:
        assert api.silent() == {"silent": ["cron_work"]}
        assert api.silent(profile="work") == {"silent": ["cron_work"]}
    finally:
        hermes_constants.reset_hermes_home_override(token)


def test_the_launch_profile_reads_the_default_store(homes):
    api = load_api().push_api
    expected = {"silent": ["cron_default", "cron_sys_tail", "cron_rewound", "webhook_silent"]}
    assert api.silent() == expected
    assert api.silent(profile="default") == expected


def test_an_unbound_request_for_another_profile_reads_that_profile(homes):
    # Older Hermes leaves every plugin request in the launch profile's home.
    api = load_api().push_api
    assert api.silent(profile="work") == {"silent": ["cron_work"]}


def test_real_route_validates_missing_invalid_and_reserved_profiles(homes):
    app = FastAPI()
    app.include_router(load_api().router)
    client = TestClient(app)
    assert client.get("/sessions/silent", params={"profile": "missing"}).status_code == 404
    assert client.get("/sessions/silent", params={"profile": "../escape"}).status_code == 400
    assert client.get("/sessions/silent", params={"profile": "root"}).status_code == 400


def test_real_corrupt_store_answers_503_without_leaking_backend_details(tmp_path, monkeypatch, caplog):
    bad_home = tmp_path / "bad_home"
    bad_home.mkdir()
    (bad_home / "state.db").write_bytes(b"not a database " * 100)
    monkeypatch.setenv("HERMES_HOME", str(bad_home))
    app = FastAPI()
    app.include_router(load_api().router)
    response = TestClient(app).get("/sessions/silent")
    assert response.status_code == 503
    assert response.json() == {"detail": "The session store could not be read; try again."}
    assert "could not be read" in caplog.text
    assert "file is not a database" in caplog.text
