"""``register()`` hands core both backends; the bundled ``default`` stays in core."""

from __future__ import annotations

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

import advanced_delegation_plugin  # noqa: E402


def test_register_hands_core_both_backends_and_the_cli_command():
    registered, cli_commands, slash_commands = [], {}, {}

    class Ctx:
        def register_delegation_provider(self, provider):
            registered.append(provider)

        def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
            cli_commands[name] = (setup_fn, handler_fn)

        def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
            slash_commands[name] = handler

        def __getattr__(self, name):  # any other registrar the plugin uses is accepted and ignored here
            return lambda *a, **k: None

    advanced_delegation_plugin.register(Ctx())
    assert sorted(p.name for p in registered) == ["model_map", "profile_routes"]
    assert set(cli_commands) == {"delegation"}
    assert set(slash_commands) == {"delegation"}


def test_slash_delegation_registers_through_the_real_plugin_manager(tmp_path, plugin_root):
    """No built-in command named /delegation shadows it, so core accepts the registration."""
    import shutil

    import hermes_yaml as yaml
    from hermes_cli.plugins import PluginManager

    home = tmp_path / "os-home" / ".hermes"
    shutil.copytree(plugin_root, home / "plugins" / "advanced-delegation",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["advanced-delegation"]}}),
                                      encoding="utf-8")
    manager = PluginManager()
    manager.discover_and_load()
    try:
        entry = manager._plugin_commands["delegation"]
        assert "Delegation mode: Default" in entry["handler"]("")
    finally:
        manager.unload()


def test_hermes_delegation_config_show_runs_through_the_real_cli(tmp_path, plugin_root):
    """Installed and enabled in a temp HERMES_HOME, the plugin adds ``hermes delegation`` to the real CLI."""
    import os
    import shutil
    import subprocess
    import sys

    import hermes_yaml as yaml

    home = tmp_path / "os-home" / ".hermes"
    shutil.copytree(plugin_root, home / "plugins" / "advanced-delegation",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["advanced-delegation"]},
        "delegation": {"backend": "profile_routes", "backends": {"profile_routes": {"profiles": [
            {"name": "coder", "description": "Writes code."}]}}}}), encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path / "os-home"), "HERMES_HOME": str(home)}
    show = subprocess.run([sys.executable, "-m", "hermes_cli.main", "delegation", "config", "--show"],
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
                          stdin=subprocess.DEVNULL)
    assert show.returncode == 0, show.stderr
    assert "Profile routes" in show.stdout and "coder" in show.stdout and "Writes code." in show.stdout
    edit = subprocess.run([sys.executable, "-m", "hermes_cli.main", "delegation", "config"],
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
                          stdin=subprocess.DEVNULL)
    assert edit.returncode == 1 and "interactive terminal" in edit.stderr
