"""The ``model-map`` provider's contract: the per-task ``option`` parameter it declares, the guidance it
adds, and the route each task gets."""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

from delegation_model_map_plugin.provider import ModelMapDelegationProvider, _warned  # noqa: E402

MODEL_MAP = [
    {"name": "fast", "description": "Cheap and quick.", "provider": "openrouter",
     "model": "google/gemini-3-flash-preview"},
    {"name": "deep", "description": "Slow and\n careful.", "provider": "anthropic", "model": "claude-opus-5",
     "reasoning_effort": "high", "api_mode": "", "base_url": None},
    {"name": "local", "description": "Runs on this machine.", "base_url": "http://localhost:8080/v1",
     "model": "qwen3-coder", "api_key": "local-key", "api_mode": "chat_completions"},
]


@pytest.fixture
def provider():
    _warned.clear()
    return ModelMapDelegationProvider()


def test_name_matches_the_backend_value(provider):
    assert provider.name == "model-map"


def test_option_parameter_is_an_enum_of_names_with_their_descriptions(provider):
    config = {"backend": "model-map", "model_map": MODEL_MAP}
    params = provider.task_parameters(config)
    assert list(params) == ["option"]
    option = params["option"]
    assert option["type"] == "string" and option["enum"] == ["fast", "deep", "local"]
    first, *entries = option["description"].split("\n")
    assert "model" in first and "Omit" in first
    assert entries == ["fast: Cheap and quick.", "deep: Slow and careful.", "local: Runs on this machine."]
    assert json.dumps(provider.task_parameters(config)) == json.dumps(params)  # cached prefix: same bytes


def test_raw_route_fields_never_reach_the_schema(provider):
    text = json.dumps(provider.task_parameters({"model_map": MODEL_MAP}))
    for secret_or_route in ("openrouter", "claude-opus-5", "localhost", "local-key", "chat_completions"):
        assert secret_or_route not in text


@pytest.mark.parametrize("config", [{}, {"model_map": []}, {"model_map": None}, {"model_map": {"fast": {}}}])
def test_no_options_means_no_parameter_and_no_guidance(provider, config):
    assert provider.task_parameters(config) == {}
    assert provider.description(config) == ""


def test_description_tells_the_model_it_may_set_option(provider):
    text = provider.description({"model_map": MODEL_MAP})
    assert "`option`" in text and text.count(".") == 1


def test_malformed_and_duplicate_entries_are_skipped_with_one_warning_each(provider, caplog):
    config = {"model_map": [
        "fast",                                                                   # not a mapping
        {"name": "a", "provider": "openrouter", "model": "m"},                    # no description
        {"description": "d", "provider": "openrouter", "model": "m"},             # no name
        {"name": "b", "description": "d", "provider": "openrouter"},              # no model
        {"name": "c", "description": "d", "model": "m"},                          # no provider/base_url
        {"name": "ok", "description": "d", "provider": "openrouter", "model": "m"},
        {"name": "ok", "description": "again", "base_url": "http://x/v1", "model": "m2"},  # duplicate
    ]}
    with caplog.at_level(logging.WARNING):
        params = provider.task_parameters(config)
        provider.route_task({"goal": "g", "index": 0, "option": "ok"}, config)
    assert params["option"]["enum"] == ["ok"]
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 6  # one per skipped entry, not repeated by the later route_task
    assert any("needs a provider or a base_url" in w for w in warnings)
    assert any("repeats option 'ok'" in w for w in warnings)


def test_chosen_option_routes_with_only_its_non_empty_route_keys(provider):
    config = {"model_map": MODEL_MAP, "model": "ignored", "provider": "ignored"}

    def route(option):
        return provider.route_task({"goal": "g", "context": None, "index": 0, "option": option}, config)

    assert route("fast") == {"provider": "openrouter", "model": "google/gemini-3-flash-preview", "label": "fast"}
    assert route("deep") == {"provider": "anthropic", "model": "claude-opus-5", "reasoning_effort": "high",
                             "label": "deep"}
    assert route("local") == {"base_url": "http://localhost:8080/v1", "model": "qwen3-coder", "api_key": "local-key",
                              "api_mode": "chat_completions", "label": "local"}


def test_task_without_option_takes_todays_delegation_route(provider):
    config = {"backend": "model-map", "model_map": MODEL_MAP, "model": "m", "provider": "openrouter"}
    assert provider.route_task({"goal": "g", "context": None, "index": 1}, config) == config


def test_task_without_option_uses_model_map_default(provider):
    config = {"model_map": MODEL_MAP, "model_map_default": "local"}
    route = provider.route_task({"goal": "g", "index": 0}, config)
    assert route["label"] == "local" and route["model"] == "qwen3-coder"


def test_model_map_default_outside_the_map_is_ignored_with_a_warning(provider, caplog):
    config = {"model_map": MODEL_MAP, "model_map_default": "huge", "model": "m"}
    with caplog.at_level(logging.WARNING):
        assert provider.route_task({"goal": "g", "index": 0}, config) == config
    assert "'huge' is not an option" in caplog.text


def test_register_hands_core_the_provider():
    import delegation_model_map_plugin

    registered = []

    class Ctx:
        def register_delegation_provider(self, provider):
            registered.append(provider)

    delegation_model_map_plugin.register(Ctx())
    (only,) = registered
    assert isinstance(only, ModelMapDelegationProvider)
