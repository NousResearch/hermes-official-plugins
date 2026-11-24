"""Which profile a route or a tool call serves."""
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException
from push_plugin import load_dashboard_api, load_device_module, load_device_package

dashboard_api = load_dashboard_api("device_api_profiles")
api = dashboard_api.device_api
plugin = load_device_package("hermes_mobile_profiles_plugin")


def hermes(monkeypatch, root, home, override=None):
    """Hermes' home helpers and plugin storage; override is the home Hermes bound to the call."""
    bound = [override]
    constants = types.ModuleType("hermes_constants")
    constants.get_default_hermes_root = lambda: root
    constants.get_hermes_home_override = lambda: bound[-1]
    constants.get_hermes_home = lambda: Path(bound[-1] or home)
    constants.set_hermes_home_override = lambda path: bound.append(path) or len(bound) - 1
    constants.reset_hermes_home_override = lambda token: bound.__delitem__(slice(token, None))
    constants.PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    constants.named_profile_is_live = lambda path: (Path(path) / "config.yaml").is_file()
    constants.profile_name_for_home = lambda path: (
        "default" if Path(path) == root else Path(path).name if Path(path).parent == root / "profiles" else None)
    storage = types.ModuleType("plugins.plugin_storage")

    def plugin_data_dir(name):
        directory = constants.get_hermes_home() / "plugin-data" / name
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    storage.plugin_data_dir = plugin_data_dir
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)


@pytest.fixture
def root(tmp_path):
    for name in ("work", "ghost"):
        (tmp_path / "profiles" / name).mkdir(parents=True)
    (tmp_path / "profiles" / "work" / "config.yaml").write_text("")
    return tmp_path


def test_setup_lists_the_live_profiles(monkeypatch, root):
    hermes(monkeypatch, root, root)
    assert api.setup()["profiles"] == ["default", "work"]


def test_a_secondary_profiles_plugin_host_serves_its_own_profile(monkeypatch, root):
    # Hermes drops ?profile= when it forwards to the profile's plugin host, which runs in its home.
    hermes(monkeypatch, root, root / "profiles" / "work")
    assert api.home(None) == root / "profiles" / "work"


def test_the_scope_hermes_binds_wins_over_the_query(monkeypatch, root):
    hermes(monkeypatch, root, root, override=root / "profiles" / "work")
    assert api.home("default") == root / "profiles" / "work"


def test_the_query_names_the_profile_where_hermes_binds_none(monkeypatch, root):
    hermes(monkeypatch, root, root)
    assert api.home("work") == root / "profiles" / "work"
    assert api.home(None) == root
    for missing_profile in ("bogus", "ghost"):
        with pytest.raises(HTTPException) as missing:
            api.home(missing_profile)
        assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as invalid:
        api.home("../work")
    assert invalid.value.status_code == 400


def test_a_scheduled_reply_targets_its_profiles_home_via_cron_plugin_api(monkeypatch, root):
    continuation = load_device_module("device_continuation_profile_home", "continuation.py")
    calls = []
    cron_pkg = types.ModuleType("cron")
    cron_api = types.ModuleType("cron.plugin_api")

    def ensure_named_job(name, **kwargs):
        calls.append((name, kwargs))
        return {"id": "job-work-1", "name": name}

    cron_api.ensure_named_job = ensure_named_job
    cron_pkg.plugin_api = cron_api
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.plugin_api", cron_api)
    work = root / "profiles" / "work"
    job_id = continuation.schedule_reply({"request_id": "req-42", "home": str(work), "prompt": "Where am I?"})
    assert job_id == "job-work-1"
    assert len(calls) == 1
    name, kwargs = calls[0]
    assert name == "hermes-mobile-device-access-req-42"
    assert kwargs["home"] == work
    assert kwargs["schedule"] == "in 1m"
    assert kwargs["repeat"] == 1
    assert kwargs["deliver"] == "push"
    assert kwargs["enabled_toolsets"] == ["mobile_device"]
    assert "req-42" in kwargs["prompt"] and "Where am I?" in kwargs["prompt"]


def test_a_busy_database_answers_503():
    import sqlite3
    with pytest.raises(HTTPException) as busy, api.errors():
        raise sqlite3.OperationalError("database is locked")
    assert busy.value.status_code == 503
    with pytest.raises(sqlite3.OperationalError), api.errors():
        raise sqlite3.OperationalError("no such table: devices")


def test_a_store_busy_on_opening_answers_503(monkeypatch):
    import sqlite3

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    def locked(profile=None):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(api._bridge, "open_store", lambda home: locked())
    monkeypatch.setattr(api, "home", lambda profile=None: Path("/nonexistent"))
    app = FastAPI()
    app.include_router(dashboard_api.router)
    client = TestClient(app)
    headers = {"X-Hermes-Mobile-Device-Token": "a" * 32}
    body = {"name": "Phone", "grants": [], "token": "a" * 32, "scope": {}}
    assert client.put("/devices/phone", json=body).status_code == 503
    assert client.post("/devices/phone/pending", headers=headers).status_code == 503
    assert client.post("/devices/phone/results/r", headers=headers,
                       json={"lease": "a" * 32, "result": {"status": "succeeded"}}).status_code == 503
    assert client.get("/requests/r").status_code == 503


def test_each_profile_keeps_its_store_in_its_own_plugin_data(monkeypatch, root):
    work = root / "profiles" / "work"
    hermes(monkeypatch, root, root)
    store = api.store("work")
    assert (store.path, store.profile) == (work / "plugin-data" / api._bridge.NAME / "bridge.db", "work")
    assert (api.store().path.parent, api.store().profile) == (root / "plugin-data" / api._bridge.NAME, "default")
    with pytest.raises(HTTPException):
        api.store("ghost")
    assert sys.modules["hermes_constants"].get_hermes_home_override() is None  # the lookup left no binding
    # A tool call opens the store of the home Hermes runs the turn in.
    hermes(monkeypatch, root, root, override=work)
    store = plugin.store()
    assert (store.path.parent, store.profile) == (work / "plugin-data" / api._bridge.NAME, "work")


def test_a_wrong_device_token_answers_403_and_a_missing_one_401(monkeypatch, root):
    # Not 401: to the apps that reads as an expired dashboard session; the pairing token is the device's own.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    hermes(monkeypatch, root, root)
    app = FastAPI()
    app.include_router(dashboard_api.router)
    client = TestClient(app)
    body = {"name": "Phone", "grants": ["calendar.read"], "token": "a" * 32, "scope": {}}
    assert client.put("/devices/phone", json=body).status_code == 200
    wrong = {"X-Hermes-Mobile-Device-Token": "b" * 32}
    refused = client.post("/devices/phone/pending", headers=wrong)
    assert (refused.status_code, refused.json()) == (403, {"detail": "Device is not paired"})
    assert client.post("/devices/tablet/pending", headers={"X-Hermes-Mobile-Device-Token": "a" * 32}).status_code == 403
    assert client.post("/devices/phone/results/r", headers=wrong,
                       json={"lease": "a" * 32, "result": {"status": "succeeded"}}).status_code == 403
    assert client.post("/devices/phone/pending").status_code == 401
    paired = client.post("/devices/phone/pending", headers={"X-Hermes-Mobile-Device-Token": "a" * 32})
    assert (paired.status_code, paired.json()) == (200, {"requests": []})
