"""Editing, filing and filtering reminders, and the app features they need."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from push_plugin import load_dashboard_api, load_device_package

plugin = load_device_package("hermes_mobile_reminders_plugin")
bridge = sys.modules["hermes_mobile_reminders_plugin.bridge"]

TOKEN = "a" * 32
FEATURES = ["reminders.query", "reminders.update"]


def paired(tmp_path, features=FEATURES, grants=("reminders.read", "reminders.write")):
    store = bridge.Store(tmp_path / "bridge.db")
    store.register("phone", "Phone", list(grants), TOKEN, features=features)
    return store


def test_an_older_app_is_asked_to_update_rather_than_ignore_new_arguments(tmp_path):
    store = paired(tmp_path, features=None)
    assert store.devices()[0]["features"] == []
    gated = [
        ("reminders.update", {"id": "r1", "title": "Call Ada"}),
        ("reminders.list", {"status": "completed"}),
        ("reminders.list", {"limit": 10}),
        ("reminders.create", {"title": "Milk", "list_id": "groceries"}),
        ("reminders.create", {"title": "Milk", "priority": "high"}),
        ("reminders.create", {"title": "Milk", "due_date": "2026-10-12"}),
    ]
    for index, (operation, arguments) in enumerate(gated):
        with pytest.raises(bridge.AccessUnavailable, match="update Hermes Agent") as error:
            store.request(operation, arguments, idempotency_key=f"gated-{index}")
        assert error.value.update_required
    # What older apps already understood still works.
    assert store.request("reminders.list", {})["status"] == "pending"
    assert store.request("reminders.create", {"title": "Milk", "due": "2026-10-12T09:00:00+02:00"},
                         idempotency_key="plain")["status"] == "pending"
    # A phone without the grant is still "not shared", not "update".
    store.register("phone", "Phone", [], TOKEN, features=FEATURES)
    with pytest.raises(bridge.AccessUnavailable) as error:
        store.request("reminders.update", {"id": "r1", "title": "x"}, idempotency_key="no-grant")
    assert not error.value.update_required


def test_features_register_and_unknown_ones_are_ignored(tmp_path):
    store = paired(tmp_path, features=["reminders.update", "health.teleport"])
    assert store.devices()[0]["features"] == ["reminders.update"]
    assert store.request("reminders.update", {"id": "r1", "priority": "low"},
                         idempotency_key="edit")["status"] == "pending"
    # reminders.update alone does not cover the list filters.
    with pytest.raises(bridge.AccessUnavailable, match="update Hermes Agent"):
        store.request("reminders.list", {"query": "milk"})
    for features in ["reminders.update", ["x"] * 65, [1]]:
        with pytest.raises(bridge.BridgeError, match="features"):
            store.register("phone", "Phone", ["reminders.read"], TOKEN, features=features)


def test_going_back_to_an_older_app_cancels_work_it_cannot_do(tmp_path):
    store = paired(tmp_path)
    edit = store.request("reminders.update", {"id": "r1", "title": "Call Ada"}, idempotency_key="edit")
    plain = store.request("reminders.list", {})
    store.register("phone", "Phone", ["reminders.read", "reminders.write"], TOKEN)
    assert store.result(edit["id"])["status"] == "cancelled"
    assert store.result(plain["id"])["status"] == "pending"


def test_reminder_updates_are_checked_writes(tmp_path):
    store = paired(tmp_path)
    with pytest.raises(bridge.BridgeError, match="idempotency_key"):
        store.request("reminders.update", {"id": "r1", "title": "Call Ada"})
    invalid = [
        {"title": "No id"},
        {"id": " ", "title": "Blank id"},
        {"id": "r1"},
        {"id": "r1", "due": "2026-10-12T09:00:00+02:00", "due_date": "2026-10-12"},
        {"id": "r1", "due": "2026-10-12T09:00:00+02:00", "clear_due": True},
        {"id": "r1", "clear_due": False},
        {"id": "r1", "list_id": "a", "list": "Groceries"},
        {"id": "r1", "priority": "urgent"},
        {"id": "r1", "title": "x" * 1001},
        {"id": "r1", "title": " "},
        {"id": "r1", "notes": "x" * 4001},
        {"id": "r1", "due": "tomorrow"},
        {"id": "r1", "due": "2026-10-12T09:00:00"},
        {"id": "r1", "due_date": "12.10.2026"},
        {"id": "r1", "completed": "yes"},
    ]
    for index, arguments in enumerate(invalid):
        with pytest.raises(bridge.BridgeError):
            store.request("reminders.update", arguments, idempotency_key=f"bad-{index}")
    valid = [
        {"id": "r1", "notes": ""},
        {"id": "r1", "due_date": "2026-10-12", "priority": "high"},
        {"id": "r1", "due": "2026-10-12T09:00:00Z", "list": "Groceries"},
        {"id": "r1", "clear_due": True, "list_id": "abc", "completed": False},
    ]
    for index, arguments in enumerate(valid):
        assert store.request("reminders.update", arguments, idempotency_key=f"ok-{index}")["status"] == "pending"


def test_create_takes_a_list_and_priority(tmp_path):
    store = paired(tmp_path)
    for index, arguments in enumerate([{}, {"title": " "}, {"title": "Milk", "list_id": "a", "list": "b"},
                                       {"title": "Milk", "priority": "urgent"},
                                       {"title": "Milk", "due": "2026-10-12T09:00:00Z", "due_date": "2026-10-12"}]):
        with pytest.raises(bridge.BridgeError):
            store.request("reminders.create", arguments, idempotency_key=f"bad-{index}")
    created = store.request("reminders.create", {"title": "Milk", "list": "Groceries", "priority": "medium"},
                            idempotency_key="milk")
    assert created["arguments"]["list"] == "Groceries"


@pytest.mark.parametrize("arguments", [
    {"status": "open"},
    {"limit": 0}, {"limit": 101}, {"limit": 2.5}, {"limit": True}, {"offset": -1},
    {"completed_start": "2026-01-01T00:00:00Z"},
    {"status": "all", "completed_end": "2026-01-01T00:00:00Z"},
    {"list_ids": "work"}, {"list_ids": []}, {"list_ids": [1]}, {"list_ids": ["x"] * 51},
    {"query": ""}, {"query": "x" * 201},
    {"due_start": "2026-10-12"}, {"due_start": "2026-10-12T00:00:00Z", "due_end": "2026-10-11T00:00:00Z"},
    {"include_undated": "yes"},
])
def test_list_filters_are_validated(tmp_path, arguments):
    store = paired(tmp_path)
    with pytest.raises(bridge.BridgeError):
        store.request("reminders.list", arguments)


def test_list_filters_reach_a_supporting_phone(tmp_path):
    store = paired(tmp_path)
    arguments = {"status": "completed", "completed_start": "2026-01-01T00:00:00Z", "list_ids": ["work"],
                 "query": "invoice", "limit": 100, "offset": 50}
    request = store.request("reminders.list", arguments)
    claimed = store.claim("phone", TOKEN)
    assert claimed[0]["id"] == request["id"] and claimed[0]["arguments"] == arguments
    overdue = {"status": "incomplete", "due_end": "2026-10-10T12:00:00+02:00", "include_undated": False}
    assert store.request("reminders.list", overdue)["status"] == "pending"


def test_the_update_tool_is_offered_once_a_supporting_phone_registers(tmp_path, monkeypatch):
    store = bridge.Store(tmp_path / "bridge.db")
    tools = {}

    class Context:
        def register_tool(self, **tool):
            tools[tool["name"]] = tool

    plugin.register(Context())
    monkeypatch.setattr(plugin, "store", lambda: store)
    monkeypatch.setattr(plugin, "wake", lambda _: None)
    update = tools["mobile_reminder_update"]
    assert set(update["schema"]["parameters"]["required"]) == {"id", "idempotency_key"}
    assert "check_fn" not in tools["mobile_reminder_create"]
    check = update["check_fn"]
    assert check() is False
    store.register("phone", "Phone", ["reminders.read", "reminders.write"], TOKEN)
    assert check() is False
    # The tool's own answer for an older app tells the agent to stop.
    reply = json.loads(asyncio.run(update["handler"]({"id": "r1", "title": "x", "idempotency_key": "k", "wait_seconds": 0})))
    assert reply["status"] == "unavailable" and reply["update_required"] is True
    assert "setup_required" not in reply
    store.register("phone", "Phone", ["reminders.read"], TOKEN, features=FEATURES)
    assert check() is False  # editing needs the write grant too
    store.register("phone", "Phone", ["reminders.read", "reminders.write"], TOKEN, features=FEATURES)
    assert check() is True
    list_schema = tools["mobile_reminders_list"]["schema"]["parameters"]["properties"]
    assert {"status", "due_start", "due_end", "completed_start", "list_ids", "query", "limit", "offset"} <= set(list_schema)
    assert {"list_id", "list", "priority"} <= set(tools["mobile_reminder_create"]["schema"]["parameters"]["properties"])


def test_registration_route_accepts_features(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    api = load_dashboard_api("device_api_features")
    store = bridge.Store(tmp_path / "bridge.db")
    monkeypatch.setattr(api.device_api, "store", lambda profile=None: store)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    body = {"name": "Phone", "grants": ["reminders.write"], "token": TOKEN, "scope": {}}
    assert client.put("/devices/old", json=body).status_code == 200
    assert client.put("/devices/new", json={**body, "features": ["reminders.update", "later.feature"]}).status_code == 200
    assert {d["id"]: d["features"] for d in store.devices()} == {"new": ["reminders.update"], "old": []}
