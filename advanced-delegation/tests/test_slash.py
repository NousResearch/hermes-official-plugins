"""``/delegation``: show the current mode and switch it, session-scoped by default, ``--global`` saves.

The handler is called the way core's ``invoke_plugin_command`` calls it on every surface (raw args
plus ``session_key`` / ``platform``). Core's per-session API is feature-detected on
``agent.delegation_registry``; here it is installed on that module for the duration of a test.
"""

from __future__ import annotations

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

import hermes_yaml as yaml  # noqa: E402

from advanced_delegation_plugin import slash  # noqa: E402

ROUTES = [{"name": "fast", "description": "Quick.", "provider": "openrouter", "model": "m1"},
          {"name": "deep", "description": "Careful.", "provider": "anthropic", "model": "m2"}]


@pytest.fixture
def home(tmp_path):
    return tmp_path / "os-home" / ".hermes"


def _write(home, delegation):
    (home / "config.yaml").write_text(yaml.safe_dump({"delegation": delegation}), encoding="utf-8")


def _saved(home):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))["delegation"]


def _run(args="", **context):
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    context = {"source": None, "session_id": "s1", "session_key": "key-1", "platform": "cli", **context}
    return invoke_plugin_command(slash.handle, args, **context)


@pytest.fixture
def session_api(monkeypatch):
    """Core's per-session backend API, recorded."""
    import agent.delegation_registry as registry
    sessions: dict[str, str | None] = {}
    monkeypatch.setattr(registry, "get_session_backend", lambda key: sessions.get(key), raising=False)
    monkeypatch.setattr(registry, "set_session_backend",
                        lambda key, name: sessions.__setitem__(key, name), raising=False)
    return sessions


@pytest.fixture
def no_session_api(monkeypatch):
    import agent.delegation_registry as registry
    monkeypatch.delattr(registry, "get_session_backend", raising=False)
    monkeypatch.delattr(registry, "set_session_backend", raising=False)


def test_bare_command_lists_the_modes_marks_the_current_one_and_says_how_to_switch(home, no_session_api):
    _write(home, {"backend": "model_map", "backends": {"model_map": {"options": ROUTES, "default": "fast"},
                                                        "profile_routes": {"profiles": [{"name": "coder"}]}}})
    text = str(_run(""))
    assert "1. Default" in text and "2. Model routes" in text and "3. Profile routes" in text
    assert "Model routes" in text.split("\n")[0]                     # the current mode leads
    assert "fast (default), deep" in text and "coder" in text        # what each mode would route to
    assert "/delegation" in text and "--global" in text


def test_global_switch_saves_the_mode(home, no_session_api):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("model_map --global"))
    assert _saved(home)["backend"] == "model_map"
    assert "Model routes" in reply and "saved" in reply.lower()


@pytest.mark.parametrize("word", ["2", "model_map", "models", "Model routes", "model-routes"])
def test_mode_words_and_numbers_are_accepted(home, no_session_api, word):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    _run(f"{word} --global")
    assert _saved(home)["backend"] == "model_map"


def test_switch_is_session_scoped_by_default_and_leaves_the_config_alone(home, session_api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("model_map", session_key="chat-7"))
    assert session_api == {"chat-7": "model_map"}
    assert _saved(home)["backend"] == "default"
    assert "this session" in reply and "--global" in reply
    assert "Model routes" in str(_run("", session_key="chat-7")).split("\n")[0]  # the session's mode leads


def test_global_also_switches_the_current_session(home, session_api):
    _write(home, {"backends": {"profile_routes": {"profiles": [{"name": "coder"}]}}})
    _run("profile_routes --global", session_key="chat-7")
    assert session_api == {"chat-7": "profile_routes"} and _saved(home)["backend"] == "profile_routes"


def test_without_core_session_support_a_plain_switch_points_to_global(home, no_session_api):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("model_map"))
    assert "--global" in reply
    assert "backend" not in _saved(home)


@pytest.mark.parametrize(("word", "mode_name"), [("model_map", "Model routes"), ("profiles", "Profile routes")])
def test_switching_to_a_mode_with_nothing_configured_says_how_to_set_it_up(home, session_api, word, mode_name):
    _write(home, {"backend": "default"})
    reply = str(_run(f"{word} --global"))
    assert mode_name in reply and "hermes delegation config" in reply
    assert _saved(home)["backend"] == "default" and session_api == {}


def test_switching_back_to_default_always_works(home, session_api):
    _write(home, {"backend": "model_map", "model": "sub-model"})
    reply = str(_run("default --global"))
    assert _saved(home)["backend"] == "default"
    assert "delegation.model" in reply


def test_an_unknown_word_shows_the_usage(home, no_session_api):
    _write(home, {"backend": "default"})
    reply = str(_run("turbo"))
    assert "turbo" in reply and "/delegation" in reply and "default" in reply
    assert _saved(home) == {"backend": "default"}


def test_a_configured_backend_that_is_not_registered_is_flagged_as_the_thing_to_fix(home, no_session_api):
    _write(home, {"backend": "something_uninstalled"})
    text = str(_run(""))
    assert "something_uninstalled" in text and "not available" in text


def test_a_refused_write_is_reported_not_raised(home, no_session_api, monkeypatch):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    slash.store.delegation_section()  # create the home layout first: a managed install never creates it
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "nix")
    reply = str(_run("model_map --global"))
    assert "not saved" in reply.lower() and "backend" not in _saved(home)
