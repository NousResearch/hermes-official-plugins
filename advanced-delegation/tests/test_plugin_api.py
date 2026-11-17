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


def test_plugin_api_get_state_and_update_decider_and_mode(api_client, home):
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

    # Switch mode via POST /mode
    mode_resp = client.post("/api/plugins/advanced-delegation/mode", json={"mode": "default"})
    assert mode_resp.status_code == 200
    assert mode_resp.json()["mode"] == "default"


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
