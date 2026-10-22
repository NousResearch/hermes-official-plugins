"""``/delegation`` under ``plugins.isolation: host``: the plugin runs in a child process, and a switch
still lands in the Hermes process that ``delegate_task`` reads (``ctx.delegation_backends`` is a host
remote facade). A handler that imported ``agent.delegation_backends`` itself would switch the plugin
host's own copy and leave this process's session untouched.

Skips on a core without the facade, and on Windows (the host is a POSIX child process).
"""

from __future__ import annotations

import os
import shutil

import pytest

if os.name == "nt":
    pytest.skip("the plugin host is a POSIX child process", allow_module_level=True)
backends = pytest.importorskip("agent.delegation_backends", reason="core without per-session delegation backends")
isolation = pytest.importorskip("hermes_cli.plugin_isolation", reason="core without the plugin host")
if "delegation_backends" not in getattr(isolation, "HOST_REMOTE_FACADES", ()):
    pytest.skip("core without ctx.delegation_backends as a host facade", allow_module_level=True)

import hermes_yaml as yaml  # noqa: E402

ROUTES = [{"name": "fast", "description": "Quick.", "provider": "openrouter", "model": "m1"}]
KEY = "chat-hosted-1"


@pytest.fixture
def hosted(tmp_path, plugin_root, monkeypatch):
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager

    home = tmp_path / "os-home" / ".hermes"
    shutil.copytree(plugin_root, home / "plugins" / "advanced-delegation",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["advanced-delegation"], "isolation": "host"},
        "delegation": {"backend": "default", "backends": {"model_map": {"options": ROUTES, "default": "fast"}}}}),
        encoding="utf-8")
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    manager = PluginManager()
    manager.discover_and_load()
    try:
        assert manager._plugins["advanced-delegation"].error is None
        yield plugins_mod.get_plugin_command_handler("delegation"), home
    finally:
        backends.set_session_backend(KEY, None)
        manager.unload()
        manager._plugin_host().shutdown()


def _run(handler, args):
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    return invoke_plugin_command(handler, args, source=None, session_id=KEY, session_key=KEY, platform="telegram")


def test_a_hosted_session_switch_lands_in_this_process(hosted):
    fn, home = hosted
    reply = _run(fn, "model_map")
    assert "this session" in reply
    assert backends.get_session_backend(KEY) == "model_map"
    assert _run(fn, "").startswith("Delegation mode: Model routes (this session)")


def test_a_hosted_global_switch_saves_and_clears_this_processs_override(hosted):
    fn, home = hosted
    backends.set_session_backend(KEY, "model_map")  # this process's override, which the save must drop
    assert "saved for this profile" in _run(fn, "model_map --global")
    assert backends.get_session_backend(KEY) is None
    assert yaml.safe_load((home / "config.yaml").read_text())["delegation"]["backend"] == "model_map"
