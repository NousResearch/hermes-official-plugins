"""browser_signed_in_status: read-only per-origin cookie status, values never returned."""

from __future__ import annotations

import json
import time
import uuid

import pytest

from camofox_plugin import camofox_client as client
from camofox_plugin import camofox_tools as tools

MANAGED = "browser:\n  camofox:\n    managed_persistence: true\n"


def _storage_state():
    now = time.time()
    return {"cookies": [
        {"name": "user_session", "value": "SECRET-VALUE-1", "domain": "github.com", "path": "/",
         "expires": now + 3600, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        {"name": "logged_in", "value": "SECRET-VALUE-2", "domain": ".github.com", "path": "/",
         "expires": now + 60, "httpOnly": False, "secure": True, "sameSite": "Lax"},
        {"name": "_ga", "value": "SECRET-VALUE-3", "domain": "github.com", "path": "/",
         "expires": -1, "httpOnly": False, "secure": False, "sameSite": "None"},
        {"name": "sid", "value": "SECRET-VALUE-4", "domain": "example.com", "path": "/",
         "expires": now + 99, "httpOnly": False, "secure": False, "sameSite": "Lax"},
    ], "origins": []}


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("CAMOFOX_API_KEY", "test-key")


def _managed_user() -> str:
    return client.get_camofox_identity(None)["user_id"]


def test_reports_signed_in_with_earliest_expiry_and_no_values(camofox, api_key, write_config):
    write_config(MANAGED)
    camofox.storage_states[_managed_user()] = _storage_state()
    out = json.loads(tools.browser_signed_in_status("https://github.com/x/y", task_id="t1"))
    assert out["success"] is True
    assert out["origin"] == "github.com"
    assert out["signed_in"] is True
    assert out["cookie_count"] == 3
    assert out["cookie_names"] == ["_ga", "logged_in", "user_session"]
    assert out["active_cookie_count"] == 3
    assert out["session_cookie_count"] == 1
    assert out["earliest_expiry_cookie"] == "logged_in"
    assert out["earliest_expiry"].endswith("Z")
    assert "SECRET" not in json.dumps(out)
    assert "value" not in out


def test_bare_host_origin_and_other_sites_cookies_mean_signed_out(camofox, api_key, write_config):
    write_config(MANAGED)
    state = _storage_state()
    camofox.storage_states[_managed_user()] = {
        "cookies": [c for c in state["cookies"] if c["name"] == "sid"]}
    out = json.loads(tools.browser_signed_in_status("github.com"))
    assert out["signed_in"] is False
    assert out["cookie_count"] == 0
    assert out["cookie_names"] == []


def test_expired_cookies_do_not_count_as_signed_in(camofox, api_key, write_config):
    write_config(MANAGED)
    state = _storage_state()
    for cookie in state["cookies"]:
        if cookie["domain"].lstrip(".") == "github.com" and cookie["expires"] != -1:
            cookie["expires"] = time.time() - 10
    camofox.storage_states[_managed_user()] = state
    out = json.loads(tools.browser_signed_in_status("github.com"))
    # The session cookie never expires, so the profile still counts as signed in; the expiring
    # auth cookies are gone and nothing reports an expiry any more.
    assert out["signed_in"] is True
    assert out["expired_count"] == 2
    assert out["active_cookie_count"] == 1
    assert "earliest_expiry" not in out


def test_requires_the_bearer_token(camofox, api_key, monkeypatch):
    monkeypatch.delenv("CAMOFOX_API_KEY", raising=False)
    out = json.loads(tools.browser_signed_in_status("github.com"))
    assert out["success"] is False
    assert "CAMOFOX_API_KEY" in out["error"]


def test_bad_origin_fails_cleanly(camofox, api_key):
    out = json.loads(tools.browser_signed_in_status("   "))
    assert out["success"] is False
    assert "origin is required" in out["error"]
    out = json.loads(tools.browser_signed_in_status("ftp://x"))
    assert out["success"] is False


def test_no_durable_identity_fails_with_the_fix(camofox, api_key):
    out = json.loads(tools.browser_signed_in_status("github.com"))
    assert out["success"] is False
    assert "managed_persistence" in out["error"]


def test_task_scoped_identity_resolves_deterministically(camofox, api_key):
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"camofox-task:{client.get_camofox_state_dir()}:t9").hex[:10]
    user = f"hermes_{digest}"
    camofox.storage_states[user] = _storage_state()
    out = json.loads(tools.browser_signed_in_status("github.com", task_id="t9"))
    assert out["user_id"] == user
    assert out["identity_source"] == "task"
    assert out["signed_in"] is True


def test_explicit_user_id_checks_that_profile(camofox, api_key):
    camofox.storage_states["hermes_ag_abc"] = _storage_state()
    out = json.loads(tools.browser_signed_in_status("github.com", user_id="hermes_ag_abc"))
    assert out["user_id"] == "hermes_ag_abc"
    assert out["identity_source"] == "user_id"
    assert out["signed_in"] is True


def test_unknown_without_active_session(camofox, api_key, write_config):
    write_config(MANAGED)
    out = json.loads(tools.browser_signed_in_status("github.com"))
    assert out["success"] is True
    assert out["signed_in"] is None
    assert out["reason"] == "no_active_session"
    assert "open_session" in out["note"]
    assert camofox.calls("POST", "/tabs") == []


def test_open_session_loads_the_profile_then_reports(camofox, api_key, write_config):
    write_config(MANAGED)
    user = _managed_user()
    camofox.storage_states[user] = _storage_state()
    camofox.no_session_users.add(user)
    out = json.loads(tools.browser_signed_in_status("github.com", open_session=True))
    assert out["signed_in"] is True
    created = camofox.calls("POST", "/tabs")
    assert created and created[0]["body"]["userId"] == user
    assert created[0]["body"]["sessionKey"].startswith("status_")


def test_open_session_failure_is_reported(camofox, api_key, write_config):
    write_config(MANAGED)
    user = _managed_user()
    camofox.no_session_users.add(user)
    camofox.overrides[("POST", "/tabs")] = (500, {"error": "boom"})
    out = json.loads(tools.browser_signed_in_status("github.com", open_session=True))
    assert out["success"] is False
    assert "could not load the profile" in out["error"]


def test_handler_passes_task_id(camofox, api_key, write_config):
    write_config(MANAGED)
    camofox.storage_states[_managed_user()] = _storage_state()
    out = json.loads(tools.handle_signed_in_status({"origin": "github.com"}, task_id="t2"))
    assert out["success"] is True and out["signed_in"] is True


def test_cli_signed_in_yes(camofox, api_key, write_config, capsys):
    from camofox_plugin import camofox_cli as cli
    write_config(MANAGED)
    camofox.storage_states[_managed_user()] = _storage_state()
    assert cli.cmd_signed_in("github.com", None, False) == 0
    out = capsys.readouterr().out
    assert "Signed in: yes" in out and "logged_in" in out and "SECRET" not in out


def test_cli_signed_in_unknown(camofox, api_key, write_config, capsys):
    from camofox_plugin import camofox_cli as cli
    write_config(MANAGED)
    assert cli.cmd_signed_in("github.com", None, False) == 0
    out = capsys.readouterr().out
    assert "unknown" in out


def test_cli_signed_in_error(camofox, capsys):
    from camofox_plugin import camofox_cli as cli
    assert cli.cmd_signed_in("github.com", None, False) == 1
    assert "Signed-in check failed" in capsys.readouterr().out
