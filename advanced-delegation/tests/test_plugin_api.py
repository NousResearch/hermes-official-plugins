"""Tests for ``dashboard/plugin_api.py`` and ``desktop/plugin.js`` admission lint."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import pytest

pytest.importorskip("fastapi", reason="FastAPI not installed")

import hermes_yaml as yaml  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path):
    return tmp_path / "os-home" / ".hermes"


@pytest.fixture
def api_client(home):
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "delegation": {
                    "backend": "model_map",
                    "backends": {
                        "model_map": {
                            "options": [
                                {
                                    "name": "FastSub",
                                    "description": "Fast general worker.",
                                    "provider": "openrouter",
                                    "model": "m-fast",
                                },
                                {
                                    "name": "VisualProwess",
                                    "description": "Visual specialist.",
                                    "provider": "openrouter",
                                    "model": "m-vis",
                                },
                            ],
                            "default": "FastSub",
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    mod_name = "_test_adv_del_plugin_api"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, PLUGIN_ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)

    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/advanced-delegation")
    return TestClient(app), mod


def test_plugin_api_get_state_and_update_decider(api_client, home):
    client, _ = api_client
    resp = client.get("/api/plugins/advanced-delegation/state")
    assert resp.status_code == 200
    state = resp.json()
    assert state["mode"] == "model_map"
    assert len(state["routes"]) == 2
    assert state["decider"]["enabled"] is False

    put_resp = client.put(
        "/api/plugins/advanced-delegation/decider",
        json={
            "enabled": True,
            "url": "http://192.168.0.150:11434/v1/systemone",
            "model": "pplx-decider:27b-nvfp4",
            "specialist_threshold": 0.72,
            "confidence_threshold": 0.62,
            "excluded_options": ["UltraFastPaid"],
        },
    )
    assert put_resp.status_code == 200
    updated = put_resp.json()
    assert updated["decider"]["enabled"] is True
    assert updated["decider"]["specialist_threshold"] == 0.72

    # Invalid URL is rejected with 400
    bad_resp = client.put("/api/plugins/advanced-delegation/decider", json={"url": "ftp://bad"})
    assert bad_resp.status_code == 400

    # The delegation mode is switched in core's Settings, not through this plugin's API.
    assert client.post("/api/plugins/advanced-delegation/mode", json={"mode": "default"}).status_code == 404
    assert client.get("/api/plugins/advanced-delegation/state").json()["mode"] == "model_map"


def test_plugin_api_probe_and_clear_decisions(api_client, monkeypatch):
    client, mod = api_client
    dr = mod._decider_router()
    monkeypatch.setattr(
        dr,
        "_post_systemone_once",
        lambda **kw: {
            "model": "pplx-decider:27b-nvfp4",
            "decisions": {
                "t0_needs_specialist": {"decision": True, "probability": 0.91},
                "t0_route": {"choice": "VisualProwess", "confidence": 0.89},
            },
        },
    )
    probe = client.post(
        "/api/plugins/advanced-delegation/probe",
        json={"goal": "Inspect screenshot.png", "has_images": True},
    )
    assert probe.status_code == 200
    data = probe.json()
    assert data["ok"] is True
    assert data["upgraded"] is True
    assert data["effective_route"] == "VisualProwess"

    cleared = client.delete("/api/plugins/advanced-delegation/decisions")
    assert cleared.status_code == 200
    assert cleared.json()["ok"] is True


def test_decider_settings_survive_a_route_save_in_the_core_editor(api_client):
    """Core's Settings > Model > Subagents editor rewrites ``delegation.backends.model_map`` from its routes and
    default alone, so the Decider's settings must not live in that block."""
    core = pytest.importorskip(
        "hermes_cli.web_routers.delegation", reason="Hermes core without the delegation settings API")
    client, _ = api_client
    saved = client.put("/api/plugins/advanced-delegation/decider", json={
        "enabled": True, "url": "http://192.168.0.150:11434/v1/systemone", "excluded_options": ["VisualProwess"]})
    assert saved.status_code == 200

    editor = FastAPI()
    editor.include_router(core.router)
    routes = [
        {"name": "FastSub", "description": "Edited in the route editor.", "provider": "openrouter", "model": "m-fast"},
        {"name": "VisualProwess", "description": "Visual specialist.", "provider": "openrouter", "model": "m-vis"},
    ]
    resp = TestClient(editor).put("/api/delegation/backends/model_map", json={"options": routes, "default": "FastSub"})
    assert resp.status_code == 200

    state = client.get("/api/plugins/advanced-delegation/state").json()
    assert state["routes"][0]["description"] == "Edited in the route editor."  # the editor's save landed
    assert state["decider"]["enabled"] is True
    assert state["decider"]["url"] == "http://192.168.0.150:11434/v1/systemone"
    assert state["decider"]["excluded_options"] == ["VisualProwess"]


