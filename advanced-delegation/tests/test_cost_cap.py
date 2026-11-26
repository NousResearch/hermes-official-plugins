"""Cost-cap guard: ``specialist`` flag parsing + over-cap / tool-less refusal.

Runs only inside a Hermes core with pluggable delegation providers (same gate as the
other model_map tests). Pricing is monkeypatched so the tests never hit the network.
"""
import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

from agent.delegation_provider import DelegationRouteError  # noqa: E402

import advanced_delegation_plugin.model_map as mm  # noqa: E402
import advanced_delegation_plugin.pricing as pricing  # noqa: E402
from advanced_delegation_plugin.model_map import ModelMapDelegationProvider, _cost_guard  # noqa: E402

OPTIONS = [
    {"name": "fast", "description": "Cheap.", "provider": "openrouter", "model": "google/gemini-2.5-flash-lite"},
    {"name": "deep", "description": "Slow.", "provider": "openrouter", "model": "anthropic/claude-sonnet-5.5"},
    {"name": "research", "description": "Search.", "provider": "openrouter", "model": "perplexity/sonar-pro",
     "specialist": True},
    {"name": "local", "description": "Private.", "base_url": "http://localhost:8080/v1", "model": "qwen3-coder"},
]


def _config(**settings):
    return {"backend": "model_map", "backends": {"model_map": settings}}


def test_specialist_flag_parsed():
    provider = ModelMapDelegationProvider()
    opts = mm.configured_options(provider.settings(_config(options=OPTIONS)))
    assert opts["research"]["specialist"] is True
    assert opts["deep"]["specialist"] is False
    assert "specialist" in provider.route_keys


def test_over_cap_refused(monkeypatch):
    monkeypatch.setattr(pricing, "verdict", lambda m: (2.00, True))
    with pytest.raises(DelegationRouteError, match="over the"):
        _cost_guard("deep", {"provider": "openrouter", "model": "anthropic/claude-sonnet-5.5"})


def test_tool_less_refused(monkeypatch):
    # price under the cap so the guard reaches the tool-support check
    monkeypatch.setattr(pricing, "verdict", lambda m: (0.30, False))
    with pytest.raises(DelegationRouteError, match="no tool support"):
        _cost_guard("research", {"provider": "openrouter", "model": "perplexity/sonar-pro"})


def test_under_cap_with_tools_passes(monkeypatch):
    monkeypatch.setattr(pricing, "verdict", lambda m: (0.10, True))
    route = {"provider": "openrouter", "model": "google/gemini-2.5-flash-lite"}
    assert _cost_guard("fast", route) == route


def test_base_url_route_skips_guard():
    route = {"base_url": "http://localhost:8080/v1", "model": "qwen3-coder"}
    assert _cost_guard("local", route) == route


def test_route_task_skips_guard_for_specialist(monkeypatch):
    provider = ModelMapDelegationProvider()
    # a specialist route must dispatch without a pricing lookup even when verdict would fail it
    monkeypatch.setattr(pricing, "verdict", lambda m: (3.00, False))
    route = provider.route_task({"goal": "g", "index": 0, "option": "research"}, _config(options=OPTIONS))
    assert route["label"] == "research"


def test_route_task_refuses_over_cap(monkeypatch):
    provider = ModelMapDelegationProvider()
    monkeypatch.setattr(pricing, "verdict", lambda m: (2.00, True))
    with pytest.raises(DelegationRouteError):
        provider.route_task({"goal": "g", "index": 0, "option": "deep"}, _config(options=OPTIONS))
