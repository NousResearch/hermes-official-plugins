"""Device tools stay hidden on homes with no paired device and never create bridge.db on probe."""
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from push_plugin import load_device_package

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "hermes-mobile-app-connector"
DATA_NAME = "hermes-mobile-device-access"
TOKEN = "a" * 32

ALL_DEVICE_TOOLS = {
    "mobile_devices",
    "mobile_request_result",
    "mobile_calendar_events",
    "mobile_calendar_create",
    "mobile_reminders_list",
    "mobile_reminder_create",
    "mobile_reminder_complete",
    "mobile_reminder_update",
    "mobile_health_summary",
    "mobile_health_log",
    "mobile_location",
    "mobile_contacts_search",
    "mobile_alarms_list",
    "mobile_alarm_create",
    "mobile_timer_start",
    "mobile_alarm_cancel",
}


def _has_hermes_discovery():
    try:
        return importlib.util.find_spec("model_tools") is not None
    except Exception:
        return False


def _stub_hermes_home(monkeypatch, home):
    bound = [None]
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: Path(bound[-1] or home)
    constants.set_hermes_home_override = lambda path: bound.append(path) or len(bound) - 1
    constants.reset_hermes_home_override = lambda token: bound.__delitem__(slice(token, None))
    constants.profile_name_for_home = lambda path: "default"
    storage = types.ModuleType("plugins.plugin_storage")

    def plugin_data_dir(name):
        directory = constants.get_hermes_home() / "plugin-data" / name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    storage.plugin_data_dir = plugin_data_dir
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)


def test_all_device_tools_gate_on_paired_device_without_creating_bridge_db(tmp_path, monkeypatch):
    plugin = load_device_package("hermes_mobile_gating_unit")
    bridge = sys.modules["hermes_mobile_gating_unit.bridge"]
    home = tmp_path / "fresh-home"
    home.mkdir()
    _stub_hermes_home(monkeypatch, home)

    tools = {}

    class Context:
        def register_tool(self, **tool):
            tools[tool["name"]] = tool

    plugin.register(Context())
    assert set(tools) == ALL_DEVICE_TOOLS

    # All 15 non-update tools share one check_fn (one cached probe per pass); update uses can_update_reminders.
    for name, entry in tools.items():
        if name == "mobile_reminder_update":
            assert entry["check_fn"] is plugin.can_update_reminders
        else:
            assert entry["check_fn"] is plugin.has_paired_device

    # Unpaired home: every tool's check_fn returns False and neither plugin-data nor bridge.db is created.
    assert [name for name, entry in tools.items() if entry["check_fn"]()] == []
    assert not (home / "plugin-data").exists()

    # After pairing a device without reminders.update: the 15 base tools pass, update stays off.
    store = bridge.open_store(home)
    store.register("phone", "Phone", ["location.read"], TOKEN)
    offered = {name for name, entry in tools.items() if entry["check_fn"]()}
    assert offered == ALL_DEVICE_TOOLS - {"mobile_reminder_update"}

    # After enabling reminders.write + reminders.update feature: all 16 tools pass.
    store.register("phone", "Phone", ["reminders.write"], TOKEN, features=["reminders.update"])
    offered = {name for name, entry in tools.items() if entry["check_fn"]()}
    assert offered == ALL_DEVICE_TOOLS


def test_readonly_device_check_handles_empty_db_and_does_not_block_on_writer(tmp_path):
    plugin = load_device_package("hermes_mobile_gating_readonly")
    bridge = sys.modules["hermes_mobile_gating_readonly.bridge"]

    # Missing file -> [] and file is not created.
    missing = tmp_path / "missing" / "bridge.db"
    assert bridge.read_devices_readonly(missing) == []
    assert not missing.exists()

    # Brand-new 0-byte file (mid-init, no devices table yet) -> [].
    blank = tmp_path / "blank.db"
    blank.write_bytes(b"")
    assert bridge.read_devices_readonly(blank) == []

    # Initialized store with 0 devices -> [].
    store = bridge.Store(tmp_path / "bridge.db")
    assert bridge.read_devices_readonly(store.path) == []

    # Active writer holding BEGIN IMMEDIATE does not block read_devices_readonly.
    store.register("phone", "Phone", ["location.read"], TOKEN)
    writer = sqlite3.connect(store.path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE devices SET name='Updated' WHERE id='phone'")
        start = time.monotonic()
        devices = bridge.read_devices_readonly(store.path)
        elapsed = time.monotonic() - start
        assert len(devices) == 1
        assert elapsed < 0.5
    finally:
        writer.rollback()
        writer.close()


def test_a_briefly_locked_store_keeps_a_paired_phones_tools(tmp_path, monkeypatch):
    plugin = load_device_package("hermes_mobile_gating_locked")
    bridge = sys.modules["hermes_mobile_gating_locked.bridge"]
    _stub_hermes_home(monkeypatch, tmp_path)
    store = bridge.open_store(tmp_path)
    store.register("phone", "Phone", ["location.read"], TOKEN)
    store_file = bridge.store_path(tmp_path)
    holder = sqlite3.connect(store_file, isolation_level=None)
    try:
        holder.execute("PRAGMA journal_mode=DELETE")
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError):
            bridge.read_devices_readonly(store_file)
        assert plugin.has_paired_device() is True
    finally:
        holder.rollback()
        holder.close()
    assert plugin.has_paired_device() is True


DISCOVERY_PROBE = """
import importlib.util, json, os, sys
from pathlib import Path
import model_tools
from tools.registry import invalidate_check_fn_cache

def mobile_names():
    defs = model_tools.get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True)
    return sorted(d["function"]["name"] for d in defs if d["function"]["name"].startswith("mobile_"))

home = Path(os.environ["HERMES_HOME"])
bridge_path = home / "plugin-data" / "hermes-mobile-device-access" / "bridge.db"
before = mobile_names()
before_db_exists = bridge_path.exists()

spec = importlib.util.spec_from_file_location("probe_bridge", home / "plugins" / "hermes-mobile-app-connector" / "device" / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

store = bridge.open_store(home)
store.register("phone", "Phone", ["location.read"], "a" * 32)
invalidate_check_fn_cache()
after_pair = mobile_names()

store.register("phone", "Phone", ["reminders.write"], "a" * 32, features=["reminders.update"])
invalidate_check_fn_cache()
after_update_feature = mobile_names()

Path(sys.argv[1]).write_text(json.dumps({
    "before": before,
    "before_db_exists": before_db_exists,
    "after_pair": after_pair,
    "after_update_feature": after_update_feature,
}))
os._exit(0)
"""


@pytest.mark.skipif(not _has_hermes_discovery(), reason="needs a Hermes checkout on PYTHONPATH")
def test_hermes_discovery_offers_zero_device_tools_until_a_device_is_paired(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(f"plugins:\n  enabled: [{PLUGIN_NAME}]\n")
    (home / ".env").write_text("")
    shutil.copytree(ROOT, home / "plugins" / PLUGIN_NAME, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    out = tmp_path / "probe.json"
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(
        [sys.executable, "-c", DISCOVERY_PROBE, str(out)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0 and out.exists(), done.stderr[-2000:]
    result = json.loads(out.read_text())
    assert result["before"] == []
    assert result["before_db_exists"] is False
    assert set(result["after_pair"]) == ALL_DEVICE_TOOLS - {"mobile_reminder_update"}
    assert set(result["after_update_feature"]) == ALL_DEVICE_TOOLS
