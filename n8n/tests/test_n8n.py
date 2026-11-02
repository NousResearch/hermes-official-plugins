"""The plugin loaded the way users get it (copied into the profile's plugins/, enabled in config.yaml,
discovered by core's PluginManager), driven through the ``hermes n8n`` parser core builds from it. The
result is read back through Hermes' own MCP config loader, the one every session connects from."""

from __future__ import annotations

import argparse
import shutil

import pytest

import n8n_plugin

URL = "https://acme.app.n8n.cloud/mcp-server/http"


@pytest.fixture
def n8n_cli(_hermes_home, plugin_root, monkeypatch):
    """Run ``hermes n8n <argv>`` through the command the real plugin manager registered."""
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager

    shutil.copytree(plugin_root, _hermes_home / "plugins" / "n8n",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    config = _hermes_home / "config.yaml"
    config.write_text("plugins:\n  enabled: [n8n]\n" + (config.read_text() if config.exists() else ""))
    empty_bundled = _hermes_home.parent / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    manager = PluginManager()
    manager.discover_and_load()
    command = manager._cli_commands["n8n"]

    def run(*argv):
        parser = argparse.ArgumentParser()
        command["setup_fn"](parser)
        command["handler_fn"](parser.parse_args(list(argv)))

    yield run
    manager.unload()


def _loaded_servers():
    from tools.mcp_tool_config import _load_mcp_config
    return _load_mcp_config()


def test_registers_only_the_cli_command():
    calls = []

    class Ctx:
        def register_cli_command(self, **kw):
            calls.append(kw)

    n8n_plugin.register(Ctx())
    assert [c["name"] for c in calls] == ["n8n"]


def test_setup_adds_the_users_url_as_an_oauth_server(n8n_cli):
    """The same entry the old catalog wrote, so ``hermes mcp login`` and Hermes' MCP OAuth manager take it."""
    n8n_cli("setup", "--url", f"  {URL} ")
    assert _loaded_servers()["n8n-official"] == {"url": URL, "auth": "oauth", "enabled": True}


def test_setup_prompts_for_the_url(n8n_cli, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: URL)
    n8n_cli()
    assert _loaded_servers()["n8n-official"]["url"] == URL


def test_existing_catalog_entry_stays_one_server(_hermes_home, n8n_cli):
    """A user who installed ``n8n-official`` from the old MCP catalog keeps that entry (tool filter
    included); --url changes only its URL."""
    old = {"url": "https://old.example.com/mcp-server/http", "auth": "oauth", "enabled": True,
           "tools": {"include": ["search_workflows"]}}
    from hermes_cli.config import load_config, save_config
    config = load_config()
    config["mcp_servers"] = {"n8n-official": old}
    save_config(config)

    n8n_cli("setup")
    assert _loaded_servers() == {"n8n-official": old}
    n8n_cli("setup", "--url", URL)
    assert _loaded_servers() == {"n8n-official": {**old, "url": URL}}


def test_non_http_url_changes_nothing(n8n_cli):
    with pytest.raises(SystemExit, match="Not an http"):
        n8n_cli("setup", "--url", "n8n.example.com/mcp-server/http")
    assert "n8n-official" not in _loaded_servers()
