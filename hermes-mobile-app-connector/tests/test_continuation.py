"""Follow-up scheduling through cron.plugin_api, with feature detection when core lacks it."""
import asyncio
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

import pytest
from push_plugin import load_device_module, load_device_package


def _has_cron_plugin_api():
    try:
        return importlib.util.find_spec("cron.plugin_api") is not None
    except Exception:
        return False


def _install_fake_cron_api(monkeypatch, ensure_named_job, *, ambiguous_cls=None, reg_error_cls=None):
    cron_pkg = types.ModuleType("cron")
    cron_api = types.ModuleType("cron.plugin_api")
    cron_api.ensure_named_job = ensure_named_job
    if ambiguous_cls is not None:
        cron_api.AmbiguousJobReference = ambiguous_cls
    if reg_error_cls is not None:
        cron_api.CronSchedulerRegistrationError = reg_error_cls
    cron_pkg.plugin_api = cron_api
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.plugin_api", cron_api)
    return cron_api


def test_schedule_reply_passes_all_arguments_and_maps_contract_errors(monkeypatch, tmp_path):
    continuation = load_device_module("device_continuation_contract", "continuation.py")
    bridge = load_device_module("device_bridge_contract", "bridge.py")

    class FakeAmbiguous(RuntimeError):
        pass

    class FakeRegError(RuntimeError):
        def __init__(self, job):
            self.job = job
            super().__init__("registration failed")

    # 1. FileNotFoundError -> RuntimeError("Original profile no longer exists")
    def raise_missing(name, **kwargs):
        raise FileNotFoundError(kwargs["home"])

    _install_fake_cron_api(monkeypatch, raise_missing, ambiguous_cls=FakeAmbiguous)
    with pytest.raises(RuntimeError, match="Original profile no longer exists"):
        continuation.schedule_reply({"request_id": "r1", "home": str(tmp_path / "gone"), "prompt": "Where?"})

    # 2. AmbiguousJobReference -> RuntimeError("Ambiguous continuation jobs; do not schedule another")
    def raise_ambiguous(name, **kwargs):
        raise FakeAmbiguous(name)

    _install_fake_cron_api(monkeypatch, raise_ambiguous, ambiguous_cls=FakeAmbiguous)
    with pytest.raises(RuntimeError, match="Ambiguous continuation jobs; do not schedule another"):
        continuation.schedule_reply({"request_id": "r1", "home": str(tmp_path), "prompt": "Where?"})

    # 3. ValueError (bad name) propagates and reconcile leaves the reply unscheduled
    def raise_bad_name(name, **kwargs):
        raise ValueError(f"bad name: {name}")

    _install_fake_cron_api(monkeypatch, raise_bad_name, ambiguous_cls=FakeAmbiguous)
    with pytest.raises(ValueError, match="bad name"):
        continuation.schedule_reply({"request_id": "r1 ", "home": str(tmp_path), "prompt": "Where?"})

    # 4. CronSchedulerRegistrationError propagates so reconcile retries registration on the saved job
    store = bridge.Store(tmp_path / "bridge.db")
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    req = store.request("location.current", {})
    store.defer_reply(req["id"], "Where am I?", str(tmp_path))
    claim = store.claim("phone", "a" * 32)[0]
    store.complete("phone", "a" * 32, req["id"], claim["lease"], {"status": "succeeded"})

    attempts = []

    def fail_once_then_register(name, **kwargs):
        attempts.append((name, kwargs))
        if len(attempts) == 1:
            raise FakeRegError({"id": "saved-job-1", "name": name})
        return {"id": "saved-job-1", "name": name}

    _install_fake_cron_api(
        monkeypatch, fail_once_then_register, ambiguous_cls=FakeAmbiguous, reg_error_cls=FakeRegError
    )
    assert continuation.reconcile(store) is True
    assert len(store.ready_replies()) == 1  # not marked yet; registration failed
    assert continuation.reconcile(store) is True
    assert store.ready_replies() == []
    assert [name for name, _ in attempts] == [
        f"hermes-mobile-device-access-{req['id']}",
        f"hermes-mobile-device-access-{req['id']}",
    ]


