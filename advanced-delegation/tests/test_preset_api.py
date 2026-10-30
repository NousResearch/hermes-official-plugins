"""Preset HTTP API through the real authenticated plugin mount, isolated disk config."""
from __future__ import annotations

import shutil
from pathlib import Path

import hermes_yaml as yaml
import pytest
from starlette.testclient import TestClient


@pytest.fixture(params=["default", "work"])
def api_client(plugin_root, tmp_path, monkeypatch, request):
    from agent import secret_scope
    from hermes_cli import plugins
    from tui_gateway import launch_profile_policy as lpp

    base = tmp_path / ".hermes"
    root = base if request.param == "default" else base / "profiles" / request.param
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setattr(lpp, "_snapshot", None)
    target = root / "plugins" / "advanced-delegation"
    shutil.copytree(plugin_root, target, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    config = {
        "plugins": {"enabled": ["advanced-delegation"], "isolation": "in-process"},
        "delegation": {"backend": "model_map", "backends": {"model_map": {
            "options": [{"name": "fast", "description": "Quick", "provider": "openrouter", "model": "test-model", "api_key": "private-route-key"}],
            "default": "fast",
        }}},
    }
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    plugins._reset_plugin_managers_for_tests()
    from hermes_cli import web_server as web
    from hermes_cli.web_server_dashboard import _mount_one_plugin_api
    before = list(web.app.router.routes)
    # Use the supported discovery + mount seam, not a bespoke test-only router.
    descriptor = next(p for p in web._get_dashboard_plugins(force_rescan=True) if p["name"] == "advanced-delegation")
    _mount_one_plugin_api(web.app, descriptor, {"advanced-delegation"}, set())
    # Routes normally mount before the SPA; this fixture may import an already booted server.
    fresh = [route for route in web.app.router.routes if route not in before]
    web.app.router.routes[:] = fresh + before
    c = TestClient(web.app)
    c.params = {"profile": request.param}
    c.headers[web._SESSION_HEADER_NAME] = web._SESSION_TOKEN
    yield c, path
    web.app.router.routes[:] = before
    plugins._reset_plugin_managers_for_tests()


URL = "/api/plugins/advanced-delegation/presets"


def test_presets_crud_reads_back_real_store_without_exposing_keys(api_client):
    c, path = api_client
    assert c.get(URL).json() == {"names": [], "active": ""}
    response = c.post(URL, json={"action": "save", "name": "quick"})
    assert response.status_code == 200, response.text
    assert response.json() == {"names": ["quick"], "active": "quick"}
    assert c.get(URL).json() == response.json()
    assert "private-route-key" not in response.text
    assert c.post(URL, json={"action": "save", "name": "quick"}).status_code == 400
    data = yaml.safe_load(path.read_text())
    data["delegation"]["backends"]["model_map"]["options"][0]["model"] = "edited-model"
    path.write_text(yaml.safe_dump(data))
    assert c.get(URL).json()["active"] == ""
    assert c.post(URL, json={"action": "use", "name": "quick"}).status_code == 200
    assert yaml.safe_load(path.read_text())["delegation"]["backends"]["model_map"]["options"][0]["api_key"] == "private-route-key"
    assert c.post(URL, json={"action": "rename", "name": "quick", "new_name": "renamed"}).status_code == 200
    assert c.get(URL).json() == {"names": ["renamed"], "active": "renamed"}
    assert c.post(URL, json={"action": "delete", "name": "renamed"}).status_code == 200
    assert c.get(URL).json() == {"names": [], "active": ""}


def test_presets_require_auth_and_plugin_consent_and_reject_snapshot_payloads(api_client):
    c, path = api_client
    before = path.read_bytes()
    assert c.post(URL, json={"action": "save", "name": "x", "options": [{"api_key": "preview"}]}).status_code == 422
    assert c.post(URL, json={"action": "save", "name": "bad name"}).status_code == 400
    assert c.post(URL, json={"action": "delete", "name": "missing"}).status_code == 400
    assert path.read_bytes() == before
    c.headers.clear()
    assert c.post(URL, json={"action": "save", "name": "x"}).status_code in (401, 403)
    from hermes_cli import web_server as web
    c.headers[web._SESSION_HEADER_NAME] = web._SESSION_TOKEN
    data = yaml.safe_load(path.read_text())
    data["plugins"]["enabled"] = []
    data["plugins"]["disabled"] = ["advanced-delegation"]
    path.write_text(yaml.safe_dump(data))
    assert c.get(URL).status_code == 404
    assert c.post(URL, json={"action": "save", "name": "x"}).status_code == 404


def test_presets_crud_without_optional_public_transaction(api_client, monkeypatch):
    from hermes_cli import config

    monkeypatch.delattr(config, "update_user_config", raising=False)
    test_presets_crud_reads_back_real_store_without_exposing_keys(api_client)
