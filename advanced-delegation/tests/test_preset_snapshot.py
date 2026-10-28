"""Only runtime route state crosses the durable session-pin boundary."""
from copy import deepcopy

from advanced_delegation_plugin.model_map import ModelMapDelegationProvider


def test_snapshot_excludes_saved_presets_but_retains_complete_active_routes():
    route = {"name": "fast", "description": "Quick", "provider": "openrouter", "model": "active",
             "fallback_providers": [{"provider": "openrouter", "model": "backup"}]}
    config = {"backends": {"model_map": {"options": [route], "default": "fast",
        "active_preset": "work", "presets": [{"name": "unused", "options": [{"api_key": "unused-secret"}]}]}}}
    original = deepcopy(config)
    snapshot = ModelMapDelegationProvider().snapshot_settings(config)
    assert snapshot == {"options": [route], "default": "fast"}
    snapshot["options"][0]["fallback_providers"][0]["model"] = "mutated"
    assert config == original
