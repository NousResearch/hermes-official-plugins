"""Contract tests: agent requests and device results share durable storage."""
import importlib.util
import stat
import sys
import types
from pathlib import Path

import pytest
from push_plugin import load_dashboard_api, load_device_module

bridge = load_device_module("device_bridge", "bridge.py")


def test_request_survives_restart_and_stays_in_its_profiles_store(tmp_path):
    store = bridge.Store(tmp_path / "default" / "bridge.db")
    other = bridge.Store(tmp_path / "other" / "bridge.db", "other")
    for value in (store, other):
        value.register("phone", "Phone", ["calendar.read"], "a" * 32, {"calendar_ids": ["home"]})
    request = store.request("calendar.events", {"start": "2026-09-09T00:00:00Z", "end": "2026-09-10T00:00:00Z"})
    assert request["profile"] == "default"
    assert other.claim("phone", "a" * 32) == []
    restarted = bridge.Store(tmp_path / "default" / "bridge.db")
    claimed = restarted.claim("phone", "a" * 32)
    assert [item["id"] for item in claimed] == [request["id"]]
    restarted.complete("phone", "a" * 32, request["id"], claimed[0]["lease"],
                       {"status": "succeeded", "data": {"events": []}, "observed_at": "2026-09-09T09:00:00Z"})
    assert store.result(request["id"])["result"]["data"] == {"events": []}
    with pytest.raises(bridge.BridgeError):
        other.result(request["id"])


def test_the_store_is_private_and_takes_its_journal_mode_from_hermes(tmp_path, monkeypatch):
    # Hermes decides whether a database may use WAL (not on SQLite builds with the WAL-reset
    # bug or on network filesystems); the store asks it instead of setting WAL itself.
    labels = []
    wal = types.ModuleType("sqlite_support")
    wal.apply_wal_with_fallback = lambda db, *, db_label: labels.append(db_label) or "delete"
    monkeypatch.setitem(sys.modules, "sqlite_support", wal)
    store = bridge.Store(tmp_path / "data" / "bridge.db")
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert labels == ["plugin-data/hermes-mobile-device-access/bridge.db"]
    with store.db() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    # Without Hermes the store keeps SQLite's rollback journal.
    monkeypatch.setitem(sys.modules, "sqlite_support", None)
    store = bridge.Store(tmp_path / "bare" / "bridge.db")
    with store.db() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_polling_reads_an_overdue_request_as_expired_without_writing(tmp_path):
    now = [1000]
    store = bridge.Store(tmp_path / "bridge.db", clock=lambda: now[0])
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    request = store.request("location.current", {}, ttl=15)
    store.defer_reply(request["id"], "Where am I?", str(tmp_path))
    now[0] = 1016
    assert store.result(request["id"])["status"] == "expired"
    assert [item["request_id"] for item in store.ready_replies()] == [request["id"]]
    with store.db() as db:
        assert db.execute("SELECT status FROM requests").fetchone()[0] == "pending"
    assert store.claim("phone", "a" * 32) == []  # a write expires it for good
    with store.db() as db:
        assert db.execute("SELECT status FROM requests").fetchone()[0] == "expired"


def test_retrying_a_write_never_creates_a_second_request_and_revocation_cancels_it(tmp_path):
    store = bridge.Store(tmp_path / "bridge.db")
    store.register("phone", "Phone", ["reminders.write"], "a" * 32)
    first = store.request("reminders.create", {"title": "Buy milk"}, idempotency_key="milk-once")
    assert store.request("reminders.create", {"title": "Buy milk"}, idempotency_key="milk-once")["id"] == first["id"]
    with pytest.raises(bridge.BridgeError):
        store.request("reminders.create", {"title": "Buy bread"}, idempotency_key="milk-once")
    claimed = store.claim("phone", "a" * 32)[0]
    store.register("phone", "Phone", [], "a" * 32)
    with pytest.raises(bridge.BridgeError):
        store.complete("phone", "a" * 32, first["id"], claimed["lease"], {"status": "succeeded"})
    assert store.result(first["id"])["status"] == "cancelled"


