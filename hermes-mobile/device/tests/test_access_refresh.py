"""Tool replies must not leave the model relying on remembered calendar grants."""
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("hermes_mobile_access_refresh", ROOT / "__init__.py",
                                              submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
bridge = sys.modules[spec.name + ".bridge"]


@pytest.mark.parametrize("poll", [False, True])
@pytest.mark.parametrize("change_during_wait", [False, True])
def test_calendar_change_during_wait_returns_latest_scope(tmp_path, monkeypatch, poll, change_during_wait):
    store = bridge.Store(tmp_path / "bridge.db", "agent")
    def share(ids):
        # A separate connection models registration by the dashboard process.
        bridge.Store(store.path).register("phone", "Phone", ["calendar.read"], "a" * 32,
            {"calendar_ids": ids, "calendars": [{"id": i, "title": i} for i in ids]})
    share(["Roche"])
    if not change_during_wait:
        share(["Personal", "Birthdays"])
    handlers = {}
    class Context:
        def register_tool(self, **tool):
            handlers[tool["name"]] = tool["handler"]
    plugin.register(Context())
    monkeypatch.setattr(plugin, "store", lambda: store)
    monkeypatch.setattr(plugin, "wake", lambda _: None)
    async def change_while_waiting(value, request_id, seconds):
        if change_during_wait:
            share(["Personal", "Birthdays"])
        else:
            claimed = value.claim("phone", "a" * 32)[0]
            value.complete("phone", "a" * 32, request_id, claimed["lease"],
                           {"status": "succeeded", "data": {"events": []}})
        return value.result(request_id)
    monkeypatch.setattr(plugin, "await_result", change_while_waiting)
    if poll:
        request = store.request("calendar.events", {})
        result = asyncio.run(handlers["mobile_request_result"]({"request_id": request["id"]}))
    else:
        result = asyncio.run(handlers["mobile_calendar_events"]({}))
    result = json.loads(result)
    if change_during_wait:
        assert result["status"] == "cancelled"
        assert result["result"] is None
    else:
        assert result["status"] == "succeeded"
        assert result["result"]["data"]["events"] == []
    access = result["current_access"]
    assert access["profile"] == "agent"
    assert len(access["devices"]) == 1
    assert access["devices"][0]["scope"]["calendar_ids"] == ["Birthdays", "Personal"]
    listed = json.loads(asyncio.run(handlers["mobile_devices"]({})))
    assert listed == access
    # Reusing an old calendar ID must retain the denial and show the new choices.
    failed = json.loads(asyncio.run(handlers["mobile_calendar_events"]({"calendar_ids": ["Roche"]})))
    assert failed["status"] == "failed"
    assert failed["current_access"] == access


def test_replies_keep_text_readable(tmp_path, monkeypatch):
    handlers = {}
    class Context:
        def register_tool(self, **tool):
            handlers[tool["name"]] = tool["handler"]
    plugin.register(Context())
    monkeypatch.setattr(plugin, "store", lambda: bridge.Store(tmp_path / "bridge.db"))
    result = asyncio.run(handlers["mobile_devices"]({}))
    assert "Settings → Device Access" in result


def test_a_tool_call_schedules_its_profiles_ready_replies(tmp_path, monkeypatch):
    stores, requests = {}, {}
    for profile in ("agent", "other"):
        store = stores[profile] = bridge.Store(tmp_path / profile / "bridge.db", profile)
        store.register("phone", "Phone", ["location.read"], "a" * 32)
        request = requests[profile] = store.request("location.current", {})
        store.defer_reply(request["id"], "Where am I?", str(tmp_path / profile))
        claim = store.claim("phone", "a" * 32)[0]
        store.complete("phone", "a" * 32, request["id"], claim["lease"], {"status": "succeeded"})
    handlers = {}
    class Context:
        def register_tool(self, **tool):
            handlers[tool["name"]] = tool["handler"]
    plugin.register(Context())
    monkeypatch.setattr(plugin, "store", lambda: stores["agent"])
    scheduled = []
    continuation = sys.modules[spec.name + ".continuation"]
    monkeypatch.setattr(continuation, "schedule_reply", lambda item: scheduled.append(item["request_id"]) or "job")
    asyncio.run(handlers["mobile_devices"]({}))
    assert scheduled == [requests["agent"]["id"]]
    assert stores["agent"].ready_replies() == [] and len(stores["other"].ready_replies()) == 1


def test_a_reply_for_a_request_the_phone_never_answers_is_scheduled_at_expiry(tmp_path, monkeypatch):
    now = [1000]
    store = bridge.Store(tmp_path / "bridge.db", "agent", clock=lambda: now[0])
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    handlers, timers, scheduled = {}, [], []
    class Context:
        def register_tool(self, **tool):
            handlers[tool["name"]] = tool["handler"]
    plugin.register(Context())
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setattr(plugin, "store", lambda: store)
    monkeypatch.setattr(plugin, "wake", lambda _: None)
    monkeypatch.setattr(plugin, "can_deliver_followup", lambda: True)
    monkeypatch.setattr(plugin.threading, "Timer", lambda delay, function, args: timers.append((delay, function, args))
                        or types.SimpleNamespace(start=lambda: None))
    continuation = sys.modules[spec.name + ".continuation"]
    monkeypatch.setattr(continuation, "schedule_reply", lambda item: scheduled.append(item["request_id"]) or "job")
    result = json.loads(asyncio.run(handlers["mobile_location"]({"wait_seconds": 0})))
    assert result["status"] == "pending" and "followup" in result
    [(delay, function, args)] = timers
    assert delay == 301 and scheduled == []
    now[0] += delay
    function(*args)
    assert scheduled == [result["id"]]


def test_the_expiry_timer_retries_while_the_reply_is_unscheduled(tmp_path, monkeypatch):
    now = [1000]
    store = bridge.Store(tmp_path / "bridge.db", "agent", clock=lambda: now[0])
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    timers, scheduled = [], []
    monkeypatch.setattr(plugin.threading, "Timer", lambda delay, function, args: timers.append((delay, function, args))
                        or types.SimpleNamespace(start=lambda: None, daemon=True))
    continuation = sys.modules[spec.name + ".continuation"]
    monkeypatch.setattr(continuation, "schedule_reply", lambda item: scheduled.append(item["request_id"]) or "job")
    request = store.request("location.current", {})
    store.defer_reply(request["id"], "Where am I?", str(tmp_path))
    plugin.follow_up_at_expiry(store, request["id"])
    # The phone claims it just before the deadline: the timer finds it still open and waits for the lease.
    now[0] = request["expires_at"] - 2
    store.claim("phone", "a" * 32)
    delay, function, args = timers.pop()
    now[0] += 3
    function(*args)
    assert scheduled == []
    delay, function, args = timers.pop()
    assert now[0] + delay > request["expires_at"] + 33  # after the lease lapses
    # Another process holds the replies lock when it fires: it tries again shortly.
    now[0] += delay
    with store.path.with_name("bridge.replies.lock").open("a") as other:
        assert continuation._try_lock(other)
        assert continuation.reconcile(store) is False
        function(*args)
        continuation._unlock(other)
    assert scheduled == []
    delay, function, args = timers.pop()
    assert delay <= 30
    now[0] += delay
    function(*args)
    assert scheduled == [request["id"]] and timers == []  # scheduled: no more retries


def test_the_expiry_timer_gives_up_after_a_bounded_number_of_tries(tmp_path, monkeypatch):
    now = [1000]
    store = bridge.Store(tmp_path / "bridge.db", "agent", clock=lambda: now[0])
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    timers = []
    monkeypatch.setattr(plugin.threading, "Timer", lambda delay, function, args: timers.append((delay, function, args))
                        or types.SimpleNamespace(start=lambda: None, daemon=True))
    continuation = sys.modules[spec.name + ".continuation"]
    def fail(item):
        raise RuntimeError("scheduler unavailable")
    monkeypatch.setattr(continuation, "schedule_reply", fail)
    request = store.request("location.current", {})
    store.defer_reply(request["id"], "Where am I?", str(tmp_path))
    plugin.follow_up_at_expiry(store, request["id"])
    fired = 0
    while timers:
        delay, function, args = timers.pop()
        now[0] += delay
        function(*args)
        fired += 1
    assert 1 < fired <= 8


def test_the_expiry_timer_prunes_old_results(tmp_path, monkeypatch):
    now = [1000]
    store = bridge.Store(tmp_path / "bridge.db", "agent", clock=lambda: now[0])
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    timers = []
    monkeypatch.setattr(plugin.threading, "Timer", lambda delay, function, args: timers.append((delay, function, args))
                        or types.SimpleNamespace(start=lambda: None, daemon=True))
    continuation = sys.modules[spec.name + ".continuation"]
    def fail(item):  # nothing marks a reply: the timer itself prunes
        raise RuntimeError("scheduler unavailable")
    monkeypatch.setattr(continuation, "schedule_reply", fail)
    old = store.request("location.current", {})
    now[0] += 86200
    request = store.request("location.current", {})
    store.defer_reply(request["id"], "Where am I?", str(tmp_path))
    plugin.follow_up_at_expiry(store, request["id"])
    delay, function, args = timers.pop()
    now[0] += delay
    function(*args)
    with pytest.raises(bridge.BridgeError):
        store.result(old["id"])
