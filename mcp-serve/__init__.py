"""mcp-serve: run Hermes as an MCP server (``hermes mcp-serve``).

Formerly built into hermes-agent core as ``hermes mcp serve`` (``mcp_serve.py``). Any MCP client
(Claude Code, Claude Desktop, Cursor, Codex, ...) can list and read your messaging conversations, poll
live events, send messages and answer approvals; ``--tools`` adds a curated slice of Hermes' own tools.
The bridge's ten tool names, schemas and descriptions are unchanged; MCP clients configured with
``["mcp", "serve"]`` change their args to ``["mcp-serve"]``.

Wiring goes only through the public plugin API (``ctx.register_cli_command``); the server module loads
only when the command runs, so discovery stays cheap.
"""

from __future__ import annotations

from .cli import handle_cli, setup_cli


def register(ctx) -> None:
    """Plugin entry point, called by the Hermes plugin loader."""
    ctx.register_cli_command(
        name="mcp-serve", help="Run Hermes as an MCP server (expose conversations, optionally tools, to other agents)",
        description="Run Hermes as a stdio MCP server. MCP client config: "
                    '{"command": "hermes", "args": ["mcp-serve"]}. Add --tools to also expose Hermes\' '
                    "web, browser, vision, image generation, skills and TTS tools.",
        setup_fn=setup_cli, handler_fn=handle_cli)


__all__ = ["register"]
