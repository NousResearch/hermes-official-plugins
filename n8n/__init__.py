"""n8n — connect Hermes to your n8n instance's official MCP server.

The server URL is different for every user, and a portable ``mcp.json`` cannot hold one (Agent
Plugins v1 forbids expanding a remote URL), so the plugin does not register the server itself.
``hermes n8n setup`` writes it to config.yaml as a normal ``mcp_servers.n8n-official`` entry, the
exact shape Hermes' old MCP catalog wrote; ``hermes mcp login n8n-official`` then signs in through
Hermes' MCP OAuth flow.
"""

from __future__ import annotations

from .cli import handle_cli, setup_cli


def register(ctx) -> None:
    """Plugin entry point. Registers ``hermes n8n``; no model tools (they come from the MCP server)."""
    ctx.register_cli_command(
        name="n8n", help="Connect Hermes to your n8n instance's MCP server",
        description="Add your n8n instance's MCP server to Hermes (OAuth, no API key).",
        setup_fn=setup_cli, handler_fn=handle_cli)


__all__ = ["register"]
