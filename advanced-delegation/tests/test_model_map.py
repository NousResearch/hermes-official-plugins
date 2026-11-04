"""The ``model_map`` provider's contract: it reads its settings from ``delegation.backends.model_map``, declares
a per-task ``option`` parameter, adds guidance, and answers each task's route."""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

from advanced_delegation_plugin.model_map import ModelMapDelegationProvider, _warned  # noqa: E402

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
    assert provider.route_task({"goal": "g", "index": 0}, config) == {}


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


def test_an_options_fallback_providers_are_routed_to_core(provider):
    chain = [{"provider": "openrouter", "model": "google/gemini-3-flash-preview"}]
    config = _config(options=[{"name": "deep", "description": "d", "provider": "anthropic", "model": "claude-opus-5",
                               "fallback_providers": chain},
                              {"name": "bad", "description": "d", "provider": "anthropic", "model": "m",
                               "fallback_providers": "openrouter"}])
    assert provider.route_task({"goal": "g", "index": 0, "option": "deep"}, config)["fallback_providers"] == chain
    assert "fallback_providers" not in provider.route_task({"goal": "g", "index": 0, "option": "bad"}, config)


def test_an_options_service_tier_is_routed_to_core(provider):
    config = _config(options=[{"name": "quick", "description": "d", "provider": "openai-codex", "model": "gpt-5.5",
                               "service_tier": " ultrafast "}])
    route = provider.route_task({"goal": "g", "index": 0, "option": "quick"}, config)
    assert route["service_tier"] == "ultrafast"


def test_task_without_option_runs_on_the_parent_route_not_delegation_model(provider):
    # In this mode delegation.model / delegation.provider are inactive: an empty route inherits the parent.
    config = {**_config(options=OPTIONS), "model": "m", "provider": "openrouter", "base_url": "http://x/v1"}
    assert provider.route_task({"goal": "g", "context": None, "index": 1}, config) == {}


def test_task_without_option_uses_the_configured_default(provider):
    route = provider.route_task({"goal": "g", "index": 0}, _config(options=OPTIONS, default="local"))
    assert route["label"] == "local" and route["model"] == "qwen3-coder"


def test_default_outside_the_options_is_ignored_with_a_warning_naming_the_new_path(provider, caplog):
    config = {**_config(options=OPTIONS, default="huge"), "model": "m"}
    with caplog.at_level(logging.WARNING):
        assert provider.route_task({"goal": "g", "index": 0}, config) == {}
    assert ("delegation.backends.model_map.default 'huge' is not an option in "
            "delegation.backends.model_map.options") in caplog.text


def test_pickers_show_the_user_facing_name_and_one_line_summary(provider):
    # /delegation and core's backend list read these; the model never sees them.
    assert provider.display_name == "Model routes"
    assert provider.summary and "\n" not in provider.summary



# --- daybreak ---------------------------------------------------------------------------------

DAYBREAK = {"name": "daybreak", "description": "Sol, reduced guardrails.", "provider": "openai-codex",
            "model": "gpt-6-sol", "service_tier": "fast", "daybreak": True}
SOL = {"name": "sol", "description": "Sol.", "provider": "openai-codex", "model": "gpt-6-sol"}
CYBER = {"extra_body": {"access_programs": {"cyber": "daybreak_blue"}}}


@pytest.fixture
def daybreak_env(monkeypatch):
    """An owner's Desktop session whose Codex catalog lists Daybreak for every model; tests flip either half."""
    offered = {"value": True}
    calls = []

    def fake_offered(model, *, provider=None, base_url=None, access_token=None):
        calls.append((model, provider, base_url))
        return offered["value"]

    monkeypatch.setattr("agent.codex_service_tiers.daybreak_offered", fake_offered)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
    return {"offered": offered, "calls": calls, "monkeypatch": monkeypatch}


def _pick(provider, config, option):
    return provider.route_task({"goal": "g", "index": 0, "option": option}, config)


