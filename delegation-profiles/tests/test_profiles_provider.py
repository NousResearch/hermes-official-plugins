"""The ``profile_routes`` provider's contract: it reads its settings from ``delegation.backends.profile_routes``,
declares a per-task ``profile`` parameter, adds guidance, and answers each task's route."""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

from delegation_profiles_plugin.provider import ProfilesDelegationProvider, _warned  # noqa: E402

PROFILES = [
    {"name": "coder", "description": "Writes and refactors code; runs tests."},
    {"name": "researcher", "description": "Web research and\n  long-form summaries."},
]


def _config(**settings):
    """A ``delegation`` section selecting this backend, with ``settings`` under ``backends.profile_routes``."""
    return {"backend": "profile_routes", "backends": {"profile_routes": settings}}


@pytest.fixture
def provider():
    _warned.clear()
    return ProfilesDelegationProvider()


def test_name_matches_the_backend_value_and_the_settings_key(provider):
    assert provider.name == "profile_routes"
    assert provider.settings(_config(profiles=PROFILES)) == {"profiles": PROFILES}


def test_profile_parameter_is_an_enum_of_the_listed_profiles_with_their_descriptions(provider):
    config = _config(profiles=PROFILES)
    params = provider.task_parameters(config)
    assert list(params) == ["profile"]
    profile = params["profile"]
    assert profile["type"] == "string"
    assert profile["enum"] == ["coder", "researcher"]
    first, *entries = profile["description"].split("\n")
    assert "Hermes profiles" in first and "model" in first and "credentials" in first
    assert entries == ["coder: Writes and refactors code; runs tests.",
                       "researcher: Web research and long-form summaries."]
    # Part of the cached prompt prefix: same config, same bytes.
    assert json.dumps(provider.task_parameters(config)) == json.dumps(params)


@pytest.mark.parametrize("config", [
    {}, {"backends": None}, {"backends": {"profile_routes": None}}, {"backends": {"other": {"profiles": PROFILES}}},
    _config(), _config(profiles=[]), _config(profiles=None), _config(profiles="coder"),
])
def test_no_profiles_means_no_parameter_and_no_guidance(provider, config):
    assert provider.task_parameters(config) == {}
    assert provider.description(config) == ""


def test_settings_at_the_old_location_beside_core_keys_are_not_read(provider):
    config = {"backend": "profile_routes", "profiles": PROFILES, "profiles_default": "coder", "default": "coder",
              "model": "m"}
    assert provider.task_parameters(config) == {}
    assert provider.description(config) == ""
    assert provider.route_task({"goal": "g", "index": 0}, config) == config


def test_description_tells_the_model_it_may_set_profile(provider):
    text = provider.description(_config(profiles=PROFILES))
    assert "`profile`" in text and text.count(".") == 1


def test_malformed_and_duplicate_entries_are_skipped_with_one_warning_each_naming_the_new_path(provider, caplog):
    config = _config(profiles=[{"description": "no name"}, "coder", {"name": "  "}, {"name": "coder"},
                               {"name": "coder", "description": "again"}, {"name": "writer"}])
    with caplog.at_level(logging.WARNING):
        params = provider.task_parameters(config)
        provider.task_parameters(config)
    assert params["profile"]["enum"] == ["coder", "writer"]
    assert params["profile"]["description"].split("\n")[1:] == ["coder", "writer"]
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 4  # [0], [1], [2] nameless, [4] duplicate; not repeated on the second build
    assert "delegation.backends.profile_routes.profiles[0] has no name; skipped." in warnings
    assert "delegation.backends.profile_routes.profiles[4] repeats profile 'coder'; skipped." in warnings


def test_profiles_that_are_not_a_list_are_ignored_with_a_warning_naming_the_new_path(provider, caplog):
    with caplog.at_level(logging.WARNING):
        assert provider.task_parameters(_config(profiles="coder")) == {}
    assert "delegation.backends.profile_routes.profiles must be a list" in caplog.text


def test_chosen_profile_routes_the_task_to_that_profile(provider):
    route = provider.route_task({"goal": "fix the bug", "context": None, "index": 0, "profile": "coder"},
                                _config(profiles=PROFILES))
    assert route == {"profile": "coder", "label": "coder"}


def test_task_without_profile_takes_todays_delegation_route(provider):
    config = {**_config(profiles=PROFILES), "model": "m", "provider": "openrouter"}
    assert provider.route_task({"goal": "g", "context": None, "index": 1}, config) == config


def test_task_without_profile_uses_the_configured_default(provider):
    route = provider.route_task({"goal": "g", "index": 0}, _config(profiles=PROFILES, default="researcher"))
    assert route == {"profile": "researcher", "label": "researcher"}


def test_default_outside_the_list_is_ignored_with_a_warning_naming_the_new_path(provider, caplog):
    config = {**_config(profiles=PROFILES, default="admin"), "model": "m"}
    with caplog.at_level(logging.WARNING):
        assert provider.route_task({"goal": "g", "index": 0}, config) == config
    assert ("delegation.backends.profile_routes.default 'admin' is not in "
            "delegation.backends.profile_routes.profiles") in caplog.text


def test_register_hands_core_the_provider():
    import delegation_profiles_plugin

    registered = []

    class Ctx:
        def register_delegation_provider(self, provider):
            registered.append(provider)

    delegation_profiles_plugin.register(Ctx())
    (only,) = registered
    assert isinstance(only, ProfilesDelegationProvider)
