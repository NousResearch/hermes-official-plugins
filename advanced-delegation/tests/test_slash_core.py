"""``/delegation`` against core's real ``ctx.delegation_backends`` and real choice picker.

Skips on a core without them (``agent.delegation_backends.DelegationBackends``,
``hermes_cli.plugin_choices``). The plugin is installed into a temp ``HERMES_HOME``, loaded by core's
``PluginManager``, and its registered handler is called through ``invoke_plugin_command`` with a real
asker bound (or none, as on a surface that cannot ask), the way a surface does.
"""

from __future__ import annotations

import shutil

import pytest

backends = pytest.importorskip("agent.delegation_backends", reason="core without per-session delegation backends")
plugin_choices = pytest.importorskip("hermes_cli.plugin_choices", reason="core without ctx.ask_choice")
if not hasattr(backends, "DelegationBackends"):
    pytest.skip("core without ctx.delegation_backends", allow_module_level=True)

import hermes_yaml as yaml  # noqa: E402

ROUTES = [{"name": "fast", "description": "Quick.", "provider": "openrouter", "model": "m1"}]
KEY = "chat-core-1"


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
        backends.set_session_backend(KEY, None)
        manager.unload()


def _run(handler, args, asker=None):
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    call = lambda: invoke_plugin_command(handler, args, source=None, session_id=KEY, session_key=KEY,  # noqa: E731
                                         platform="telegram")
    if asker is None:
        return call()
    with plugin_choices.bind_asker(asker):
        return call()


def test_the_picker_switches_the_session_through_core(handler):
    fn, home = handler
    seen = []

    def asker(question, labels):  # the surface's clarify prompt: buttons on Telegram
        seen.append(labels)
        return labels[1]

    reply = _run(fn, "", asker)
    assert backends.get_session_backend(KEY) == "model_map"
    assert seen and seen[0][0].startswith("Default (current)") and seen[0][1].startswith("Model routes")
    assert "this session" in reply
    assert yaml.safe_load((home / "config.yaml").read_text())["delegation"]["backend"] == "default"


def test_global_saves_and_clears_the_sessions_override(handler):
    fn, home = handler
    _run(fn, "model_map")
    assert backends.get_session_backend(KEY) == "model_map"
    _run(fn, "default --global")
    assert backends.get_session_backend(KEY) is None
    assert yaml.safe_load((home / "config.yaml").read_text())["delegation"]["backend"] == "default"


def test_the_listing_names_cores_reason_for_a_backend_that_cannot_run(handler):
    fn, home = handler
    data = yaml.safe_load((home / "config.yaml").read_text())
    data["delegation"]["backend"] = "ghost"
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    reply = _run(fn, "")
    assert "ghost" in reply and "⚠" in reply


def test_with_no_asker_bound_the_command_answers_with_the_text_listing(handler):
    fn, _home = handler  # a hook, cron or event-loop caller: ctx.ask_choice returns None
    reply = _run(fn, "")
    assert reply.startswith("Delegation mode: Default") and "2. Model routes" in reply
    assert backends.get_session_backend(KEY) is None


def test_global_on_a_managed_install_reports_cores_refusal(handler, monkeypatch):
    fn, home = handler
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "nix")
    reply = _run(fn, "model_map --global")
    assert reply.startswith("Not switched:") and "nix" in reply.lower()
    assert yaml.safe_load((home / "config.yaml").read_text())["delegation"]["backend"] == "default"
