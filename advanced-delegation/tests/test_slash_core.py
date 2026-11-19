"""``/delegation`` against core's real ``ctx.delegation_backends`` and real choice picker.

Skips on a core without them (``agent.delegation_backends.DelegationBackends``,
``hermes_cli.plugin_choices``). The plugin is installed into a temp ``HERMES_HOME``, loaded by core's
``PluginManager``, and its registered handler is called through ``invoke_plugin_command`` with a real
asker bound (or none, as on a surface that cannot ask), the way a surface does.

``--now`` depends on the core: one with ``set_global_backend(..., apply_now=)`` applies the mode to the
running session; an older one only saves, and the reply says so. Each case runs on the core it is for.
"""

from __future__ import annotations

import inspect
import shutil

import pytest

backends = pytest.importorskip("agent.delegation_backends", reason="core without delegation backends API")
plugin_choices = pytest.importorskip("hermes_cli.plugin_choices", reason="core without ctx.ask_choice")
if not hasattr(backends, "DelegationBackends"):
    pytest.skip("core without ctx.delegation_backends", allow_module_level=True)

import hermes_yaml as yaml  # noqa: E402

ROUTES = [{"name": "fast", "description": "Quick.", "provider": "openrouter", "model": "m1"}]
KEY = "chat-core-1"
APPLY_NOW = "apply_now" in inspect.signature(backends.set_global_backend).parameters
needs_apply_now = pytest.mark.skipif(not APPLY_NOW, reason="core without set_global_backend(apply_now=)")
before_apply_now = pytest.mark.skipif(APPLY_NOW, reason="core with set_global_backend(apply_now=)")


@pytest.fixture
def handler(tmp_path, plugin_root):
    from hermes_cli.plugins import PluginManager

    home = tmp_path / "os-home" / ".hermes"
    shutil.copytree(plugin_root, home / "plugins" / "advanced-delegation",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["advanced-delegation"]},
        "delegation": {"backend": "default", "backends": {"model_map": {"options": ROUTES, "default": "fast"}}}}),
        encoding="utf-8")
    manager = PluginManager()
    manager.discover_and_load()
    try:
        yield manager._plugin_commands["delegation"]["handler"], home
    finally:
        manager.unload()


def _saved(home):
    return yaml.safe_load((home / "config.yaml").read_text())["delegation"]["backend"]


def _run(handler, args, asker=None):
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    call = lambda: invoke_plugin_command(handler, args, source=None, session_id=KEY, session_key=KEY,  # noqa: E731
                                         platform="telegram")
    if asker is None:
        return call()
    with plugin_choices.bind_asker(asker):
        return call()


def test_a_switch_saves_through_core_for_the_next_session(handler):
    fn, home = handler
    reply = _run(fn, "model_map")
    assert _saved(home) == "model_map"
    assert "next session" in reply and "--now" in reply


def test_the_picker_asks_the_mode_then_when_and_saves_for_the_next_session(handler):
    fn, home = handler
    seen = []

    def asker(question, labels):  # the surface's clarify prompt: buttons on Telegram
        seen.append(labels)
        return labels[1] if len(seen) == 1 else labels[0]  # Model routes, then "Next session"

    reply = _run(fn, "", asker)
    assert seen[0][0].startswith("Default (current)") and seen[0][1].startswith("Model routes")
    assert seen[1][0].startswith("Next session (recommended)") and seen[1][1].startswith("Now (rebuilds")
    assert _saved(home) == "model_map" and "next session" in reply


def test_the_listing_names_cores_reason_for_a_backend_that_cannot_run(handler):
    fn, home = handler
    data = yaml.safe_load((home / "config.yaml").read_text())
    data["delegation"]["backend"] = "ghost"
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    reply = _run(fn, "")
    assert "ghost" in reply and "⚠" in reply


def test_with_no_asker_bound_the_command_answers_with_the_text_listing(handler):
    fn, home = handler  # a hook, cron or event-loop caller: ctx.ask_choice returns None
    reply = _run(fn, "")
    assert reply.startswith("Delegation mode: Default") and "2. Model routes" in reply
    assert "/delegation <mode> --now" in reply and _saved(home) == "default"


def test_a_managed_install_reports_cores_refusal(handler, monkeypatch):
    fn, home = handler
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "nix")
    reply = _run(fn, "model_map")
    assert reply.startswith("Not switched:") and "nix" in reply.lower()
    assert _saved(home) == "default"


@before_apply_now
def test_now_on_a_core_before_apply_now_saves_and_says_it_needs_a_newer_hermes(handler):
    fn, home = handler
    reply = _run(fn, "model_map --now")
    assert _saved(home) == "model_map"
    assert "newer Hermes" in reply and "next session" in reply


def _agent():
    """This session's real agent, built on the saved mode (Default: delegate_task without ``option``)."""
    from run_agent import AIAgent
    return AIAgent(base_url="http://127.0.0.1:9/v1", api_key="test", model="test-model", quiet_mode=True,
                   enabled_toolsets=["delegation"], session_id=KEY, skip_context_files=True, skip_memory=True)


def _task_choices(agent):
    tool = next(t for t in agent.tools if t["function"]["name"] == "delegate_task")
    return set(tool["function"]["parameters"]["properties"]["tasks"]["items"]["properties"])


@needs_apply_now
def test_now_rebuilds_this_sessions_delegate_task_at_its_next_turn_and_a_plain_save_does_not(handler,
                                                                                            monkeypatch):
    from agent.delegation_backends import apply_requested_tool_rebuild  # what core runs at turn start
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)  # the session is the agent's (session_id KEY)
    fn, home = handler
    agent = _agent()
    assert "option" not in _task_choices(agent)
    assert "next session" in _run(fn, "model_map")
    assert not apply_requested_tool_rebuild(agent) and "option" not in _task_choices(agent)
    reply = _run(fn, "model_map --now")
    assert "applied to this chat" in reply and _saved(home) == "model_map"
    assert apply_requested_tool_rebuild(agent) and "option" in _task_choices(agent)