def test_missing_cron_seam_does_not_raise_or_schedule_and_logs_once_at_debug(monkeypatch, tmp_path, caplog):
    continuation = load_device_module("device_continuation_missing_seam", "continuation.py")
    bridge = load_device_module("device_bridge_missing_seam", "bridge.py")
    monkeypatch.setitem(sys.modules, "cron.plugin_api", None)
    continuation._seam_missing_logged = False

    store = bridge.Store(tmp_path / "bridge.db")
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    req = store.request("location.current", {})
    store.defer_reply(req["id"], "Where am I?", str(tmp_path))
    claim = store.claim("phone", "a" * 32)[0]
    store.complete("phone", "a" * 32, req["id"], claim["lease"], {"status": "succeeded"})

    with caplog.at_level(logging.DEBUG, logger=continuation.logger.name):
        assert continuation.cron_api_available() is False
        assert continuation.schedule_reply({"request_id": req["id"], "home": str(tmp_path), "prompt": "Where?"}) is None
        assert continuation.reconcile(store) is True
        assert continuation.reconcile(store) is True

    # Nothing raised, nothing scheduled, and logged once at debug (never warning).
    assert [item["request_id"] for item in store.ready_replies()] == [req["id"]]
    assert not (tmp_path / "cron" / "jobs.json").exists()
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG and "cron.plugin_api" in r.getMessage()]
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(debug_records) == 1
    assert warning_records == []


def test_pending_tool_result_without_cron_seam_omits_followup_and_does_not_defer(monkeypatch, tmp_path):
    plugin = load_device_package("hermes_mobile_missing_cron_seam_tool")
    bridge = sys.modules["hermes_mobile_missing_cron_seam_tool.bridge"]
    continuation = sys.modules["hermes_mobile_missing_cron_seam_tool.continuation"]
    continuation._seam_missing_logged = False

    monkeypatch.setitem(sys.modules, "cron.plugin_api", None)
    secrets_mod = types.ModuleType("agent.secret_scope")
    secrets_mod.get_secret = lambda name, default=None: {
        "HERMES_MOBILE_PUSH_RELAY_URL": "https://relay.example",
        "HERMES_MOBILE_PUSH_INSTALL_KEY": "install-key",
    }.get(name, default)
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secrets_mod)
    registry_mod = types.ModuleType("gateway.platform_registry")
    registry_mod.platform_registry = types.SimpleNamespace(is_registered=lambda p: p == "push")
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", registry_mod)

    assert plugin.can_deliver_followup() is False

    store = bridge.Store(tmp_path / "bridge.db")
    store.register("phone", "Phone", ["location.read"], "a" * 32)
    handlers = {}

    class Context:
        def register_tool(self, **tool):
            handlers[tool["name"]] = tool["handler"]

    plugin.register(Context())
    monkeypatch.setattr(plugin, "store", lambda: store)
    monkeypatch.setattr(plugin, "wake", lambda _: "Background refresh requested; iOS delivery is best effort.")

    result = json.loads(asyncio.run(handlers["mobile_location"]({"wait_seconds": 0})))
    assert result["status"] == "pending"
    assert "Request saved, not completed." in result["message"]
    assert "followup" not in result
    assert store.reply_due(result["id"]) is None


@pytest.mark.skipif(not _has_cron_plugin_api(), reason="needs hermes-agent PR 159657")
def test_real_cron_plugin_api_schedules_once_and_maps_errors(tmp_path):
    continuation = load_device_module("device_continuation_real_seam", "continuation.py")
    home = tmp_path / "profile-home"
    home.mkdir()
    (home / "config.yaml").write_text("timezone: UTC\n")
    (home / ".env").write_text("")

    # 1. Creates one job and returns the same job ID on repeat calls (idempotent).
    item = {"request_id": "req123", "home": str(home), "prompt": "Check my steps"}
    first_id = continuation.schedule_reply(item)
    second_id = continuation.schedule_reply(item)
    assert first_id and first_id == second_id

    jobs_file = home / "cron" / "jobs.json"
    data = json.loads(jobs_file.read_text())
    jobs = data["jobs"] if isinstance(data, dict) else data
    assert len(jobs) == 1
    assert jobs[0]["id"] == first_id
    assert jobs[0]["name"] == "hermes-mobile-device-access-req123"
    assert jobs[0]["deliver"] == "push"
    assert jobs[0]["enabled_toolsets"] == ["mobile_device"]
    assert "req123" in jobs[0]["prompt"] and "Check my steps" in jobs[0]["prompt"]

    # 2. Missing home raises RuntimeError("Original profile no longer exists") without creating it.
    missing = tmp_path / "missing-home"
    with pytest.raises(RuntimeError, match="Original profile no longer exists"):
        continuation.schedule_reply({"request_id": "req456", "home": str(missing), "prompt": "Where?"})
    assert not missing.exists()

    # 3. Duplicate jobs with the same name raise RuntimeError("Ambiguous continuation jobs; do not schedule another").
    dup = {**jobs[0], "id": "duplicate-id"}
    if isinstance(data, dict):
        data["jobs"].append(dup)
    else:
        data.append(dup)
    jobs_file.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="Ambiguous continuation jobs; do not schedule another"):
        continuation.schedule_reply(item)