def test_a_daybreak_route_runs_the_base_model_with_the_daybreak_access_program(provider, daybreak_env):
    config = _config(options=[SOL, DAYBREAK])
    assert _pick(provider, config, "daybreak") == {
        "provider": "openai-codex", "model": "gpt-6-sol", "service_tier": "fast", "request_overrides": CYBER,
        "label": "daybreak"}
    assert daybreak_env["calls"] == [("gpt-6-sol", "openai-codex", None)]
    # The same base model without daybreak is a plain route: no catalog lookup, no access program.
    assert _pick(provider, config, "sol") == {"provider": "openai-codex", "model": "gpt-6-sol", "label": "sol"}
    assert len(daybreak_env["calls"]) == 1


def test_the_backend_declares_daybreak_for_settings_editors(provider):
    assert provider.route_keys == ("daybreak",)


def test_daybreak_never_reaches_the_schema(provider):
    text = json.dumps(provider.task_parameters(_config(options=[SOL, DAYBREAK])))
    assert "daybreak_blue" not in text and "access_programs" not in text and "gpt-6-sol" not in text


@pytest.mark.parametrize("source", ["cli", "tui", "desktop"])
def test_daybreak_runs_from_the_owners_own_surfaces(provider, daybreak_env, source):
    daybreak_env["monkeypatch"].setenv("HERMES_SESSION_SOURCE", source)
    assert _pick(provider, _config(options=[DAYBREAK]), "daybreak")["request_overrides"] == CYBER


@pytest.mark.parametrize("source", ["telegram", "discord", "cron", "a2a"])
def test_daybreak_is_refused_outside_the_owners_own_surfaces(provider, daybreak_env, source):
    from agent.delegation_provider import DelegationRouteError
    daybreak_env["monkeypatch"].setenv("HERMES_SESSION_SOURCE", source)
    with pytest.raises(DelegationRouteError, match=f"owner's own.*{source}"):
        _pick(provider, _config(options=[DAYBREAK]), "daybreak")


# (session source, the gateway's verdict on the bound agent, allowed on a core that reports verdicts)
GATEWAY_TURNS = {
    "owner's gateway chat": ("local", True, True),
    "hosted room or A2A peer": ("local", False, False),
    "unjudged local agent": ("local", None, False),
    "/bg task from a non-owner chat": ("cli", False, False),
    "/bg task from the owner's chat": ("cli", True, True),
    "classic CLI or TUI backend": ("cli", None, True),
}


@pytest.mark.parametrize("case", list(GATEWAY_TURNS))
def test_the_gateways_verdict_decides_and_a_refusal_beats_the_session_source(provider, daybreak_env, case):
    """A gateway-hosted chat runs on the ``local`` transport and a ``/bg`` task binds no session (it reads as
    ``cli``), so the gateway's verdict on the turn's agent decides; only an unjudged agent falls back to the source."""
    from types import SimpleNamespace

    import agent.session_source
    from agent.delegation_provider import DelegationRouteError
    from agent.subagent_lifecycle import bind_subagent_parent
    platform, verdict, allowed = GATEWAY_TURNS[case]
    if not hasattr(agent.session_source, "turn_daybreak_surface"):  # older cores: the session source alone
        allowed = platform in agent.session_source.DAYBREAK_SOURCES
    daybreak_env["monkeypatch"].delenv("HERMES_SESSION_SOURCE")
    daybreak_env["monkeypatch"].setenv("HERMES_SESSION_PLATFORM", platform)
    parent = SimpleNamespace(platform=platform)
    if verdict is not None:
        parent.daybreak_surface = verdict
    with bind_subagent_parent(parent):
        if allowed:
            assert _pick(provider, _config(options=[DAYBREAK]), "daybreak")["request_overrides"] == CYBER
        else:
            with pytest.raises(DelegationRouteError, match="owner's own"):
                _pick(provider, _config(options=[DAYBREAK]), "daybreak")


