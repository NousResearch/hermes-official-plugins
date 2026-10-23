"""``/delegation`` under ``plugins.isolation: host``: the plugin runs in a child process, and the save (and,
with ``--now``, the rebuild of the chat's tools) still lands in the Hermes process the chat runs in
(``ctx.delegation_backends`` is a host remote facade). A handler that imported ``agent.delegation_backends``
itself would act on the plugin host's own copy.

Skips on a core without the facade, and on Windows (the host is a POSIX child process).
"""

from __future__ import annotations

import inspect
import os
import shutil

import pytest

if os.name == "nt":
    pytest.skip("the plugin host is a POSIX child process", allow_module_level=True)
backends = pytest.importorskip("agent.delegation_backends", reason="core without delegation backends API")
isolation = pytest.importorskip("hermes_cli.plugin_isolation", reason="core without the plugin host")
if "delegation_backends" not in getattr(isolation, "HOST_REMOTE_FACADES", ()):
    pytest.skip("core without ctx.delegation_backends as a host facade", allow_module_level=True)

import hermes_yaml as yaml  # noqa: E402

ROUTES = [{"name": "fast", "description": "Quick.", "provider": "openrouter", "model": "m1"}]
KEY = "chat-hosted-1"
APPLY_NOW = "apply_now" in inspect.signature(backends.set_global_backend).parameters


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
        manager.unload()
        manager._plugin_host().shutdown()


def _run(handler, args):
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    return invoke_plugin_command(handler, args, source=None, session_id=KEY, session_key=KEY, platform="telegram")


def _saved(home):
    return yaml.safe_load((home / "config.yaml").read_text())["delegation"]["backend"]


def test_a_hosted_switch_saves_for_the_next_session(hosted):
    fn, home = hosted
    reply = _run(fn, "model_map")
    assert "next session" in reply and _saved(home) == "model_map"
    assert _run(fn, "").startswith("Delegation mode: Model routes")


@pytest.mark.skipif(APPLY_NOW, reason="core with set_global_backend(apply_now=)")
def test_hosted_now_on_a_core_before_apply_now_still_saves(hosted):
    # The older core's refusal of the keyword crosses the host wire as a TypeError; the plugin then saves.
    fn, home = hosted
    reply = _run(fn, "model_map --now")
    assert "newer Hermes" in reply and _saved(home) == "model_map"


@pytest.mark.skipif(not APPLY_NOW, reason="core without set_global_backend(apply_now=)")
def test_hosted_now_rebuilds_the_tools_of_this_processs_session(hosted, monkeypatch):
    from agent.delegation_backends import apply_requested_tool_rebuild  # what core runs at turn start
    from run_agent import AIAgent
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    fn, home = hosted
    agent = AIAgent(base_url="http://127.0.0.1:9/v1", api_key="test", model="test-model", quiet_mode=True,
                    enabled_toolsets=["delegation"], session_id=KEY, skip_context_files=True, skip_memory=True)
    reply = _run(fn, "model_map --now")
    assert "applied to this chat" in reply and _saved(home) == "model_map"
    assert apply_requested_tool_rebuild(agent)  # the request landed here, not in the plugin host
