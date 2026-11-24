"""Tests for one-call /pair dashboard routes, unified relay-settings reader, and live Hermes flow."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from push_plugin import load_dashboard_api, load_device_package, load_push_adapter

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "hermes-mobile-app-connector"


def _make_hermes_stubs(monkeypatch, root: Path, home: Path, override: Path | None = None, secrets: dict | None = None):
    bound = [override]
    constants = types.ModuleType("hermes_constants")
    constants.get_default_hermes_root = lambda: root
    constants.get_hermes_home_override = lambda: bound[-1]
    constants.get_hermes_home = lambda: Path(bound[-1] or home)
    constants.PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    constants.named_profile_is_live = lambda path: (Path(path) / "config.yaml").is_file()
    constants.profile_name_for_home = lambda path: (
        "default" if Path(path) == root else Path(path).name if Path(path).parent == root / "profiles" else None
    )
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)

    if secrets is not None:
        secret_mod = types.ModuleType("agent.secret_scope")
        secret_mod.get_secret = lambda name, default=None: secrets.get(name, default)
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
        monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_mod)
    else:
        # Ensure real Hermes secret_scope isn't leaking process env from another profile.
        secret_mod = types.ModuleType("agent.secret_scope")
        secret_mod.get_secret = lambda name, default=None: os.getenv(name, default)
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
        monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_mod)
    # Remove any cached gateway.platforms._shared so read_secret reads our stubbed scope cleanly.
    monkeypatch.delitem(sys.modules, "gateway.platforms._shared", raising=False)
    return constants


def _stub_gateway_config(monkeypatch, push_extra=None):
    """Hermes' gateway config loader, answering *push_extra* as ``platforms.push`` extras
    (``None``: no push block), so no test reads a real config.yaml."""
    config_mod = types.ModuleType("gateway.config")
    config_mod.Platform = str
    platforms = {} if push_extra is None else {"push": types.SimpleNamespace(extra=dict(push_extra))}
    config_mod.load_gateway_config = lambda **kwargs: types.SimpleNamespace(platforms=platforms)
    if "gateway" not in sys.modules:
        monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.config", config_mod)
    return config_mod


def _make_client():
    api = load_dashboard_api("hermes_mobile_pair_api_test")
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermes-mobile-app-connector")
    return TestClient(app, raise_server_exceptions=False), api


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_MOBILE_PUSH_RELAY_URL", raising=False)
    monkeypatch.delenv("HERMES_MOBILE_PUSH_INSTALL_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    work = tmp_path / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("")
    _make_hermes_stubs(monkeypatch, tmp_path, tmp_path)
    _stub_gateway_config(monkeypatch)
    return tmp_path


def test_post_pair_mints_key_stores_0600_and_get_hides_secrets(clean_env):
    client, _ = _make_client()
    before = client.get("/api/plugins/hermes-mobile-app-connector/pair")
    assert before.status_code == 200
    assert before.json() == {
        "paired": False,
        "install_id": None,
        "relay_url": None,
        "source": None,
        "notifications_ready": False,
    }
    assert "install_key" not in before.json() and "preview_key" not in before.json()

    resp = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://relay.example/", "install_key": None})
    assert resp.status_code == 200
    data = resp.json()
    key = data["install_key"]
    assert len(key) >= 32
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,256}", key)
    assert data["install_id"] == hashlib.sha256(key.encode("utf-8")).hexdigest()
    assert len(base64.b64decode(data["preview_key"], validate=True)) == 32
    assert data["relay_url"] == "https://relay.example"
    assert data["source"] == "plugin"
    assert data["notifications_ready"] is True

    pairing_file = clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json"
    assert pairing_file.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(pairing_file.stat().st_mode) == 0o600
    saved = json.loads(pairing_file.read_text(encoding="utf-8"))
    assert saved == {"install_key": key, "relay_url": "https://relay.example"}

    # GET never returns secrets.
    after = client.get("/api/plugins/hermes-mobile-app-connector/pair")
    assert after.status_code == 200
    assert after.json() == {
        "paired": True,
        "install_id": data["install_id"],
        "relay_url": "https://relay.example",
        "source": "plugin",
        "notifications_ready": True,
    }
    assert "install_key" not in after.json() and "preview_key" not in after.json()
    assert key not in after.text and data["preview_key"] not in after.text


def test_post_pair_adopts_valid_key_and_rejects_bad_key_with_400(clean_env):
    client, _ = _make_client()
    for bad_key in ("", "too-short", "a" * 31, "has spaces " + "a" * 32, "bad/char+" + "a" * 32, "a" * 32 + "\n"):
        bad = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://relay.example", "install_key": bad_key})
        assert bad.status_code == 400, f"expected 400 for {bad_key!r}, got {bad.status_code}"

    adopted_key = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-"
    resp = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://relay.example", "install_key": adopted_key})
    assert resp.status_code == 200
    data = resp.json()
    assert data["install_key"] == adopted_key
    assert data["install_id"] == hashlib.sha256(adopted_key.encode("utf-8")).hexdigest()
    assert data["source"] == "plugin"
    assert data["notifications_ready"] is True

    # Re-pairing with the same key or null keeps the key without rotating it.
    same = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://relay2.example", "install_key": adopted_key})
    assert same.status_code == 200
    assert same.json()["install_key"] == adopted_key
    assert same.json()["relay_url"] == "https://relay2.example"
    assert same.json()["preview_key"] == data["preview_key"]

    keep = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://relay3.example", "install_key": None})
    assert keep.status_code == 200
    assert keep.json()["install_key"] == adopted_key
    assert keep.json()["relay_url"] == "https://relay3.example"


def test_post_pair_returns_409_on_different_install_key_and_never_rotates(clean_env):
    client, _ = _make_client()
    first = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://relay.example", "install_key": None})
    assert first.status_code == 200
    original_key = first.json()["install_key"]

    other_key = "Z" * 43
    conflict = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://relay.example", "install_key": other_key})
    assert conflict.status_code == 409
    assert "already paired" in conflict.json()["detail"].lower()

    saved = json.loads((clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json").read_text(encoding="utf-8"))
    assert saved["install_key"] == original_key


@pytest.mark.parametrize("bad_url", [
    "",
    "http://relay.example",
    "http://localhost.relay.example",
    "ftp://relay.example",
    "file:///tmp/relay",
    "relay.example",
    "https://",
])
def test_post_pair_rejects_disallowed_relay_url_with_400(clean_env, bad_url):
    client, _ = _make_client()
    resp = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": bad_url, "install_key": None})
    assert resp.status_code == 400
    assert not (clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json").exists()


def test_post_pair_allows_localhost_http(clean_env):
    client, _ = _make_client()
    resp = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "http://127.0.0.1:8787/", "install_key": None})
    assert resp.status_code == 200
    assert resp.json()["relay_url"] == "http://127.0.0.1:8787"
    assert resp.json()["notifications_ready"] is True


def test_env_wins_nothing_written_and_conflict_checked_against_env_key(monkeypatch, clean_env):
    env_key = "EnvConfiguredKey_0123456789abcdefghijklmno"
    _make_hermes_stubs(monkeypatch, clean_env, clean_env, secrets={
        "HERMES_MOBILE_PUSH_RELAY_URL": "https://env-relay.example/",
        "HERMES_MOBILE_PUSH_INSTALL_KEY": env_key,
    })
    client, _ = _make_client()

    # Different key -> 409 even when configured by env.
    conflict = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://other.example", "install_key": "X" * 43})
    assert conflict.status_code == 409

    resp = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://app-relay.example", "install_key": None})
    assert resp.status_code == 200
    data = resp.json()
    assert data["install_key"] == env_key
    assert data["install_id"] == hashlib.sha256(env_key.encode("utf-8")).hexdigest()
    assert data["relay_url"] == "https://env-relay.example"
    assert data["source"] == "environment"
    assert data["notifications_ready"] is True
    assert not (clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json").exists()
    assert not (clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json.lock").exists()


def test_partial_env_only_writes_non_env_field_and_reports_environment_source(monkeypatch, clean_env):
    _make_hermes_stubs(monkeypatch, clean_env, clean_env, secrets={
        "HERMES_MOBILE_PUSH_RELAY_URL": "https://env-only-url.example/",
    })
    client, _ = _make_client()
    resp = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://ignored.example", "install_key": None})
    assert resp.status_code == 200
    data = resp.json()
    assert data["relay_url"] == "https://env-only-url.example"
    assert data["source"] == "environment"
    assert data["notifications_ready"] is True
    saved = json.loads((clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json").read_text(encoding="utf-8"))
    assert "relay_url" not in saved
    assert saved == {"install_key": data["install_key"]}


def test_delete_pair_removes_only_pairing_json_keeps_preview_key_and_env(monkeypatch, clean_env):
    client, _ = _make_client()
    assert client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://relay.example", "install_key": None}).status_code == 200
    pairing_file = clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json"
    preview_file = clean_env / "plugin-data" / "hermes-mobile-push" / "preview_key"
    preview_before = preview_file.read_bytes()
    assert pairing_file.is_file()

    # Delete when plugin-managed: clears pairing, keeps preview key untouched.
    deleted = client.delete("/api/plugins/hermes-mobile-app-connector/pair")
    assert deleted.status_code == 200
    assert deleted.json() == {
        "paired": False,
        "install_id": None,
        "relay_url": None,
        "source": None,
        "notifications_ready": False,
    }
    assert not pairing_file.exists()
    assert preview_file.read_bytes() == preview_before

    # When env is configured, DELETE removes any leftover pairing.json and still reports env values.
    pairing_file.write_text(json.dumps({"relay_url": "https://stale.example", "install_key": "s" * 43}))
    env_key = "EnvKeyStays_0123456789abcdefghijklmnopqrst"
    _make_hermes_stubs(monkeypatch, clean_env, clean_env, secrets={
        "HERMES_MOBILE_PUSH_RELAY_URL": "https://env.example",
        "HERMES_MOBILE_PUSH_INSTALL_KEY": env_key,
    })
    deleted_env = client.delete("/api/plugins/hermes-mobile-app-connector/pair")
    assert deleted_env.status_code == 200
    assert deleted_env.json() == {
        "paired": True,
        "install_id": hashlib.sha256(env_key.encode("utf-8")).hexdigest(),
        "relay_url": "https://env.example",
        "source": "environment",
        "notifications_ready": True,
    }
    assert not pairing_file.exists()
    assert preview_file.read_bytes() == preview_before
    assert "install_key" not in deleted_env.json() and "preview_key" not in deleted_env.json()


def test_profile_query_and_bound_override_match_device_api(monkeypatch, clean_env):
    client, _ = _make_client()
    work_resp = client.post("/api/plugins/hermes-mobile-app-connector/pair", params={"profile": "work"}, json={"relay_url": "https://work.example"})
    assert work_resp.status_code == 200
    assert (clean_env / "profiles" / "work" / "plugin-data" / "hermes-mobile-push" / "pairing.json").is_file()
    assert not (clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json").exists()
    # Preview key stays shared at the installation root.
    assert (clean_env / "plugin-data" / "hermes-mobile-push" / "preview_key").is_file()

    assert client.get("/api/plugins/hermes-mobile-app-connector/pair", params={"profile": "../bad"}).status_code == 400
    assert client.get("/api/plugins/hermes-mobile-app-connector/pair", params={"profile": "missing"}).status_code == 404

    # Bound home override wins over ?profile=, as in device_api.py.
    _make_hermes_stubs(monkeypatch, clean_env, clean_env, override=clean_env / "profiles" / "work")
    bound_status = client.get("/api/plugins/hermes-mobile-app-connector/pair", params={"profile": "default"})
    assert bound_status.status_code == 200
    assert bound_status.json()["relay_url"] == "https://work.example"


def test_settings_reader_order_across_push_adapter_and_device_wake(monkeypatch, clean_env):
    client, _ = _make_client()
    paired = client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://stored.example", "install_key": None}).json()
    stored_key = paired["install_key"]

    # 1. push/adapter.py reads stored pairing when env is unset.
    adapter, _ = load_push_adapter(clean_env)
    assert adapter._relay_settings() == ("https://stored.example", stored_key)
    assert adapter.check_requirements() is True
    assert adapter.is_connected(types.SimpleNamespace(extra={})) is True
    assert adapter.validate_config(types.SimpleNamespace(extra={})) is True
    # The enablement seed turns push on but never copies the stored pairing into the gateway's
    # config, where it would outrank a later re-pair or unpair.
    env_seed = adapter._env_enablement()
    assert set(env_seed) == {"home_channel"}
    assert env_seed["home_channel"]["chat_id"] == "device"

    # 2. device/__init__.py wake() and can_deliver_followup() use stored pairing.
    device_plugin = load_device_package("hermes_mobile_device_pair_test")
    sent = []

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, size):
            return b"{}"

    monkeypatch.setattr(device_plugin.urllib.request, "urlopen", lambda req, timeout: sent.append(req) or FakeResp())
    answer = device_plugin.wake("phone-1")
    assert answer.startswith("Background refresh requested")
    assert [r.full_url for r in sent] == ["https://stored.example/devices/phone-1/wake"]
    assert sent[0].get_header("Authorization") == f"Bearer {stored_key}"

    reg_mod = types.ModuleType("gateway.platform_registry")
    reg_mod.platform_registry = types.SimpleNamespace(is_registered=lambda name: name == "push")
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", reg_mod)
    monkeypatch.setattr(device_plugin, "cron_api_available", lambda: True)
    assert device_plugin.can_deliver_followup() is True

    # 3. Environment / scoped secret beats pairing.json everywhere.
    _make_hermes_stubs(monkeypatch, clean_env, clean_env, secrets={
        "HERMES_MOBILE_PUSH_RELAY_URL": "https://env-wins.example/",
        "HERMES_MOBILE_PUSH_INSTALL_KEY": "env-winning-key",
    })
    assert adapter._relay_settings() == ("https://env-wins.example", "env-winning-key")
    sent.clear()
    device_plugin.wake("phone-1")
    assert [r.full_url for r in sent] == ["https://env-wins.example/devices/phone-1/wake"]
    assert sent[0].get_header("Authorization") == "Bearer env-winning-key"

    # 4. Explicit PlatformConfig.extra beats env and pairing.json.
    assert adapter._relay_settings({"relay_url": "https://extra.example/", "install_key": "extra-key"}) == (
        "https://extra.example", "extra-key"
    )


def test_openapi_schemas_match_pair_responses(clean_env):
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    assert "/api/plugins/hermes-mobile-app-connector/pair" in spec["paths"]
    ops = spec["paths"]["/api/plugins/hermes-mobile-app-connector/pair"]
    assert set(ops.keys()) >= {"get", "post", "delete"}

    status_validator = Draft202012Validator(schemas["PushPairStatus"])
    result_validator = Draft202012Validator(schemas["PushPairResult"])

    client, _ = _make_client()
    status_validator.validate(client.get("/api/plugins/hermes-mobile-app-connector/pair").json())
    result_validator.validate(
        client.post("/api/plugins/hermes-mobile-app-connector/pair", json={"relay_url": "https://relay.example", "install_key": None}).json()
    )
    status_validator.validate(client.get("/api/plugins/hermes-mobile-app-connector/pair").json())
    status_validator.validate(client.delete("/api/plugins/hermes-mobile-app-connector/pair").json())


PAIR = "/api/plugins/hermes-mobile-app-connector/pair"


def _live_adapter(home, extra):
    """A connected PushAdapter built from *extra*, with sends recorded instead of posted."""
    import asyncio

    adapter_mod, _ = load_push_adapter(home)
    adapter_mod.SendResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
    sent = []
    adapter_mod._send_message = lambda url, key, content, **kwargs: sent.append((url, key)) or (200, '{"delivered":1}')
    live = adapter_mod.PushAdapter(types.SimpleNamespace(extra=extra))
    live._mark_connected = lambda: None  # the stubbed base adapter has no runtime status to publish
    assert asyncio.run(live.connect()) is True

    def send(text):
        return asyncio.run(live.send("device", text))

    return adapter_mod, live, sent, send


def test_live_adapter_follows_repair_and_unpair(clean_env):
    """R1: a connected adapter built from the gateway's seeded config sends with the current pairing."""
    client, _ = _make_client()
    first = client.post(PAIR, json={"relay_url": "https://first.example"}).json()
    key = first["install_key"]
    adapter_mod, _ = load_push_adapter(clean_env)
    seed = adapter_mod._env_enablement()
    # The extras Hermes commits for an env-enabled platform: the seed minus its home channel.
    extra = {name: value for name, value in seed.items() if name != "home_channel"}
    _, _, sent, send = _live_adapter(clean_env, extra)

    assert send("one").success is True
    assert sent[-1] == ("https://first.example", key)

    assert client.post(PAIR, json={"relay_url": "https://second.example"}).status_code == 200
    assert send("two").success is True
    assert sent[-1] == ("https://second.example", key)

    assert client.delete(PAIR).json()["paired"] is False
    count = len(sent)
    result = send("three")
    assert result.success is False
    assert "not configured" in result.error
    assert len(sent) == count


