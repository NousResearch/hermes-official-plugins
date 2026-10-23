"""register(ctx) wiring: ``hermes mcp-serve`` goes through the public plugin API, and the plugin
registers no model tools (its tools live on the MCP wire, not in the agent's tool list)."""

from __future__ import annotations

import argparse

import mcp_serve_plugin


class RecordingCtx:
    def __init__(self):
        self.cli, self.tools = [], []

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)

    def register_tool(self, **kwargs):  # pragma: no cover - must never be called
        self.tools.append(kwargs)


def test_registers_only_the_mcp_serve_command():
    ctx = RecordingCtx()
    mcp_serve_plugin.register(ctx)
    assert ctx.tools == []
    (cmd,) = ctx.cli
    assert cmd["name"] == "mcp-serve"
    parser = argparse.ArgumentParser()
    cmd["setup_fn"](parser)
    defaults = parser.parse_args([])
    assert (defaults.tools, defaults.verbose, defaults.accept_hooks) == (False, False, False)
    assert parser.parse_args(["--tools", "-v"]).tools is True
