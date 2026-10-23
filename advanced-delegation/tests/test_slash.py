"""``/delegation``: show the mode and save a new one for the next session; ``--now`` also applies it here.

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
        self.calls: list[tuple[str, str, bool]] = []
        self.unavailable: dict[str, str] = {}  # backend -> the reason core gives
        self.refuse: Exception | None = None

    def list_backends(self):
        return [{"name": n, "label": n, "description": "", "available": n not in self.unavailable,
                 "reason": self.unavailable.get(n, "")} for n in ("default", "model_map", "profile_routes")]

    def _save(self, name):
        data = yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8")) or {}
        data.setdefault("delegation", {})["backend"] = name  # what core writes
        (self.home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def set_global_backend(self, name, *, session_key="", apply_now=False):
        self.calls.append((name, session_key, apply_now))
        if self.refuse:
            raise self.refuse
        self._save(name)


class OlderBackends(Backends):
    """The API on a core from before ``apply_now``: it saves, and has no way to apply to the running chat."""

    def set_global_backend(self, name, session_key=""):
        self.calls.append((name, session_key, False))
        self._save(name)


class Ctx:
    """The plugin context ``make_handler`` reads ``ask_choice`` / ``delegation_backends`` from (both absent
    on an older core). ``answers`` are returned by successive asks."""

    def __init__(self, *answers, ask=True, api=None):
        self.asked = []
        self._answers = list(answers)
        if ask:
            def ask_choice(question, choices):
                self.asked.append((question, choices))
                return self._answers.pop(0) if self._answers else None
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


def test_bare_command_lists_the_modes_marks_the_saved_one_and_shows_both_commands(home):
    _write(home, {"backend": "model_map", "backends": {"model_map": {"options": ROUTES, "default": "fast"},
                                                        "profile_routes": {"profiles": [{"name": "coder"}]}}})
    text = str(_run(""))
    assert "1. Default" in text and "2. Model routes" in text and "3. Profile routes" in text
    assert "Model routes" in text.split("\n")[0]                     # the saved mode leads
    assert "fast (default), deep" in text and "coder" in text        # what each mode would route to
    assert "/delegation <mode>" in text and "/delegation <mode> --now" in text
    assert "next session" in text and "--global" not in text


def test_a_plain_switch_saves_through_core_for_the_next_session(home, api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("model_map", Ctx(ask=False, api=api), session_key="chat-7"))
    assert api.calls == [("model_map", "chat-7", False)]
    assert _saved(home)["backend"] == "model_map"
    assert "next session" in reply and "/new" in reply and "--now" in reply
    assert "this chat" in reply  # says plainly that the running chat keeps its mode


def test_now_saves_and_asks_core_to_apply_it_to_this_chat(home, api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES, "default": "deep"}}})
    reply = str(_run("model_map --now", Ctx(ask=False, api=api), session_key="chat-7"))
    assert api.calls == [("model_map", "chat-7", True)]
    assert _saved(home)["backend"] == "model_map"
    assert "applied to this chat now" in reply and "next session" not in reply
    assert "deep (default)" in reply


def test_now_on_a_core_without_apply_now_saves_and_says_it_needs_a_newer_hermes(home):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    api = OlderBackends(home)
    reply = str(_run("model_map --now", Ctx(ask=False, api=api), session_key="chat-7"))
    assert api.calls == [("model_map", "chat-7", False)]
    assert _saved(home)["backend"] == "model_map"
    assert "newer Hermes" in reply and "next session" in reply


def test_without_core_api_a_switch_writes_the_mode_itself_for_the_next_session(home):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("model_map"))
    assert _saved(home)["backend"] == "model_map"
    assert "Model routes" in reply and "next session" in reply


def test_without_core_api_now_saves_and_says_it_needs_a_newer_hermes(home):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("model_map --now"))
    assert _saved(home)["backend"] == "model_map"
    assert "newer Hermes" in reply and "next session" in reply


@pytest.mark.parametrize("word", ["2", "model_map", "models", "Model routes", "model-routes"])
def test_mode_words_and_numbers_are_accepted(home, word):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    _run(word)
    assert _saved(home)["backend"] == "model_map"


@pytest.mark.parametrize("flag", ["--global", "-g", "--session"])
def test_an_unknown_flag_shows_the_usage_and_changes_nothing(home, api, flag):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run(f"model_map {flag}", Ctx(ask=False, api=api)))
    assert flag in reply and "--now" in reply
    assert api.calls == [] and _saved(home)["backend"] == "default"


def test_the_picker_offers_set_up_modes_then_when_and_saves_for_the_next_session(home, api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES, "default": "deep"}}})
    ctx = Ctx("model_map", "next", api=api)
    reply = str(_run("", ctx=ctx, session_key="chat-7"))
    (mode_q, mode_choices), (when_q, when_choices) = ctx.asked
    assert "Default" in mode_q
    # Only modes with something set up are offered; each carries what it routes to.
    assert [c["value"] for c in mode_choices] == ["default", "model_map"]
    assert mode_choices[0]["label"] == "Default (current)" and "deep (default)" in mode_choices[1]["description"]
    assert [c["value"] for c in when_choices] == ["next", "now"]
    assert when_choices[0]["label"] == "Next session (recommended)"
    assert when_choices[1]["label"] == "Now (rebuilds this chat's tools)"
    assert "Model routes" in when_q
    assert api.calls == [("model_map", "chat-7", False)] and "next session" in reply


def test_the_picker_now_choice_applies_to_this_chat(home, api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("", ctx=Ctx("model_map", "now", api=api), session_key="chat-7"))
    assert api.calls == [("model_map", "chat-7", True)] and "applied to this chat now" in reply


def test_cancelling_the_when_question_changes_nothing(home, api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    reply = str(_run("", ctx=Ctx("model_map", None, api=api), session_key="chat-7"))
    assert api.calls == [] and _saved(home)["backend"] == "default"
    assert "unchanged" in reply.lower()


def test_the_picker_without_core_api_saves_without_asking_when(home):
    # Nothing can apply a mode to a running chat on such a core, so the second question is not asked.
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    ctx = Ctx("model_map")
    reply = str(_run("", ctx=ctx))
    assert len(ctx.asked) == 1 and _saved(home)["backend"] == "model_map" and "next session" in reply


def test_a_picker_that_returns_none_falls_back_to_the_text_listing(home, api):
    # ctx.ask_choice returns None when the user cancels and when the surface cannot ask at all.
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    ctx = Ctx(None, api=api)
    reply = str(_run("", ctx=ctx, session_key="chat-7"))
    assert len(ctx.asked) == 1 and "1. Default" in reply and "2. Model routes" in reply
    assert api.calls == []


@pytest.mark.parametrize(("word", "mode_name"), [("model_map", "Model routes"), ("profiles", "Profile routes")])
def test_switching_to_a_mode_with_nothing_configured_says_how_to_set_it_up(home, api, word, mode_name):
    _write(home, {"backend": "default"})
    reply = str(_run(word, Ctx(ask=False, api=api)))
    assert mode_name in reply and "hermes delegation config" in reply
    assert _saved(home)["backend"] == "default" and api.calls == []


def test_switching_back_to_default_always_works(home, api):
    _write(home, {"backend": "model_map", "model": "sub-model"})
    reply = str(_run("default", Ctx(ask=False, api=api)))
    assert _saved(home)["backend"] == "default" and api.calls == [("default", "key-1", False)]
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


@pytest.mark.parametrize("args", ["model_map", "model_map --now"])
def test_core_refusing_the_switch_is_reported_with_its_reason(home, api, args):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    api.refuse = ValueError("Model routes cannot run: route 'fast' has no credentials.")
    reply = str(_run(args, Ctx(ask=False, api=api)))
    assert reply == "Not switched: Model routes cannot run: route 'fast' has no credentials."
    assert _saved(home)["backend"] == "default"


def test_core_refusing_a_managed_install_is_reported_not_raised(home, api):
    _write(home, {"backend": "default", "backends": {"model_map": {"options": ROUTES}}})
    api.refuse = PermissionError("This Hermes install is managed by nix; set delegation.backend there.")
    reply = str(_run("model_map", Ctx(ask=False, api=api)))
    assert reply == "Not switched: This Hermes install is managed by nix; set delegation.backend there."
    assert _saved(home)["backend"] == "default"


def test_without_core_api_a_refused_write_is_reported_not_raised(home, monkeypatch):
    _write(home, {"backends": {"model_map": {"options": ROUTES}}})
    slash.store.delegation_section()  # create the home layout first: a managed install never creates it
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "nix")
    reply = str(_run("model_map"))
    assert "not switched" in reply.lower() and "nix" in reply.lower() and "backend" not in _saved(home)
