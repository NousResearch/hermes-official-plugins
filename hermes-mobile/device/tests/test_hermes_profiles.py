"""The plugin loaded by real Hermes: each profile keeps its own store, and a reply lands in its cron."""
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAME = "hermes-mobile-device-access"


def _hermes_dashboard():
    try:
        return importlib.util.find_spec("hermes_cli.web_server") is not None
    except Exception:
        return False


# Runs in its own process: Hermes reads HERMES_HOME at import, keeps process-wide state and
# takes over stdout, so the answers go to the file named by argv[1].
DRIVER = """
import importlib.util, json, os, sys
from fastapi.testclient import TestClient
import hermes_cli.web_server as server
client = TestClient(server.app)
client.__enter__()
headers = {"Authorization": f"Bearer {server._SESSION_TOKEN}", "Host": "127.0.0.1"}
name = "%s"
base = "/api/plugins/" + name
body = {"name": "Phone", "grants": ["location.read"], "token": "a" * 32, "scope": {}}
answers = {"setup": client.get(base + "/setup", headers=headers).json()["profiles"]}
for profile in ("work", "default"):
    response = client.put(base + "/devices/" + profile + "-phone", params={"profile": profile}, json=body, headers=headers)
    answers["put-" + profile] = response.status_code
# The agent deferred a reply in work; the phone's result through work's plugin host schedules it.
home = os.environ["HERMES_HOME"]
spec = importlib.util.spec_from_file_location("bridge_under_test", os.path.join(home, "plugins", name, "bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
work = os.path.join(home, "profiles", "work")
store = bridge.open_store(work)  # through Hermes' plugin storage, as the work profile's tools open it
answers["store"] = [str(store.path), store.profile]
request = store.request("location.current", {})
store.defer_reply(request["id"], "Where am I?", work)
phone = {**headers, "X-Hermes-Mobile-Device-Token": "a" * 32}
claimed = client.post(base + "/devices/work-phone/pending", params={"profile": "work"}, headers=phone).json()["requests"]
answers["claimed-work"] = [item["id"] for item in claimed] == [request["id"]]
response = client.post(base + "/devices/work-phone/results/" + request["id"], params={"profile": "work"}, headers=phone,
                       json={"lease": claimed[0]["lease"], "result": {"status": "succeeded"}})
answers["result-work"] = response.status_code
answers["request"] = request["id"]
with open(sys.argv[1], "w") as file:
    json.dump(answers, file)
os._exit(0)
""" % NAME  # noqa: UP031 -- the driver source is full of braces


@pytest.mark.skipif(not _hermes_dashboard(), reason="needs a Hermes checkout on PYTHONPATH (run on the test server)")
def test_a_second_profile_keeps_its_devices_and_replies(tmp_path):
    home = tmp_path / "home"
    work = home / "profiles" / "work"
    for profile_home in (home, work):
        profile_home.mkdir(parents=True)
        (profile_home / "config.yaml").write_text(f"plugins:\n  enabled: [{NAME}]\n")
        (profile_home / ".env").write_text("")
    shutil.copytree(ROOT, home / "plugins" / NAME, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    answers = tmp_path / "answers.json"
    done = subprocess.run([sys.executable, "-c", DRIVER, str(answers)], env=env, cwd=tmp_path, capture_output=True, text=True,
                          timeout=180, check=False)
    assert done.returncode == 0 and answers.exists(), done.stderr[-2000:]
    answers = json.loads(answers.read_text())
    request = answers.pop("request")
    stores = {"default": home / "plugin-data" / NAME / "bridge.db", "work": work / "plugin-data" / NAME / "bridge.db"}
    assert answers.pop("store") == [str(stores["work"]), "work"]
    assert answers == {"setup": ["default", "work"], "put-work": 200, "put-default": 200,
                       "claimed-work": True, "result-work": 200}
    import hermes_state_wal
    journal_mode = "delete" if hermes_state_wal.is_sqlite_wal_reset_vulnerable() else "wal"
    replies = {}
    for profile, path in stores.items():
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700 and stat.S_IMODE(path.stat().st_mode) == 0o600
        db = sqlite3.connect(path)
        try:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode
            assert db.execute("SELECT id FROM devices").fetchall() == [(f"{profile}-phone",)]
            replies[profile] = db.execute("SELECT request_id, job_id FROM replies").fetchall()
        finally:
            db.close()
    [(reply, job)] = replies.pop("work")
    assert reply == request and replies == {"default": []}
    # The phone's result scheduled one push reply in work's own cron, none in the launch profile's.
    jobs = json.loads((work / "cron" / "jobs.json").read_text())
    jobs = jobs["jobs"] if isinstance(jobs, dict) else jobs
    assert [(item["id"], item["name"], item["deliver"], item["enabled_toolsets"]) for item in jobs] == [
        (job, f"{NAME}-{request}", "push", ["mobile_device"])]
    assert request in jobs[0]["prompt"] and "Where am I?" in jobs[0]["prompt"]
    assert not (home / "cron" / "jobs.json").exists()


@pytest.mark.skipif(not _hermes_dashboard(), reason="needs a Hermes checkout on PYTHONPATH (run on the test server)")
@pytest.mark.parametrize("vulnerable, mode", [(True, "delete"), (False, "wal")])
def test_the_store_uses_wal_only_where_hermes_allows_it(tmp_path, monkeypatch, vulnerable, mode):
    import hermes_state_wal
    spec = importlib.util.spec_from_file_location("bridge_wal_under_test", ROOT / "bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    # What Hermes sees on an SQLite build with the WAL-reset bug (3.7.0 to 3.51.2), and on a fixed one.
    monkeypatch.setattr(hermes_state_wal, "is_sqlite_wal_reset_vulnerable", lambda *args, **kwargs: vulnerable)
    store = bridge.Store(tmp_path / "bridge.db")
    db = sqlite3.connect(store.path)
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == mode
    finally:
        db.close()
