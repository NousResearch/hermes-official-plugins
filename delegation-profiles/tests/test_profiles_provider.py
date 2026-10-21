"""The ``profiles`` provider's contract: the per-task ``profile`` parameter it declares, the guidance it
adds, and the route each task gets."""

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


@pytest.fixture
def provider():
    _warned.clear()
    return ProfilesDelegationProvider()


def test_name_matches_the_backend_value(provider):
    assert provider.name == "profiles"


def test_profile_parameter_is_an_enum_of_the_listed_profiles_with_their_descriptions(provider):
    config = {"backend": "profiles", "profiles": PROFILES}
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


@pytest.mark.parametrize("config", [{}, {"profiles": []}, {"profiles": None}, {"profiles": "coder"}])
def test_no_profiles_means_no_parameter_and_no_guidance(provider, config):
    assert provider.task_parameters(config) == {}
    assert provider.description(config) == ""


def test_description_tells_the_model_it_may_set_profile(provider):
    text = provider.description({"profiles": PROFILES})
    assert "`profile`" in text and text.count(".") == 1


def test_malformed_and_duplicate_entries_are_skipped_with_one_warning_each(provider, caplog):
    config = {"profiles": [{"description": "no name"}, "coder", {"name": "  "}, {"name": "coder"},
                           {"name": "coder", "description": "again"}, {"name": "writer"}]}
    with caplog.at_level(logging.WARNING):
        params = provider.task_parameters(config)
        provider.task_parameters(config)
    assert params["profile"]["enum"] == ["coder", "writer"]
    assert params["profile"]["description"].split("\n")[1:] == ["coder", "writer"]
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 4  # [0], [1], [2] nameless, [4] duplicate; not repeated on the second build
    assert any("repeats profile 'coder'" in w for w in warnings)


def test_chosen_profile_routes_the_task_to_that_profile(provider):
    route = provider.route_task({"goal": "fix the bug", "context": None, "index": 0, "profile": "coder"},
                                {"profiles": PROFILES})
    assert route == {"profile": "coder", "label": "coder"}


def test_task_without_profile_takes_todays_delegation_route(provider):
    config = {"backend": "profiles", "profiles": PROFILES, "model": "m", "provider": "openrouter"}
    assert provider.route_task({"goal": "g", "context": None, "index": 1}, config) == config


def test_task_without_profile_uses_profiles_default(provider):
    config = {"profiles": PROFILES, "profiles_default": "researcher"}
    assert provider.route_task({"goal": "g", "index": 0}, config) == {"profile": "researcher", "label": "researcher"}


def test_profiles_default_outside_the_list_is_ignored_with_a_warning(provider, caplog):
    config = {"profiles": PROFILES, "profiles_default": "admin", "model": "m"}
    with caplog.at_level(logging.WARNING):
        assert provider.route_task({"goal": "g", "index": 0}, config) == config
    assert "'admin' is not in delegation.profiles" in caplog.text


def test_register_hands_core_the_provider():
    import delegation_profiles_plugin

    registered = []

    class Ctx:
        def register_delegation_provider(self, provider):
            registered.append(provider)

    delegation_profiles_plugin.register(Ctx())
    (only,) = registered
    assert isinstance(only, ProfilesDelegationProvider)
