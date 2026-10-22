"""``/delegation``: show the current mode and switch it, session-scoped by default, ``--global`` saves.

The handler is called the way core's ``invoke_plugin_command`` calls it on every surface (raw args
plus ``session_key`` / ``platform``). Core's backend API (``ctx.delegation_backends``) and picker
(``ctx.ask_choice``) are feature-detected on the context; tests hand the handler a context with a
recording stand-in for them, or without them to act as an older core. ``test_slash_core.py`` runs the
same command against core's real API.
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


class Backends:
    """``ctx.delegation_backends`` as core hands it to a plugin (plain data), recording what it is asked."""

    def __init__(self, home):
        self.home = home
        self.sessions: dict[str, str] = {}
        self.global_calls: list[tuple[str, str]] = []
        self.unavailable: dict[str, str] = {}  # backend -> the reason core gives
        self.refuse_global: Exception | None = None

    def list_backends(self):
        return [{"name": n, "label": n, "description": "", "available": n not in self.unavailable,
                 "reason": self.unavailable.get(n, "")} for n in ("default", "model_map", "profile_routes")]

    def get_session_backend(self, key):
        return self.sessions.get(key)

    def set_session_backend(self, key, name):
        if name is None:
            self.sessions.pop(key, None)
        elif name in self.unavailable:
            raise ValueError(self.unavailable[name])
        else:
            self.sessions[key] = name

    def set_global_backend(self, name, session_key=""):
        self.global_calls.append((name, session_key))
        if self.refuse_global:
            raise self.refuse_global
        data = yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8")) or {}
        data.setdefault("delegation", {})["backend"] = name  # what core writes
        (self.home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        if session_key:
            self.sessions.pop(session_key, None)


class Ctx:
    """The plugin context ``make_handler`` reads ``ask_choice`` / ``delegation_backends`` from (both absent
    on an older core)."""

    def __init__(self, answer=None, ask=True, api=None):
        self.asked = []
        if ask:
            def ask_choice(question, choices):
                self.asked.append((question, choices))
                return answer
            self.ask_choice = ask_choice
        if api is not None:
            self.delegation_backends = api


def _run(args="", ctx=None, **context):
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    context = {"source": None, "session_id": "s1", "session_key": "key-1", "platform": "cli", **context}
    return invoke_plugin_command(slash.make_handler(ctx or Ctx(ask=False)), args, **context)


@pytest.fixture
def api(home):
    return Backends(home)


def test_bare_command_lists_the_modes_marks_the_current_one_and_says_how_to_switch(home):
    _write(home, {"backend": "model_map", "backends": {"model_map": {"options": ROUTES, "default": "fast"},
                                                        "profile_routes": {"profiles": [{"name": "coder"}]}}})
    text = str(_run(""))
    assert "1. Default" in text and "2. Model routes" in text and "3. Profile routes" in text
    assert "Model routes" in text.split("\n")[0]                     # the current mode leads
    assert "fast (default), deep" in text and "coder" in text        # what each mode would route to
    assert "/delegation" in text and "--global" in text


def test_without_core_api_global_writes_the_mode_itself(home):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("model_map --global"))
    assert _saved(home)["backend"] == "model_map"
    assert "Model routes" in reply and "saved" in reply.lower()


@pytest.mark.parametrize("word", ["2", "model_map", "models", "Model routes", "model-routes"])
def test_mode_words_and_numbers_are_accepted(home, word):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    _run(f"{word} --global")
    assert _saved(home)["backend"] == "model_map"


def test_switch_is_session_scoped_by_default_and_leaves_the_config_alone(home, api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("model_map", Ctx(ask=False, api=api), session_key="chat-7"))
    assert api.sessions == {"chat-7": "model_map"} and api.global_calls == []
    assert _saved(home)["backend"] == "default"
    assert "this session" in reply and "--global" in reply
    listing = str(_run("", Ctx(ask=False, api=api), session_key="chat-7"))
    assert "Model routes" in listing.split("\n")[0]  # the session's mode leads


def test_global_goes_through_core_which_saves_it_and_drops_the_sessions_override(home, api):
    _write(home, {"backends": {"profile_routes": {"profiles": [{"name": "coder"}]}}})
    api.sessions["chat-7"] = "default"
    reply = str(_run("profile_routes --global", Ctx(ask=False, api=api), session_key="chat-7"))
    assert api.global_calls == [("profile_routes", "chat-7")] and "saved" in reply
    assert _saved(home)["backend"] == "profile_routes" and "chat-7" not in api.sessions
    listing = str(_run("", Ctx(ask=False, api=api), session_key="chat-7"))
    assert listing.startswith("Delegation mode: Profile routes")


def test_bare_command_asks_with_the_surfaces_picker_and_switches_the_session(home, api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES, "default": "deep"}}})
    ctx = Ctx(answer="model_map", api=api)
    reply = str(_run("", ctx=ctx, session_key="chat-7"))
    ((question, choices),) = ctx.asked
    assert "Default" in question
    # Only modes with something set up are offered; each carries what it routes to.
    assert [c["value"] for c in choices] == ["default", "model_map"]
    assert choices[0]["label"] == "Default (current)" and "deep (default)" in choices[1]["description"]
    assert api.sessions == {"chat-7": "model_map"} and _saved(home)["backend"] == "default"
    assert "this session" in reply and "default route 'deep'" in reply  # tasks here take the mode's default
    assert "/new" in question  # the picker says up front that the agent's own choices change only on /new


@pytest.mark.parametrize(("target", "routes_to"), [("profile_routes", "the default profile 'coder'"),
                                                   ("default", "delegation.model (sub-model)")])
def test_a_session_switch_says_routing_changes_now_and_choices_follow_the_starting_mode(home, api, target,
                                                                                         routes_to):
    # Saved mode model_map: the chat's delegate_task offers `option`. Switching only this session changes
    # where tasks go, not that schema, and core refuses a task sending a choice the new mode lacks.
    _write(home, {"backend": "model_map", "model": "sub-model", "backends": {
        "model_map": {"options": ROUTES, "default": "fast"},
        "profile_routes": {"profiles": [{"name": "coder"}], "default": "coder"}}})
    reply = str(_run(target, Ctx(ask=False, api=api), session_key="chat-7"))
    assert api.sessions == {"chat-7": target}
    assert "Routing changes now" in reply
    assert "follow the mode this chat started with until /new" in reply
    assert "is refused" in reply and routes_to in reply
    assert "--global" in reply and "/new" in reply


def test_a_picker_that_returns_none_falls_back_to_the_text_listing(home, api):
    # ctx.ask_choice returns None when the user cancels and when the surface cannot ask at all.
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    ctx = Ctx(answer=None, api=api)
    reply = str(_run("", ctx=ctx, session_key="chat-7"))
    assert ctx.asked and "1. Default" in reply and "2. Model routes" in reply
    assert api.sessions == {} and api.global_calls == []


def test_without_core_session_support_a_plain_switch_points_to_global(home):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("model_map"))
    assert "--global" in reply
    assert "backend" not in _saved(home)


@pytest.mark.parametrize(("word", "mode_name"), [("model_map", "Model routes"), ("profiles", "Profile routes")])
def test_switching_to_a_mode_with_nothing_configured_says_how_to_set_it_up(home, api, word, mode_name):
    _write(home, {"backend": "default"})
    reply = str(_run(f"{word} --global", Ctx(ask=False, api=api)))
    assert mode_name in reply and "hermes delegation config" in reply
    assert _saved(home)["backend"] == "default" and api.sessions == {} and api.global_calls == []


def test_switching_back_to_default_always_works(home, api):
    _write(home, {"backend": "model_map", "model": "sub-model"})
    reply = str(_run("default --global", Ctx(ask=False, api=api)))
    assert _saved(home)["backend"] == "default" and api.global_calls == [("default", "key-1")]
    assert "delegation.model" in reply


def test_an_unknown_word_shows_the_usage(home):
    _write(home, {"backend": "default"})
    reply = str(_run("turbo"))
    assert "turbo" in reply and "/delegation" in reply and "default" in reply
    assert _saved(home) == {"backend": "default"}


def test_a_configured_backend_that_is_not_registered_is_flagged_as_the_thing_to_fix(home, api):
    _write(home, {"backend": "something_uninstalled"})
    text = str(_run("", Ctx(ask=False, api=api)))
    assert "something_uninstalled" in text and "not available" in text


def test_the_listing_shows_cores_reason_for_a_backend_that_cannot_run(home, api):
    _write(home, {"backend": "model_map", "backends": {"model_map": {"options": ROUTES}}})
    api.unavailable["model_map"] = "Delegation backend 'model_map' failed its check: no credentials for openrouter."
    text = str(_run("", Ctx(ask=False, api=api)))
    assert "⚠ Delegation backend 'model_map' failed its check: no credentials for openrouter." in text


def test_core_refusing_a_session_switch_is_reported_with_its_reason(home, api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    api.unavailable["model_map"] = "Model routes cannot run: route 'fast' has no credentials."
    reply = str(_run("model_map", Ctx(ask=False, api=api), session_key="chat-7"))
    assert reply == "Not switched: Model routes cannot run: route 'fast' has no credentials."
    assert api.sessions == {}


def test_core_refusing_a_global_switch_on_a_managed_install_is_reported_not_raised(home, api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    api.refuse_global = PermissionError("This Hermes install is managed by nix; set delegation.backend there.")
    reply = str(_run("model_map --global", Ctx(ask=False, api=api)))
    assert reply == "Not switched: This Hermes install is managed by nix; set delegation.backend there."
    assert _saved(home)["backend"] == "default"


def test_without_core_api_a_refused_write_is_reported_not_raised(home, monkeypatch):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    slash.store.delegation_section()  # create the home layout first: a managed install never creates it
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "nix")
    reply = str(_run("model_map --global"))
    assert "not switched" in reply.lower() and "nix" in reply.lower() and "backend" not in _saved(home)