def test_live_adapter_keeps_operator_values_over_pairing(clean_env):
    """R1: values the operator put in platforms.push outrank a stored pairing, live."""
    operator_key = "OperatorKey_0123456789abcdefghijklmnopqrs"
    _, _, sent, send = _live_adapter(clean_env, {"relay_url": "https://operator.example", "install_key": operator_key})
    client, _ = _make_client()
    assert client.post(PAIR, json={"relay_url": "https://phone.example"}).status_code == 200
    assert send("x").success is True
    assert sent[-1] == ("https://operator.example", operator_key)


def test_pair_respects_platforms_push_config(monkeypatch, clean_env):
    """R2: an install key and URL in platforms.push are reported, returned and never replaced."""
    config_key = "ConfigKey_0123456789abcdefghijklmnopqrstuv"
    _stub_gateway_config(monkeypatch, {"enabled": True, "relay_url": "https://operator.example/", "install_key": config_key})
    client, _ = _make_client()
    pairing_file = clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json"

    status = client.get(PAIR)
    assert status.json() == {
        "paired": True,
        "install_id": hashlib.sha256(config_key.encode("utf-8")).hexdigest(),
        "relay_url": "https://operator.example",
        "source": "config",
        "notifications_ready": True,
    }
    assert config_key not in status.text

    resp = client.post(PAIR, json={"relay_url": "https://phone.example"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["install_key"] == config_key
    assert data["install_id"] == hashlib.sha256(config_key.encode("utf-8")).hexdigest()
    assert data["relay_url"] == "https://operator.example"
    assert data["source"] == "config"
    assert not pairing_file.exists()

    assert client.post(PAIR, json={"relay_url": "https://phone.example", "install_key": config_key}).status_code == 200
    conflict = client.post(PAIR, json={"relay_url": "https://phone.example", "install_key": "Q" * 43})
    assert conflict.status_code == 409
    assert config_key not in conflict.text
    assert not pairing_file.exists()


def test_pair_with_config_url_only_stores_just_the_key(monkeypatch, clean_env):
    """R2: a configured URL alone is the operator's; the plugin keeps only the key it mints."""
    _stub_gateway_config(monkeypatch, {"relay_url": "https://operator.example"})
    client, _ = _make_client()
    data = client.post(PAIR, json={"relay_url": "https://phone.example"}).json()
    assert data["relay_url"] == "https://operator.example"
    assert data["source"] == "config"
    saved = json.loads((clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json").read_text(encoding="utf-8"))
    assert saved == {"install_key": data["install_key"]}


def test_config_values_seeded_from_environment_still_report_environment(monkeypatch, clean_env):
    """R2: extras Hermes seeded from the environment are the environment's, not config's."""
    env_key = "EnvConfiguredKey_0123456789abcdefghijklmno"
    _make_hermes_stubs(monkeypatch, clean_env, clean_env, secrets={
        "HERMES_MOBILE_PUSH_RELAY_URL": "https://env-relay.example/",
        "HERMES_MOBILE_PUSH_INSTALL_KEY": env_key,
    })
    _stub_gateway_config(monkeypatch, {"relay_url": "https://env-relay.example", "install_key": env_key})
    client, _ = _make_client()
    assert client.get(PAIR).json()["source"] == "environment"
    assert client.post(PAIR, json={"relay_url": "https://phone.example"}).json()["source"] == "environment"


@pytest.mark.parametrize("label,body,field", [
    ("missing relay_url", {"install_key": "SecretKeyValue_0123456789abcdefghijklmn"}, "relay_url"),
    ("typed key", {"relay_url": "https://relay.example", "install_key": ["SecretKeyValue_0123456789abcdefghijklmn"]}, "install_key"),
    ("typed url", {"relay_url": ["https://SecretKeyValue_0123456789abcdefghijklmn.example"]}, "relay_url"),
])
def test_invalid_body_answers_400_naming_the_field_only(clean_env, label, body, field):
    """R3: no request value comes back in an error, and the answer is a 400."""
    client, _ = _make_client()
    resp = client.post(PAIR, json=body)
    assert resp.status_code == 400, label
    assert field in resp.json()["detail"]
    assert "SecretKeyValue_0123456789abcdefghijklmn" not in resp.text


@pytest.mark.parametrize("raw", [b"[1, 2]", b"not json SecretKeyValue_0123456789abcdefghijklmn", b'"text"', b""])
def test_body_that_is_not_a_json_object_is_a_400(clean_env, raw):
    client, _ = _make_client()
    resp = client.post(PAIR, content=raw, headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert "SecretKeyValue" not in resp.text


def test_relay_url_with_userinfo_is_refused(clean_env):
    """R4: a relay URL carrying user:pass@ is refused at POST and its credentials never echoed."""
    client, _ = _make_client()
    for url in ("https://user:hunter2pass@relay.example", "https://tokenonly@relay.example"):
        resp = client.post(PAIR, json={"relay_url": url})
        assert resp.status_code == 400
        assert "relay_url" in resp.json()["detail"]
        assert "hunter2pass" not in resp.text and "tokenonly" not in resp.text
    assert not (clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json").exists()


def test_operator_relay_url_userinfo_is_hidden_in_responses(monkeypatch, clean_env):
    """R4: an operator URL with userinfo is used as configured but shown without it."""
    env_key = "EnvConfiguredKey_0123456789abcdefghijklmno"
    _make_hermes_stubs(monkeypatch, clean_env, clean_env, secrets={
        "HERMES_MOBILE_PUSH_RELAY_URL": "https://user:hunter2pass@relay.example/base/",
        "HERMES_MOBILE_PUSH_INSTALL_KEY": env_key,
    })
    client, _ = _make_client()
    status = client.get(PAIR)
    assert status.json()["relay_url"] == "https://relay.example/base"
    posted = client.post(PAIR, json={"relay_url": "https://phone.example"})
    assert posted.json()["relay_url"] == "https://relay.example/base"
    deleted = client.delete(PAIR)
    for resp in (status, posted, deleted):
        assert "hunter2pass" not in resp.text


def test_a_token_in_the_relay_urls_query_is_never_shown(clean_env):
    """A relay URL may carry a token in its query string or fragment: it is stored and used as given,
    and every response shows scheme, host and path only."""
    client, _ = _make_client()
    url = "https://relay.example/push?token=QueryTokenNeverShown#FragmentNeverShown"
    posted = client.post(PAIR, json={"relay_url": url})
    assert posted.status_code == 200
    status = client.get(PAIR)
    for resp in (posted, status):
        assert resp.json()["relay_url"] == "https://relay.example/push"
        assert "QueryTokenNeverShown" not in resp.text and "FragmentNeverShown" not in resp.text
    stored = json.loads((clean_env / "plugin-data" / "hermes-mobile-push" / "pairing.json").read_text())
    assert stored["relay_url"].startswith("https://relay.example/push?token=QueryTokenNeverShown")


@pytest.mark.parametrize("secrets_set", [False, True])
def test_non_ascii_key_is_a_400_never_a_server_error(monkeypatch, clean_env, secrets_set):
    """R5: the supplied key's shape is checked before any comparison."""
    if secrets_set:
        _make_hermes_stubs(monkeypatch, clean_env, clean_env, secrets={
            "HERMES_MOBILE_PUSH_RELAY_URL": "https://env-relay.example",
            "HERMES_MOBILE_PUSH_INSTALL_KEY": "EnvConfiguredKey_0123456789abcdefghijklmno",
        })
    else:
        client, _ = _make_client()
        assert client.post(PAIR, json={"relay_url": "https://relay.example"}).status_code == 200
    client, _ = _make_client()
    resp = client.post(PAIR, json={"relay_url": "https://relay.example", "install_key": "\u00e9" * 32})
    assert resp.status_code == 400
    assert "install_key" in resp.json()["detail"]


def test_environment_values_keep_their_bytes(monkeypatch, clean_env):
    """R6: padded environment values reach the relay and the app exactly as set."""
    padded_key = " EnvConfiguredKey_0123456789abcdefghijklmno "
    _make_hermes_stubs(monkeypatch, clean_env, clean_env, secrets={
        "HERMES_MOBILE_PUSH_RELAY_URL": "https://env-relay.example/",
        "HERMES_MOBILE_PUSH_INSTALL_KEY": padded_key,
    })
    adapter_mod, _ = load_push_adapter(clean_env)
    assert adapter_mod._relay_settings() == ("https://env-relay.example", padded_key)
    assert adapter_mod._env_enablement()["install_key"] == padded_key
    assert adapter_mod._relay_settings({"install_key": " extra-key "}) == ("https://env-relay.example", " extra-key ")
    client, _ = _make_client()
    assert client.post(PAIR, json={"relay_url": "https://phone.example"}).json()["install_key"] == padded_key


def test_openapi_documents_config_source_and_400s(monkeypatch, clean_env):
    """R7: the documented schemas accept a config-sourced pairing, and POST answers no 422."""
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    _stub_gateway_config(monkeypatch, {"relay_url": "https://operator.example", "install_key": "C" * 43})
    client, _ = _make_client()
    Draft202012Validator(schemas["PushPairStatus"]).validate(client.get(PAIR).json())
    Draft202012Validator(schemas["PushPairResult"]).validate(client.post(PAIR, json={"relay_url": "https://phone.example"}).json())
    post = spec["paths"][PAIR]["post"]
    assert "422" not in post["responses"]
    assert "userinfo" in post["responses"]["400"]["description"]


def _hermes_available():
    try:
        return (
            importlib.util.find_spec("hermes_cli.web_server") is not None
            and importlib.util.find_spec("gateway.outbound.send") is not None
        )
    except Exception:
        return False


REAL_HERMES_DRIVER = r"""
import base64, hashlib, http.server, json, os, stat, sys, threading
from pathlib import Path
from fastapi.testclient import TestClient

os.environ.pop("HERMES_MOBILE_PUSH_RELAY_URL", None)
os.environ.pop("HERMES_MOBILE_PUSH_INSTALL_KEY", None)

recorded = []
record_event = threading.Event()

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        recorded.append({"path": self.path, "auth": self.headers.get("Authorization", ""), "body": body})
        record_event.set()
        reply = json.dumps({"status": "delivered", "delivered": 1}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)
    def log_message(self, *args):
        pass

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
relay_url = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()

from gateway.config import Platform, load_gateway_config
from gateway.outbound.send import send_message_tool
from gateway.outbound.senders import _live_adapter
from hermes_cli import plugins
from hermes_cli.plugins import invoke_hook

plugins._reset_plugin_managers_for_tests()
plugins.discover_plugins(force=True)

cfg_before = load_gateway_config()
before_enabled = bool((cfg_before.platforms.get(Platform("push")) or None) and cfg_before.platforms[Platform("push")].enabled)

import hermes_cli.web_server as server
client = TestClient(server.app)
client.__enter__()
headers = {"Authorization": f"Bearer {server._SESSION_TOKEN}", "Host": "127.0.0.1"}

get_before = client.get("/api/plugins/hermes-mobile-app-connector/pair", headers=headers).json()
post_resp = client.post("/api/plugins/hermes-mobile-app-connector/pair", headers=headers, json={"relay_url": relay_url, "install_key": None})
assert post_resp.status_code == 200, post_resp.text
pair_data = post_resp.json()
get_after = client.get("/api/plugins/hermes-mobile-app-connector/pair", headers=headers).json()

cfg_after = load_gateway_config()
after_enabled = bool((cfg_after.platforms.get(Platform("push")) or None) and cfg_after.platforms[Platform("push")].enabled)

_, live_adapter = _live_adapter(Platform("push"), "device", None)
outbound_result = json.loads(send_message_tool({"target": "push", "message": "Hello from cron delivery."}))

record_event.clear()
invoke_hook("post_llm_call", session_id="real-turn-1", turn_id="t1", assistant_response="Turn reply right after pair.", platform="gui")
invoke_hook("on_session_end", session_id="real-turn-1", turn_id="t1", completed=True, failed=False, interrupted=False, platform="gui")
hook_delivered = record_event.wait(timeout=5.0)

del_after = client.delete("/api/plugins/hermes-mobile-app-connector/pair", headers=headers).json()
cfg_deleted = load_gateway_config()
deleted_enabled = bool((cfg_deleted.platforms.get(Platform("push")) or None) and cfg_deleted.platforms[Platform("push")].enabled)

home = Path(os.environ["HERMES_HOME"])
out = {
    "before_enabled": before_enabled,
    "after_enabled": after_enabled,
    "deleted_enabled": deleted_enabled,
    "live_adapter_is_none": live_adapter is None,
    "get_before": get_before,
    "get_after": get_after,
    "del_after": del_after,
    "outbound_success": bool(outbound_result.get("success")),
    "hook_delivered": hook_delivered,
    "recorded_count": len(recorded),
    "recorded_auth_matches": all(r["auth"] == f"Bearer {pair_data['install_key']}" for r in recorded),
    "preview_key_kept": (home / "plugin-data" / "hermes-mobile-push" / "preview_key").is_file(),
    "pairing_file_deleted": not (home / "plugin-data" / "hermes-mobile-push" / "pairing.json").exists(),
}
srv.shutdown()
Path(sys.argv[1]).write_text(json.dumps(out))
os._exit(0)
"""


@pytest.mark.skipif(not _hermes_available(), reason="needs a Hermes checkout on PYTHONPATH (run on the test server)")
def test_real_hermes_pair_enables_push_and_delivers_without_restart(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(f"plugins:\n  enabled: [{PLUGIN_NAME}]\n")
    (home / ".env").write_text("")
    shutil.copytree(ROOT, home / "plugins" / PLUGIN_NAME, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("HERMES_MOBILE_PUSH_RELAY_URL", None)
    env.pop("HERMES_MOBILE_PUSH_INSTALL_KEY", None)
    result_path = tmp_path / "result.json"
    done = subprocess.run(
        [sys.executable, "-c", REAL_HERMES_DRIVER, str(result_path)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0 and result_path.exists(), done.stderr[-2000:]
    out = json.loads(result_path.read_text())
    assert out["before_enabled"] is False
    assert out["after_enabled"] is True
    assert out["deleted_enabled"] is False
    assert out["live_adapter_is_none"] is True
    assert out["get_before"]["paired"] is False
    assert out["get_after"]["paired"] is True
    assert "install_key" not in out["get_after"] and "preview_key" not in out["get_after"]
    assert out["del_after"]["paired"] is False
    assert out["outbound_success"] is True
    assert out["hook_delivered"] is True
    assert out["recorded_count"] == 2
    assert out["recorded_auth_matches"] is True
    assert out["preview_key_kept"] is True
    assert out["pairing_file_deleted"] is True