def test_a_save_wins_over_a_decider_block_left_in_the_route_block(api_client, home):
    """A ``decider`` block inside ``delegation.backends.model_map`` is read until the plugin saves its own
    settings; from then on the saved ones apply."""
    client, _ = api_client
    path = home / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["delegation"]["backends"]["model_map"]["decider"] = {
        "enabled": True, "url": "http://old-host:11434/v1/systemone"}
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    before = client.get("/api/plugins/advanced-delegation/state").json()["decider"]
    assert (before["enabled"], before["url"]) == (True, "http://old-host:11434/v1/systemone")

    after = client.put("/api/plugins/advanced-delegation/decider", json={"enabled": False}).json()["decider"]
    assert (after["enabled"], after["url"]) == (False, "http://old-host:11434/v1/systemone")
    on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "plugins" in on_disk
    assert on_disk["plugins"]["entries"]["advanced-delegation"]["settings"]["decider"]["enabled"] is False


def test_a_decider_save_is_refused_with_a_reason_when_the_config_cannot_be_parsed(api_client, home):
    client, _ = api_client
    path = home / "config.yaml"
    broken = "delegation: [unclosed\n"
    path.write_text(broken, encoding="utf-8")
    resp = client.put("/api/plugins/advanced-delegation/decider", json={"enabled": True})
    assert resp.status_code == 409
    assert "was not saved" in resp.json()["detail"]
    assert path.read_text(encoding="utf-8") == broken


def test_a_decider_save_is_refused_with_a_reason_in_a_managed_install(api_client, home, monkeypatch):
    client, _ = api_client
    assert client.get("/api/plugins/advanced-delegation/state").status_code == 200  # creates the home layout
    before = (home / "config.yaml").read_text(encoding="utf-8")
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "nix")
    resp = client.put("/api/plugins/advanced-delegation/decider", json={"enabled": True})
    assert resp.status_code == 409
    assert "nix" in resp.json()["detail"].lower()
    assert (home / "config.yaml").read_text(encoding="utf-8") == before


def test_a_decider_save_leaves_other_plugin_settings_alone(api_client, home):
    client, _ = api_client
    path = home / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    others = {
        "enabled": ["advanced-delegation", "other-plugin"],
        "entries": {
            "other-plugin": {"settings": {"endpoint": "http://other", "retry": {"policy": "twice"}},
                             "allow_gateway_injection": True},
            "advanced-delegation": {"allow_platform_actions": False},
        },
    }
    config["plugins"] = others
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert client.put("/api/plugins/advanced-delegation/decider", json={"enabled": True}).status_code == 200

    plugins = yaml.safe_load(path.read_text(encoding="utf-8"))["plugins"]
    assert plugins["enabled"] == others["enabled"]
    assert plugins["entries"]["other-plugin"] == others["entries"]["other-plugin"]
    assert plugins["entries"]["advanced-delegation"]["allow_platform_actions"] is False
    assert plugins["entries"]["advanced-delegation"]["settings"]["decider"]["enabled"] is True


def test_state_never_returns_a_route_credential(api_client, home):
    """The settings page shows a route's name, description and summary; a stored api_key stays in the backend."""
    client, _ = api_client
    path = home / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["delegation"]["backends"]["model_map"]["options"][0]["api_key"] = "route-credential-not-a-real-key"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    state = client.get("/api/plugins/advanced-delegation/state")
    assert state.status_code == 200
    assert [r["name"] for r in state.json()["routes"]] == ["FastSub", "VisualProwess"]
    assert "route-credential-not-a-real-key" not in state.text
    saved = client.put("/api/plugins/advanced-delegation/decider", json={"enabled": True})
    assert saved.status_code == 200
    assert "route-credential-not-a-real-key" not in saved.text


def test_making_a_route_eligible_again_is_saved(api_client):
    client, _ = api_client
    url = "/api/plugins/advanced-delegation/decider"
    assert client.put(url, json={"excluded_options": ["FastSub", "VisualProwess"]}).status_code == 200
    again = client.put(url, json={"excluded_options": ["FastSub"]})
    assert again.status_code == 200
    assert again.json()["decider"]["excluded_options"] == ["FastSub"]
    assert [r["name"] for r in again.json()["routes"] if r["decider_eligible"]] == ["VisualProwess"]
    # Every route eligible is a choice too: an empty list must not fall back to the default exclusions.
    everything = client.put(url, json={"excluded_options": []})
    assert everything.status_code == 200
    assert everything.json()["decider"]["excluded_options"] == []
    assert all(r["decider_eligible"] for r in client.get("/api/plugins/advanced-delegation/state").json()["routes"])


def test_dashboard_manifest_version_matches_the_plugin():
    import json
    manifest = json.loads((PLUGIN_ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    plugin = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["version"] == plugin["version"]


def test_desktop_plugin_js_passes_hermes_static_surface_lint():
    from hermes_cli.plugin_validate_desktop import (
        desktop_review_findings,
        desktop_surface_findings,
        desktop_surface_hits,
    )

    js_path = PLUGIN_ROOT / "desktop" / "plugin.js"
    source = js_path.read_text(encoding="utf-8")
    assert desktop_surface_findings(source) == []
    assert desktop_review_findings(source) == []
    assert desktop_surface_hits(PLUGIN_ROOT) == []
