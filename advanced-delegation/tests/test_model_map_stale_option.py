"""A task naming an option that a config edit removed must fall back, not raise."""
import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

from advanced_delegation_plugin.model_map import ModelMapDelegationProvider, _warned  # noqa: E402

OPTIONS = [
    {"name": "fast", "description": "Cheap.", "provider": "openrouter", "model": "m1"},
    {"name": "deep", "description": "Slow.", "provider": "anthropic", "model": "m2"},
]


def _config(**settings):
    return {"backend": "model_map", "backends": {"model_map": settings}}


def test_option_removed_after_the_enum_was_built_uses_the_default_route():
    _warned.clear()
    provider = ModelMapDelegationProvider()
    before = provider.task_parameters(_config(options=OPTIONS))
    assert "deep" in before["option"]["enum"]

    after = _config(options=[OPTIONS[0]])
    assert provider.route_task({"goal": "g", "index": 0, "option": "deep"}, after) == {}
