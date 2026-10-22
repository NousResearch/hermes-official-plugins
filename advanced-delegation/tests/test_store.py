"""``store``: what ``hermes delegation config`` and ``/delegation`` read and write.

Writes go through core's ``atomic_config_write`` (comment-preserving, refuses to drop settings), so
everything outside the touched keys survives byte-for-byte in meaning.
"""

from __future__ import annotations

import pytest

import hermes_yaml as yaml

from advanced_delegation_plugin import store


@pytest.fixture
def home(tmp_path):
    return tmp_path / "os-home" / ".hermes"  # HERMES_HOME from the conftest fixture


def _write(home, text):
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _read(home):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def test_set_mode_writes_only_the_backend_and_keeps_comments_and_other_keys(home):
    _write(home, "# my config\nmodel:\n  default: m  # main\n"
                 "delegation:\n  model: sub-model\n  max_concurrent_children: 2\n")
    store.set_mode("model_map")
    text = (home / "config.yaml").read_text(encoding="utf-8")
    assert "# my config" in text and "# main" in text
    data = _read(home)
    assert data["delegation"] == {"model": "sub-model", "max_concurrent_children": 2, "backend": "model_map"}
    assert data["model"] == {"default": "m"}


def test_set_mode_creates_the_config_when_there_is_none(home):
    store.set_mode("profile_routes")
    assert _read(home)["delegation"]["backend"] == "profile_routes"


def test_unknown_mode_is_refused_and_nothing_is_written(home):
    with pytest.raises(ValueError, match="model_map"):
        store.set_mode("models")
    assert not (home / "config.yaml").exists()


def test_save_routes_writes_the_options_and_default_under_the_backend_block(home):
    _write(home, "delegation:\n  backend: model_map\n  backends:\n"
                 "    profile_routes:\n      profiles: [{name: coder}]\n")
    routes = [{"name": "fast", "description": "Quick.", "provider": "openrouter", "model": "m1"},
              {"name": "deep", "description": "Careful.", "provider": "anthropic", "model": "m2",
               "reasoning_effort": "high"}]
    store.save_routes(routes, default="deep")
    block = _read(home)["delegation"]["backends"]
    assert block["model_map"] == {"options": routes, "default": "deep"}
    assert block["profile_routes"] == {"profiles": [{"name": "coder"}]}


def test_save_routes_without_a_default_clears_a_stale_one(home):
    store.save_routes([{"name": "fast", "description": "d", "provider": "openrouter", "model": "m"}], default="old")
    store.save_routes([{"name": "fast", "description": "d", "provider": "openrouter", "model": "m"}], default="")
    assert not _read(home)["delegation"]["backends"]["model_map"]["default"]


def test_save_profiles_writes_names_descriptions_and_default(home):
    store.save_profiles([{"name": "coder", "description": "Writes code."}, {"name": "researcher"}], default="coder")
    assert _read(home)["delegation"]["backends"]["profile_routes"] == {
        "profiles": [{"name": "coder", "description": "Writes code."}, {"name": "researcher"}], "default": "coder"}


def test_reads_follow_the_saved_config(home):
    _write(home, "delegation:\n  backend: Model_Map\n  backends:\n    model_map:\n      options: [{name: a}]\n"
                 "      default: a\n    profile_routes:\n      profiles: [{name: coder, description: d}]\n")
    section = store.delegation_section()
    assert store.current_mode(section) == "model_map"
    assert store.routes(section) == [{"name": "a"}]
    assert store.route_default(section) == "a"
    assert store.listed_profiles(section) == [{"name": "coder", "description": "d"}]
    assert store.current_mode({}) == "default"


VALID = {"name": "fast", "description": "Quick lookups.", "provider": "openrouter", "model": "some/model"}


@pytest.mark.parametrize(("change", "problem"), [
    ({"name": ""}, "name"),
    ({"name": "two words"}, "name"),
    ({"description": " "}, "description"),
    ({"model": ""}, "model"),
    ({"provider": "", "base_url": ""}, "provider"),
    ({"provider": "no-such-provider-xyz"}, "no-such-provider-xyz"),
    ({"reasoning_effort": "hgih"}, "hgih"),
    ({"service_tier": "turbo"}, "turbo"),
])
def test_validate_route_names_each_problem(change, problem):
    problems = store.validate_route({**VALID, **change}, others=[])
    assert problems and any(problem in p for p in problems)


def test_validate_route_accepts_a_complete_route_and_an_endpoint_route():
    assert store.validate_route(VALID, others=[]) == []
    assert store.validate_route({"name": "local", "description": "d", "base_url": "http://localhost:8080/v1",
                                 "model": "qwen", "reasoning_effort": "none"}, others=[]) == []


def test_validate_route_refuses_a_repeated_name():
    assert any("fast" in p for p in store.validate_route(VALID, others=[{"name": "fast"}]))


def test_validate_route_refuses_a_speed_tier_the_route_does_not_offer():
    # A plain OpenRouter model has no priority tier; core would refuse every spawn on this route.
    problems = store.validate_route({**VALID, "service_tier": "fast"}, others=[])
    assert any("fast" in p and "offer" in p for p in problems)


def test_describe_shows_the_mode_and_its_routes():
    text = store.describe({"backend": "model_map", "model": "sub", "backends": {"model_map": {
        "options": [VALID, {"name": "deep", "description": "Careful.", "provider": "anthropic", "model": "opus",
                            "reasoning_effort": "high"}], "default": "deep"}}})
    assert "Model routes" in text and "model_map" in text
    assert "fast" in text and "Quick lookups." in text and "openrouter" in text and "some/model" in text
    assert "deep" in text and "high" in text and "default" in text
    assert "delegation.model" in text and "inactive" in text


def test_describe_default_mode_names_the_delegation_model():
    text = store.describe({"model": "sub-model", "provider": "openrouter"})
    assert "Default" in text and "sub-model" in text and "openrouter" in text


def test_describe_profile_routes_lists_profiles():
    text = store.describe({"backend": "profile_routes", "backends": {"profile_routes": {
        "profiles": [{"name": "coder", "description": "Writes code."}], "default": "coder"}}})
    assert "Profile routes" in text and "coder" in text and "Writes code." in text


def test_a_package_manager_managed_install_refuses_the_write(home, monkeypatch):
    store.delegation_section()  # create the home layout first: a managed install never creates it
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "nix")
    with pytest.raises(RuntimeError, match="(?i)nix"):
        store.set_mode("model_map")
    assert not (home / "config.yaml").exists()