def test_a_spent_idempotency_key_is_forgotten_after_its_retention(tmp_path):
    now = [1000]
    store = bridge.Store(tmp_path / "bridge.db", clock=lambda: now[0])
    store.register("phone", "Phone", ["reminders.write"], "a" * 32)
    store.request("reminders.create", {"title": "Buy milk"}, idempotency_key="milk-once")
    now[0] += 86401
    store.prune()
    with store.db() as db:
        assert [tuple(row) for row in db.execute("SELECT spent FROM spent_keys")] == [(1000,)]
    now[0] = 1000 + bridge.SPENT_KEY_SECONDS - 1
    with pytest.raises(bridge.BridgeError, match="already used"):
        store.request("reminders.create", {"title": "Buy milk"}, idempotency_key="milk-once")
    now[0] = 1000 + bridge.SPENT_KEY_SECONDS + 1
    store.prune()
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM spent_keys").fetchone()[0] == 0


def test_hashes_from_older_plugins_are_adopted_and_expire(tmp_path):
    import hashlib
    now = [5000]
    store = bridge.Store(tmp_path / "bridge.db", clock=lambda: now[0])
    store.register("phone", "Phone", ["reminders.write"], "a" * 32)
    with store.db() as db:  # what 1.7.1 kept: a hash without a time
        db.execute("INSERT INTO used_keys VALUES(?)", (hashlib.sha256(b"old-key").hexdigest(),))
    with pytest.raises(bridge.BridgeError, match="already used"):
        store.request("reminders.create", {"title": "Buy milk"}, idempotency_key="old-key")
    store.prune()
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM used_keys").fetchone()[0] == 0
        assert [tuple(row) for row in db.execute("SELECT spent FROM spent_keys")] == [(5000,)]
    now[0] += bridge.SPENT_KEY_SECONDS + 1
    store.prune()
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM spent_keys").fetchone()[0] == 0


