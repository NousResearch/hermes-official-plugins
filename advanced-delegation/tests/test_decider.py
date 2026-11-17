"""Tests for the System-1 Decider auto-router (``decider_router``, ``model_map``, ``cli``, ``slash``)."""

from __future__ import annotations

import argparse
import json
from typing import Any
import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

import hermes_yaml as yaml  # noqa: E402

import advanced_delegation_plugin  # noqa: E402
from advanced_delegation_plugin import cli, decider_router, slash, store  # noqa: E402
from advanced_delegation_plugin.model_map import ModelMapDelegationProvider  # noqa: E402

ROUTES = [
    {"name": "FastSub", "description": "Fast general coding and research worker.", "provider": "openrouter", "model": "m-fast"},
    {"name": "VisualProwess", "description": "Visual and screenshot analysis specialist.", "provider": "openrouter", "model": "m-vis"},
    {"name": "FrontierOpenAI", "description": "Adversarial verification and architecture.", "provider": "openai-codex", "model": "m-front"},
    {"name": "UltraFastPaid", "description": "Paid per-token worker.", "provider": "openrouter", "model": "m-paid"},
]


@pytest.fixture
def home(tmp_path):
    return tmp_path / "os-home" / ".hermes"


def test_decider_disabled_by_default_preserves_exact_upstream_behavior():
    provider = ModelMapDelegationProvider()
    cfg = {"backend": "model_map", "backends": {"model_map": {"options": ROUTES, "default": "FastSub"}}}
    params = provider.task_parameters(cfg)
    assert "System-1 Decider" not in params["option"]["description"]
    assert provider.description(cfg) == "Each task may set `option` to run that subagent on one of the listed models."
    assert provider.route_task({"goal": "Write a unit test"}, cfg) == {
        "provider": "openrouter",
        "model": "m-fast",
        "label": "FastSub",
    }


def test_decider_enabled_updates_schema_and_upgrades_specialist_task(monkeypatch, home):
    provider = ModelMapDelegationProvider()
    cfg = {
        "backend": "model_map",
        "backends": {
            "model_map": {
                "options": ROUTES,
                "default": "FastSub",
                "decider": {
                    "enabled": True,
                    "url": "http://192.168.0.150:11434/v1/systemone",
                    "model": "pplx-decider:27b-nvfp4",
                    "specialist_threshold": 0.70,
                    "confidence_threshold": 0.60,
                },
            }
        },
    }
    params = provider.task_parameters(cfg)
    assert "System-1 Decider" in params["option"]["description"]
    assert "System-1 Decider" in provider.description(cfg)

    captured_calls: list[dict[str, Any]] = []

    def fake_post(*, url, model, state, questions, timeout, api_key=""):
        captured_calls.append({"url": url, "model": model, "state": state, "questions": questions})
        # UltraFastPaid must be excluded from auto-selection
        assert "UltraFastPaid" not in questions["t0_route"]["criteria"]
        return {
            "model": model,
            "decisions": {
                "t0_needs_specialist": {"decision": True, "probability": 0.94},
                "t0_route": {"choice": "FrontierOpenAI", "confidence": 0.88},
            },
        }

    monkeypatch.setattr(decider_router, "_post_systemone_once", fake_post)
    routed = provider.route_task({"goal": "Perform adversarial security verification of auth flow"}, cfg)
    assert routed == {"provider": "openai-codex", "model": "m-front", "label": "FrontierOpenAI"}
    assert len(captured_calls) == 1

    log_rows = decider_router.read_decision_log(limit=5)
    assert len(log_rows) == 1
    assert log_rows[0]["action"] == "upgraded"
    assert log_rows[0]["route"] == "FrontierOpenAI"


def test_decider_never_sets_missing_default_when_custom_route_names_are_used(monkeypatch):
    """Regression test: when options are named fast/deep (no FastSub) and default is unset,
    keeping a task on default must return {} (inherit parent) and never raise KeyError."""
    provider = ModelMapDelegationProvider()
    custom_routes = [
        {"name": "fast", "description": "Quick worker.", "provider": "openrouter", "model": "m1"},
        {"name": "deep", "description": "Deep thinker.", "provider": "anthropic", "model": "m2"},
    ]
    cfg = {
        "backend": "model_map",
        "backends": {
            "model_map": {
                "options": custom_routes,
                "decider": {"enabled": True},
            }
        },
    }

    monkeypatch.setattr(
        decider_router,
        "_post_systemone_once",
        lambda **kw: {
            "decisions": {
                "t0_needs_specialist": {"decision": False, "probability": 0.12},
                "t0_route": {"choice": "fast", "confidence": 0.90},
            }
        },
    )
    assert provider.route_task({"goal": "Simple typo fix"}, cfg) == {}