def test_a_cron_job_is_refused_though_it_binds_no_platform(provider, daybreak_env):
    """A cron job binds an empty platform, which the session source reads as ``cli``; the README says cron is refused."""
    from agent.delegation_provider import DelegationRouteError
    daybreak_env["monkeypatch"].delenv("HERMES_SESSION_SOURCE")
    daybreak_env["monkeypatch"].setenv("HERMES_CRON_SESSION", "1")
    with pytest.raises(DelegationRouteError, match="this session is 'cron'"):
        _pick(provider, _config(options=[DAYBREAK]), "daybreak")


def test_daybreak_is_refused_when_the_catalog_does_not_list_it(provider, daybreak_env):
    from agent.delegation_provider import DelegationRouteError
    daybreak_env["offered"]["value"] = False
    with pytest.raises(DelegationRouteError, match="catalog does not list Daybreak for 'gpt-6-sol'"):
        _pick(provider, _config(options=[DAYBREAK]), "daybreak")


def test_a_default_daybreak_route_is_gated_too(provider, daybreak_env):
    from agent.delegation_provider import DelegationRouteError
    daybreak_env["monkeypatch"].setenv("HERMES_SESSION_SOURCE", "telegram")
    with pytest.raises(DelegationRouteError):
        provider.route_task({"goal": "g", "index": 0}, _config(options=[DAYBREAK], default="daybreak"))


@pytest.mark.parametrize("change, problem", [
    ({"provider": "openrouter"}, "needs provider: openai-codex"),
    ({"provider": None, "base_url": "https://chatgpt.com/backend-api/codex"}, "needs provider: openai-codex"),
    ({"daybreak": "yes"}, "use true or false"),
])
def test_a_misconfigured_daybreak_route_is_skipped_with_a_warning(provider, caplog, change, problem):
    with caplog.at_level(logging.WARNING):
        params = provider.task_parameters(_config(options=[SOL, {**DAYBREAK, **change}]))
    assert params["option"]["enum"] == ["sol"]
    assert problem in caplog.text


def test_daybreak_false_is_a_plain_route(provider, daybreak_env):
    route = _pick(provider, _config(options=[{**DAYBREAK, "daybreak": False}]), "daybreak")
    assert "request_overrides" not in route and daybreak_env["calls"] == []

# --- profile routes in model_map -----------------------------------------------------------

def test_profile_route_returns_profile_dict(provider):
    config = _config(options=[
        {"name": "code", "description": "Frontier coding.", "profile": "openai"},
    ])
    route = provider.route_task({"goal": "g", "index": 0, "option": "code"}, config)
    assert route == {"profile": "openai", "label": "code"}


def test_entry_with_both_profile_and_model_is_skipped(provider, caplog):
    config = _config(options=[
        {"name": "bad", "description": "Ambiguous.", "profile": "openai", "provider": "custom", "model": "gpt-6-sol"},
    ])
    with caplog.at_level(logging.WARNING):
        params = provider.task_parameters(config)
    assert params == {}
    assert "both" in caplog.text.lower()


def test_mixed_config_routes_model_and_profile_entries(provider):
    config = _config(options=[
        {"name": "cheap", "description": "Lookups.", "provider": "openrouter", "model": "google/gemini-3-flash-preview"},
        {"name": "code", "description": "Frontier coding.", "profile": "openai"},
    ])
    params = provider.task_parameters(config)
    assert params["option"]["enum"] == ["cheap", "code"]
    cheap_route = provider.route_task({"goal": "g", "index": 0, "option": "cheap"}, config)
    assert cheap_route["model"] == "google/gemini-3-flash-preview" and "profile" not in cheap_route
    code_route = provider.route_task({"goal": "g", "index": 0, "option": "code"}, config)
    assert code_route == {"profile": "openai", "label": "code"}


def test_profile_route_default_works(provider):
    config = _config(options=[
        {"name": "code", "description": "Frontier coding.", "profile": "openai"},
    ], default="code")
    route = provider.route_task({"goal": "g", "index": 0}, config)
    assert route == {"profile": "openai", "label": "code"}