def test_http_device_flow_uses_the_same_result_as_the_agent(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    api = load_dashboard_api("device_api")
    store = bridge.Store(tmp_path / "bridge.db")
    monkeypatch.setattr(api.device_api, "store", lambda profile=None: store)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-mobile")
    client = TestClient(app)
    base = "/api/plugins/hermes-mobile"
    assert client.put(base + "/devices/legacy", json={
        "name": "Old Phone", "grants": ["health.read"], "token": "a" * 32}).status_code == 422
    assert client.put(base + "/devices/phone", params={"profile": "default"}, json={
        "name": "Phone", "grants": ["reminders.read"], "token": "a" * 32, "scope": {}}).status_code == 200
    request = store.request("reminders.list", {})
    assert client.post(base + "/devices/phone/pending", params={"profile": "default"}).status_code == 401
    headers = {"X-Hermes-Mobile-Device-Token": "a" * 32}
    claimed = client.post(base + "/devices/phone/pending", params={"profile": "default"}, headers=headers).json()["requests"][0]
    response = client.post(base + f"/devices/phone/results/{request['id']}", params={"profile": "default"}, headers=headers,
                           json={"lease": claimed["lease"], "result": {"status": "succeeded", "data": {"reminders": [{"title": "Buy milk"}]}}})
    assert response.status_code == 200
    assert store.result(request["id"])["result"]["data"]["reminders"][0]["title"] == "Buy milk"
    # The contract has no device listing: the agent reads devices through mobile_devices.
    assert {route.path for route in api.router.routes} == {
        "/sessions/silent", "/setup", "/devices/{device_id}", "/devices/{device_id}/pending",
        "/devices/{device_id}/results/{request_id}", "/requests/{request_id}"}
    assert client.get(base + "/devices").status_code in {404, 405}


def test_delayed_completion_schedules_one_reply_even_after_registration_failure(tmp_path):
    module = load_device_module("device_continuation", "continuation.py")
    store = bridge.Store(tmp_path / "bridge.db")
    store.register("phone", "Phone", ["calendar.read"], "a" * 32, {"calendar_ids": ["home"]})
    request = store.request("calendar.events", {})
    store.defer_reply(request["id"], "Prepare my morning briefing", str(tmp_path))
    scheduled = []
    def schedule(item):
        if item["request_id"] not in scheduled:
            scheduled.append(item["request_id"])
            raise RuntimeError("Scheduler saved the job, but provider registration failed")
        return "job-123"
    module.reconcile(store, schedule=schedule)
    assert scheduled == []  # still waiting for phone data
    claim = store.claim("phone", "a" * 32)[0]
    store.complete("phone", "a" * 32, request["id"], claim["lease"], {"status": "succeeded", "data": {"events": []}})
    module.reconcile(store, schedule=schedule)
    module.reconcile(bridge.Store(tmp_path / "bridge.db"), schedule=schedule)
    module.reconcile(store, schedule=schedule)
    assert scheduled == [request["id"]]


def test_the_phones_result_schedules_the_reply_without_router_lifespan(tmp_path, monkeypatch):
    # Hermes' plugin host serves the router like this and never sends lifespan events.
    import asyncio

    import httpx
    from fastapi import FastAPI
    api = load_dashboard_api("device_api_hosted")
    store = bridge.Store(tmp_path / "bridge.db")
    monkeypatch.setattr(api.device_api, "store", lambda profile=None: store)
    scheduled = []
    monkeypatch.setattr(api.device_api._replies, "schedule_reply", lambda item: scheduled.append(item["request_id"]) or "job-1")
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    request = store.request("location.current", {})
    store.defer_reply(request["id"], "Where am I?", str(tmp_path))
    app = FastAPI()
    app.include_router(api.router)
    headers = {"X-Hermes-Mobile-Device-Token": "a" * 32}

    async def phone():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://plugin-host") as client:
            claimed = (await client.post("/devices/phone/pending", headers=headers)).json()["requests"][0]
            assert scheduled == []
            response = await client.post(f"/devices/phone/results/{request['id']}", headers=headers,
                                         json={"lease": claimed["lease"], "result": {"status": "succeeded"}})
            assert response.status_code == 200
    asyncio.run(phone())
    assert scheduled == [request["id"]]
    assert store.ready_replies() == []


@pytest.mark.parametrize("operation,grants,initial,narrowed,arguments,denied", [
    ("calendar.events", ["calendar.read"], {"calendar_ids": ["work", "home"]},
     {"calendar_ids": ["home"]}, {}, {"calendar_ids": ["work"]}),
    ("health.summary", ["health.read"], {"health_metrics": ["steps", "sleep"]},
     {"health_metrics": ["steps"]}, {}, {"metrics": ["sleep"]}),
    ("health.summary", ["health.read"], {"health_metrics": ["steps", "workouts"]},
     {"health_metrics": ["steps"]}, {"metrics": ["workouts"]}, {"metrics": ["workouts"]}),
    ("health.summary", ["health.read"], {"health_metrics": ["weight", "blood_pressure"]},
     {"health_metrics": ["weight"]}, {"metrics": ["weight"]}, {"metrics": ["blood_pressure"]}),
])
def test_narrowing_scope_revokes_running_and_cached_results(tmp_path, operation, grants, initial, narrowed, arguments, denied):
    store = bridge.Store(tmp_path / "bridge.db")
    store.register("phone", "Phone", grants, "a" * 32, initial)
    cached = store.request(operation, arguments)
    claim = store.claim("phone", "a" * 32)[0]
    store.complete("phone", "a" * 32, cached["id"], claim["lease"],
                   {"status": "succeeded", "data": {"sensitive": "old scope"}})
    running = store.request(operation, arguments)
    claim = store.claim("phone", "a" * 32)[0]
    store.register("phone", "Phone", grants, "a" * 32, narrowed)
    assert store.devices()[0]["grants"] == grants  # coarse grant was retained
    assert store.result(cached["id"])["result"] is None
    assert store.result(cached["id"])["status"] == "cancelled"
    with pytest.raises(bridge.BridgeError, match="revoked"):
        store.complete("phone", "a" * 32, running["id"], claim["lease"], {"status": "succeeded"})
    with pytest.raises(bridge.BridgeError, match="not shared"):
        store.request(operation, denied)
    assert store.request(operation, {})["status"] == "pending"


def test_empty_scopes_and_excluded_calendar_writes_fail_closed(tmp_path):
    store = bridge.Store(tmp_path / "bridge.db")
    store.register("phone", "Phone", ["calendar.read", "calendar.write", "health.read"], "a" * 32)
    assert store.devices()[0]["grants"] == []
    for operation in ["calendar.events", "health.summary"]:
        with pytest.raises(bridge.BridgeError):
            store.request(operation, {})
    store.register("phone", "Phone", ["calendar.write"], "a" * 32, {"calendar_ids": ["home"]})
    with pytest.raises(bridge.BridgeError, match="not shared"):
        store.request("calendar.create", {"calendar_id": "work"}, idempotency_key="excluded")
    assert store.request("calendar.create", {"calendar_id": "home"}, idempotency_key="allowed")["status"] == "pending"


def test_contacts_search_requires_a_query_and_shared_detail_categories(tmp_path):
    store = bridge.Store(tmp_path / "bridge.db")
    # A coarse grant without chosen detail categories shares nothing.
    store.register("phone", "Phone", ["contacts.read"], "a" * 32)
    assert store.devices()[0]["grants"] == []
    with pytest.raises(bridge.AccessUnavailable):
        store.request("contacts.search", {"query": "Ada"})
    store.register("phone", "Phone", ["contacts.read"], "a" * 32, {"contact_fields": ["email", "phone"]})
    for arguments in [{}, {"query": "  "}, {"query": "Ada", "fields": ["birthday"]}]:
        with pytest.raises(bridge.BridgeError):
            store.request("contacts.search", arguments)
    cached = store.request("contacts.search", {"query": "Ada", "fields": ["phone"]})
    claim = store.claim("phone", "a" * 32)[0]
    store.complete("phone", "a" * 32, cached["id"], claim["lease"],
                   {"status": "succeeded", "data": {"contacts": [{"name": "Ada Lovelace"}]}})
    # Dropping a detail category discards results gathered under the old scope.
    store.register("phone", "Phone", ["contacts.read"], "a" * 32, {"contact_fields": ["email"]})
    assert store.result(cached["id"])["status"] == "cancelled"
    assert store.result(cached["id"])["result"] is None


def test_weekly_steps_are_delivered_by_one_device_request(tmp_path):
    store = bridge.Store(tmp_path / "bridge.db")
    store.register("phone", "Phone", ["health.read"], "a" * 32,
                   {"health_metrics": ["steps"]})
    args = {"start": "2026-09-07T00:00:00Z", "end": "2026-09-14T00:00:00Z", "metrics": ["steps"]}
    request = store.request("health.summary", args)
    claimed = store.claim("phone", "a" * 32)
    assert len(claimed) == 1
    assert claimed[0]["arguments"] == args
    days = [{"start": f"2026-09-{day:02d}T00:00:00Z", "end": f"2026-09-{day + 1:02d}T00:00:00Z",
             "steps": None if day == 10 else 1000} for day in range(7, 14)]
    result = {"status": "succeeded", "data": {"steps": 6000, "daily_steps": days, "time_zone": "UTC"}}
    store.complete("phone", "a" * 32, request["id"], claimed[0]["lease"], result)
    assert store.result(request["id"])["result"] == result


def test_alarm_requests_are_writes_with_checked_schedules(tmp_path):
    store = bridge.Store(tmp_path / "bridge.db")
    store.register("phone", "Phone", ["alarms.manage"], "a" * 32)
    assert store.devices()[0]["grants"] == ["alarms.manage"]
    assert store.request("alarms.list", {})["status"] == "pending"
    with pytest.raises(bridge.BridgeError, match="idempotency_key"):
        store.request("alarms.create", {"time": "07:30"})
    invalid = [
        ("alarms.create", {}),
        ("alarms.create", {"at": "2026-10-10T07:00:00+02:00", "time": "07:00"}),
        ("alarms.create", {"time": "7:30"}),
        ("alarms.create", {"time": "24:00"}),
        ("alarms.create", {"at": "2026-10-10T07:00:00+02:00", "weekdays": ["monday"]}),
        ("alarms.create", {"time": "07:30", "weekdays": ["someday"]}),
        ("alarms.create", {"time": "07:30", "label": "x" * 101}),
        ("timers.start", {"duration_seconds": 0}),
        ("timers.start", {"duration_seconds": True}),
        ("timers.start", {"duration_seconds": 86401}),
        ("alarms.cancel", {"id": " "}),
    ]
    for index, (operation, arguments) in enumerate(invalid):
        with pytest.raises(bridge.BridgeError):
            store.request(operation, arguments, idempotency_key=f"invalid-{index}")
    valid = [
        ("alarms.create", {"time": "07:30", "weekdays": ["monday", "friday"], "label": "Gym"}),
        ("alarms.create", {"at": "2026-10-10T07:00:00+02:00"}),
        ("timers.start", {"duration_seconds": 600, "label": "Pasta"}),
        ("alarms.cancel", {"id": "6F9619FF-8B86-D011-B42D-00CF4FC964FF"}),
    ]
    for index, (operation, arguments) in enumerate(valid):
        assert store.request(operation, arguments, idempotency_key=f"valid-{index}")["status"] == "pending"
    # Turning the switch off on the phone drops the queued alarm work.
    store.register("phone", "Phone", [], "a" * 32)
    with pytest.raises(bridge.AccessUnavailable):
        store.request("alarms.list", {})


def test_health_logging_needs_its_own_selection_and_an_idempotency_key(tmp_path):
    store = bridge.Store(tmp_path / "bridge.db")
    water = {"entries": [{"metric": "water", "value": 250}]}
    # Reading a category does not let the agent log it.
    store.register("phone", "Phone", ["health.read", "health.write"], "a" * 32, {"health_metrics": ["water"]})
    assert store.devices()[0]["grants"] == ["health.read"]
    with pytest.raises(bridge.AccessUnavailable):
        store.request("health.log", water, idempotency_key="water-1")
    store.register("phone", "Phone", ["health.write"], "a" * 32, {"health_write_metrics": ["water", "caffeine"]})
    assert store.devices()[0]["scope"]["health_write_metrics"] == ["caffeine", "water"]
    with pytest.raises(bridge.BridgeError, match="idempotency_key"):
        store.request("health.log", water)
    for arguments in [{}, {"entries": []}, {"entries": [{"metric": "water", "value": "lots"}]},
                      {"entries": [{"metric": "water", "value": True}]}, {"entries": [{"metric": "water"}] * 21}]:
        with pytest.raises(bridge.BridgeError, match="entries"):
            store.request("health.log", arguments, idempotency_key="bad")
    with pytest.raises(bridge.BridgeError, match="not shared"):
        store.request("health.log", {"entries": [{"metric": "weight", "value": 70}]}, idempotency_key="weight")
    logged = store.request("health.log", water, idempotency_key="water-1")
    assert store.request("health.log", water, idempotency_key="water-1")["id"] == logged["id"]
    # Withdrawing a logging category cancels a queued log.
    store.register("phone", "Phone", ["health.write"], "a" * 32, {"health_write_metrics": ["caffeine"]})
    assert store.result(logged["id"])["status"] == "cancelled"


def test_health_scope_rejects_unknown_categories_and_read_only_logging(tmp_path):
    store = bridge.Store(tmp_path / "bridge.db")
    for scope in [{"health_metrics": ["blood_alcohol"]}, {"health_write_metrics": ["heart_rate"]},
                  {"health_write_metrics": "water"}]:
        with pytest.raises(bridge.BridgeError, match="Health"):
            store.register("phone", "Phone", ["health.read"], "a" * 32, scope)
    store.register("phone", "Phone", ["health.read"], "a" * 32,
                   {"health_metrics": sorted(bridge.HEALTH_METRICS)})
    with pytest.raises(bridge.BridgeError, match="interval"):
        store.request("health.summary", {"interval": "minute"})
    assert store.request("health.summary", {"interval": "week", "metrics": ["sleep", "vo2_max"]})["status"] == "pending"


def test_a_request_claimed_just_before_it_expires_keeps_its_lease(tmp_path):
    now = [1000]
    store = bridge.Store(tmp_path / "bridge.db", clock=lambda: now[0])
    store.register("phone", "Phone", ["calendar.write", "location.read"], "a" * 32,
                   {"calendar_ids": ["c"], "calendars": [{"id": "c", "title": "C", "writable": True}]})
    arguments = {"title": "x", "start": "2026-10-20T10:00:00Z", "end": "2026-10-20T11:00:00Z"}
    request = store.request("calendar.create", arguments, idempotency_key="k1")
    reply = store.request("location.current", {})
    store.defer_reply(request["id"], "Add it", str(tmp_path))
    store.defer_reply(reply["id"], "Where am I?", str(tmp_path))
    claimed_at = now[0] = request["expires_at"] - 2
    claimed = {item["id"]: item for item in store.claim("phone", "a" * 32)}
    now[0] += 5  # past expires_at, inside the 35-second lease
    # The phone may be doing it: neither the agent nor the reply treats it as expired yet.
    assert store.result(request["id"])["status"] == "running"
    assert store.ready_replies() == []
    assert store.request("calendar.create", arguments, idempotency_key="k1")["status"] == "running"
    completed = store.complete("phone", "a" * 32, request["id"], claimed[request["id"]]["lease"],
                               {"status": "succeeded", "data": {"id": "evt"}})
    assert completed["status"] == "succeeded"
    assert store.result(request["id"])["result"]["data"] == {"id": "evt"}
    assert store.request("calendar.create", arguments, idempotency_key="k1")["status"] == "succeeded"
    assert [item["request_id"] for item in store.ready_replies()] == [request["id"]]
    # A lease that lapses unanswered expires the request, for reads and writes alike.
    now[0] = claimed_at + 34
    assert store.result(reply["id"])["status"] == "running"
    now[0] = claimed_at + 36
    assert store.result(reply["id"])["status"] == "expired"
    assert {item["request_id"] for item in store.ready_replies()} == {request["id"], reply["id"]}
    with pytest.raises(bridge.BridgeError):
        store.complete("phone", "a" * 32, reply["id"], claimed[reply["id"]]["lease"], {"status": "succeeded"})
    assert store.result(reply["id"])["status"] == "expired"


def test_a_pending_request_still_expires_at_its_deadline(tmp_path):
    now = [1000]
    store = bridge.Store(tmp_path / "bridge.db", clock=lambda: now[0])
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    request = store.request("location.current", {}, ttl=15)
    now[0] = request["expires_at"]
    assert store.result(request["id"])["status"] == "expired"
    assert store.claim("phone", "a" * 32) == []


@pytest.mark.parametrize("write", ["defer_reply", "mark_reply", "prune"])
def test_every_write_prunes_results_past_their_retention(tmp_path, write):
    now = [1000]
    store = bridge.Store(tmp_path / "bridge.db", clock=lambda: now[0])
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    old = store.request("location.current", {})
    claim = store.claim("phone", "a" * 32)[0]
    store.complete("phone", "a" * 32, old["id"], claim["lease"], {"status": "succeeded", "data": {"latitude": 1}})
    now[0] += 80000
    recent = store.request("location.current", {})
    store.defer_reply(recent["id"], "Where am I?", str(tmp_path))
    now[0] += 6402  # the old result is past 24 hours
    assert store.result(old["id"])["result"]["data"] == {"latitude": 1}  # reads never write
    {"defer_reply": lambda: store.defer_reply(recent["id"], "Where am I?", str(tmp_path)),
     "mark_reply": lambda: store.mark_reply(recent["id"], "job"),
     "prune": store.prune}[write]()
    with pytest.raises(bridge.BridgeError):
        store.result(old["id"])
