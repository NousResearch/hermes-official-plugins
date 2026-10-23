"""Multiplex: one host gateway serves N profiles, and a served profile owns no ``gateway.pid`` of its
own. The migration's "gateway is running with active connections" warning must still fire for it
(ported from core's ``tests/hermes_cli/test_multiplex_host_topology_reporting.py``)."""

from __future__ import annotations

import json

import pytest

from agent_importers_plugin import openclaw


@pytest.fixture
def served_host(tmp_path, monkeypatch):
    """A host gateway (this PID) serving ``default`` + ``served``; the active profile is ``served``."""
    from gateway import host_rendezvous as hr

    root = tmp_path / "hermes"
    home = root / "profiles" / "served"
    home.mkdir(parents=True)
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "served")
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(locks))
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", list)
    monkeypatch.setattr("gateway.status.is_gateway_runtime_lock_active", lambda lock_path=None: False)
    assert hr.claim_host_lock(hr.ROLE_GATEWAY)[0] is hr.HostLockOutcome.ACQUIRED
    hr.publish_record(hr.ROLE_GATEWAY, profiles=("default", "served"))
    yield root
    hr.release_host_lock(hr.ROLE_GATEWAY)


def test_gateway_warning_fires_for_a_served_profile(served_host, monkeypatch):
    (served_host / "gateway_state.json").write_text(json.dumps(
        {"served_profiles": ["default", "served"],
         "platforms": {"served:telegram": {"state": "connected"}, "telegram": {"state": "connected"}}}))
    fired = []
    monkeypatch.setattr(openclaw, "_warn_running", lambda *a, **k: fired.append(a) or True)

    openclaw._warn_if_gateway_running(auto_yes=True)

    assert fired, "destructive-action warning was silently skipped for a served profile"
    assert "telegram" in fired[0][1]
