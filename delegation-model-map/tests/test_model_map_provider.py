"""The ``model_map`` provider's contract: it reads its settings from ``delegation.backends.model_map``, declares
a per-task ``option`` parameter, adds guidance, and answers each task's route."""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

from delegation_model_map_plugin.provider import ModelMapDelegationProvider, _warned  # noqa: E402

OPTIONS = [
    {"name": "fast", "description": "Cheap and quick.", "provider": "openrouter",
     "model": "google/gemini-3-flash-preview"},
    {"name": "deep", "description": "Slow and\n careful.", "provider": "anthropic", "model": "claude-opus-5",
     "reasoning_effort": "high", "service_tier": "fast", "api_mode": "", "base_url": None},
    {"name": "local", "description": "Runs on this machine.", "base_url": "http://localhost:8080/v1",
     "model": "qwen3-coder", "api_key": "local-key", "api_mode": "chat_completions"},
]


def _config(**settings):
    """A ``delegation`` section selecting this backend, with ``settings`` under ``backends.model_map``."""
    return {"backend": "model_map", "backends": {"model_map": settings}}


@pytest.fixture
def provider():
    _warned.clear()
    return ModelMapDelegationProvider()


def test_name_matches_the_backend_value_and_the_settings_key(provider):
    assert provider.name == "model_map"
    assert provider.settings(_config(options=OPTIONS)) == {"options": OPTIONS}


def test_option_parameter_is_an_enum_of_names_with_their_descriptions(provider):
    config = _config(options=OPTIONS)
    params = provider.task_parameters(config)
    assert list(params) == ["option"]
    option = params["option"]
    assert option["type"] == "string" and option["enum"] == ["fast", "deep", "local"]
    first, *entries = option["description"].split("\n")
    assert "model" in first and "Omit" in first
    assert entries == ["fast: Cheap and quick.", "deep: Slow and careful.", "local: Runs on this machine."]
    assert json.dumps(provider.task_parameters(config)) == json.dumps(params)  # cached prefix: same bytes


def test_raw_route_fields_never_reach_the_schema(provider):
    text = json.dumps(provider.task_parameters(_config(options=OPTIONS)))
    for secret_or_route in ("openrouter", "claude-opus-5", "localhost", "local-key", "chat_completions"):
        assert secret_or_route not in text


@pytest.mark.parametrize("config", [
    {}, {"backends": None}, {"backends": {"model_map": None}}, {"backends": {"other": {"options": OPTIONS}}},
    _config(), _config(options=[]), _config(options=None), _config(options={"fast": {}}),
])
def test_no_options_means_no_parameter_and_no_guidance(provider, config):
    assert provider.task_parameters(config) == {}
    assert provider.description(config) == ""


def test_settings_at_the_old_location_beside_core_keys_are_not_read(provider):
    config = {"backend": "model_map", "model_map": OPTIONS, "model_map_default": "fast", "options": OPTIONS,
              "default": "fast", "model": "m"}
    assert provider.task_parameters(config) == {}
    assert provider.description(config) == ""
    assert provider.route_task({"goal": "g", "index": 0}, config) == config


def test_description_tells_the_model_it_may_set_option(provider):
    text = provider.description(_config(options=OPTIONS))
    assert "`option`" in text and text.count(".") == 1


def test_malformed_and_duplicate_options_are_skipped_with_one_warning_each_naming_the_new_path(provider, caplog):
    config = _config(options=[
        "fast",                                                                   # not a mapping
        {"name": "a", "provider": "openrouter", "model": "m"},                    # no description
        {"description": "d", "provider": "openrouter", "model": "m"},             # no name
        {"name": "b", "description": "d", "provider": "openrouter"},              # no model
        {"name": "c", "description": "d", "model": "m"},                          # no provider/base_url
        {"name": "ok", "description": "d", "provider": "openrouter", "model": "m"},
        {"name": "ok", "description": "again", "base_url": "http://x/v1", "model": "m2"},  # duplicate
    ])
    with caplog.at_level(logging.WARNING):
        params = provider.task_parameters(config)
        provider.route_task({"goal": "g", "index": 0, "option": "ok"}, config)
    assert params["option"]["enum"] == ["ok"]
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 6  # one per skipped entry, not repeated by the later route_task
    assert "delegation.backends.model_map.options[4] needs a provider or a base_url; skipped." in warnings
    assert "delegation.backends.model_map.options[6] repeats option 'ok'; skipped." in warnings


def test_options_that_are_not_a_list_are_ignored_with_a_warning_naming_the_new_path(provider, caplog):
    with caplog.at_level(logging.WARNING):
        assert provider.task_parameters(_config(options={"fast": {}})) == {}
    assert "delegation.backends.model_map.options must be a list" in caplog.text


def test_chosen_option_routes_with_only_its_non_empty_route_keys(provider):
    config = {**_config(options=OPTIONS), "model": "ignored", "provider": "ignored"}

    def route(option):
        return provider.route_task({"goal": "g", "context": None, "index": 0, "option": option}, config)

    assert route("fast") == {"provider": "openrouter", "model": "google/gemini-3-flash-preview", "label": "fast"}
    assert route("deep") == {"provider": "anthropic", "model": "claude-opus-5", "reasoning_effort": "high",
                             "service_tier": "fast", "label": "deep"}
    assert route("local") == {"base_url": "http://localhost:8080/v1", "model": "qwen3-coder", "api_key": "local-key",
                              "api_mode": "chat_completions", "label": "local"}


def test_an_options_service_tier_is_routed_to_core(provider):
    config = _config(options=[{"name": "quick", "description": "d", "provider": "openai-codex", "model": "gpt-5.5",
                               "service_tier": " ultrafast "}])
    route = provider.route_task({"goal": "g", "index": 0, "option": "quick"}, config)
    assert route["service_tier"] == "ultrafast"


def test_task_without_option_takes_todays_delegation_route(provider):
    config = {**_config(options=OPTIONS), "model": "m", "provider": "openrouter"}
    assert provider.route_task({"goal": "g", "context": None, "index": 1}, config) == config


def test_task_without_option_uses_the_configured_default(provider):
    route = provider.route_task({"goal": "g", "index": 0}, _config(options=OPTIONS, default="local"))
    assert route["label"] == "local" and route["model"] == "qwen3-coder"


def test_default_outside_the_options_is_ignored_with_a_warning_naming_the_new_path(provider, caplog):
    config = {**_config(options=OPTIONS, default="huge"), "model": "m"}
    with caplog.at_level(logging.WARNING):
        assert provider.route_task({"goal": "g", "index": 0}, config) == config
    assert ("delegation.backends.model_map.default 'huge' is not an option in "
            "delegation.backends.model_map.options") in caplog.text


def test_register_hands_core_the_provider():
    import delegation_model_map_plugin

    registered = []

    class Ctx:
        def register_delegation_provider(self, provider):
            registered.append(provider)

    delegation_model_map_plugin.register(Ctx())
    (only,) = registered
    assert isinstance(only, ModelMapDelegationProvider)