def test_batch_pre_tool_call_routes_multiple_tasks_and_returns_modify_directive(monkeypatch, home):
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "delegation": {
                    "backend": "model_map",
                    "backends": {
                        "model_map": {
                            "options": ROUTES,
                            "default": "FastSub",
                            "decider": {"enabled": True},
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    def fake_post(*, url, model, state, questions, timeout, api_key=""):
        # For >1 tasks, per-question criteria use compact keys while full descriptions live in state.available_routes
        assert questions["t0_route"]["criteria"]["VisualProwess"] == "Route VisualProwess"
        assert "screenshot" in state["available_routes"]["VisualProwess"]
        return {
            "decisions": {
                "t0_needs_specialist": {"decision": True, "probability": 0.92},
                "t0_route": {"choice": "VisualProwess", "confidence": 0.91},
                "t1_needs_specialist": {"decision": False, "probability": 0.15},
                "t1_route": {"choice": "FastSub", "confidence": 0.85},
            }
        }

    monkeypatch.setattr(decider_router, "_post_systemone_once", fake_post)
    args = {
        "action": "spawn",
        "tasks": [
            {"goal": "Inspect UI screenshot.png", "images": ["/tmp/screenshot.png"]},
            {"goal": "Run pytest and summarize"},
            {"goal": "Already pinned", "option": "FrontierOpenAI"},
        ],
    }
    res = advanced_delegation_plugin._on_pre_tool_call("delegate_task", args)
    assert res is not None and res["action"] == "modify"
    modified_tasks = res["args"]["tasks"]
    assert modified_tasks[0]["option"] == "VisualProwess"
    assert modified_tasks[1]["option"] == "FastSub"
    assert modified_tasks[2]["option"] == "FrontierOpenAI"


def test_cloud_fallback_and_cooldown_when_primary_endpoint_fails(monkeypatch):
    monkeypatch.setattr(decider_router, "_LAN_COOLDOWN_UNTIL", 0.0)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key")
    calls: list[str] = []

    def fake_post(*, url, model, state, questions, timeout, api_key=""):
        calls.append(url)
        if "192.168." in url:
            raise TimeoutError("LAN timeout")
        # TypeSafe Cloud requires `criterion` (singular) on `noul` questions
        adapted = decider_router._adapt_questions_for_endpoint(questions, url)
        assert "criterion" in adapted["t0_needs_specialist"]
        return {
            "model": model,
            "decisions": {
                "t0_needs_specialist": {"decision": True, "probability": 0.88},
                "t0_route": {"choice": "FrontierOpenAI", "confidence": 0.82},
            },
        }

    monkeypatch.setattr(decider_router, "_post_systemone_once", fake_post)
    settings = {
        "options": ROUTES,
        "default": "FastSub",
        "decider": {
            "enabled": True,
            "url": "http://192.168.0.150:11434/v1/systemone",
            "fallback_url": "https://api.typesafe.ai/v1/systemone",
            "allow_fallback": True,
        },
    }
    probe1 = decider_router.probe_decider("Verify cryptographic proof", settings=settings)
    assert probe1["ok"] is True
    assert probe1["fallback_used"] is True
    assert probe1["effective_route"] == "FrontierOpenAI"
    assert calls == [
        "http://192.168.0.150:11434/v1/systemone",
        "https://api.typesafe.ai/v1/systemone",
    ]

    # Second call within the 45s cooldown skips the timed-out LAN endpoint directly
    probe2 = decider_router.probe_decider("Verify second proof", settings=settings)
    assert probe2["ok"] is True and probe2["fallback_used"] is True
    assert calls[-1] == "https://api.typesafe.ai/v1/systemone"
    assert len(calls) == 3
    monkeypatch.setattr(decider_router, "_LAN_COOLDOWN_UNTIL", 0.0)


def test_cli_and_slash_decider_commands(monkeypatch, capsys, home):
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "delegation": {
                    "backend": "model_map",
                    "backends": {"model_map": {"options": ROUTES, "default": "FastSub"}},
                }
            }
        ),
        encoding="utf-8",
    )

    parser = argparse.ArgumentParser(prog="hermes delegation")
    cli.setup_parser(parser)
    args = parser.parse_args(
        [
            "decider",
            "--enable",
            "--url",
            "http://192.168.0.150:11434/v1/systemone",
            "--threshold",
            "0.75",
            "--confidence",
            "0.65",
        ]
    )
    assert cli.handle(args) == 0
    out = capsys.readouterr().out
    assert "System-1 Decider auto-router: ON" in out
    assert "0.75" in out

    # Slash command `/delegation decider status` and `/delegation decider off`
    status_txt = slash.handle("decider status")
    assert "System-1 Decider auto-router: ON" in status_txt
    off_txt = slash.handle("decider off")
    assert "OFF" in off_txt
    assert store.decider_settings(store.delegation_section())["enabled"] is False
