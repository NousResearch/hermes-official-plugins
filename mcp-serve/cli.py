"""``hermes mcp-serve`` (formerly ``hermes mcp serve`` in core)."""

from __future__ import annotations


def setup_cli(parser) -> None:
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging on stderr")
    parser.add_argument(
        "--tools", action="store_true",
        help="Also expose Hermes' web, browser, vision, image generation, skills and TTS tools to the "
             "MCP client, under this profile's credentials (off by default; never terminal, file or "
             "kanban tools)")
    parser.add_argument(
        "--accept-hooks", action="store_true",
        help="With --tools: run the shell hooks in config.yaml without the first-use consent check")


def handle_cli(args) -> None:
    from .bridge import run_mcp_server

    run_mcp_server(verbose=bool(getattr(args, "verbose", False)), include_tools=bool(getattr(args, "tools", False)),
                   accept_hooks=bool(getattr(args, "accept_hooks", False)))
