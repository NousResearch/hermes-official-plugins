"""``hermes n8n setup``: add the ``n8n-official`` MCP server to config.yaml."""

from __future__ import annotations

from urllib.parse import urlsplit

SERVER = "n8n-official"
URL_SUFFIX = "/mcp-server/http"
WHERE = "n8n Settings > Instance-level MCP > Connect"


def setup_cli(parser) -> None:
    parser.add_argument("action", nargs="?", choices=["setup"], default="setup")
    parser.add_argument("--url", help=f"MCP Server URL from {WHERE} (ends in {URL_SUFFIX})")


def handle_cli(args) -> None:
    setup(args.url)


def _ask_url() -> str:
    print(f"In {WHERE}, enable MCP access and copy the full Server URL (it ends in {URL_SUFFIX}).")
    print("Use that URL, not your editor URL. Hermes must be able to reach it.")
    try:
        return input("MCP Server URL: ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nNo URL entered; nothing changed.") from None


def _checked(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SystemExit(f"Not an http(s) URL: {url!r}. Copy the Server URL from {WHERE}; nothing changed.")
    if not parts.path.rstrip("/").endswith(URL_SUFFIX):
        print(f"Note: n8n's MCP Server URL usually ends in {URL_SUFFIX}. Using this one as given.")
    return url


def setup(url: str | None) -> None:
    """Write ``mcp_servers.n8n-official`` (url + OAuth), the shape the old MCP catalog wrote. An existing
    entry is kept, so a catalog install stays one server; ``url`` replaces only its URL."""
    from hermes_cli.config import load_config, save_config

    config = load_config()
    servers = config.get("mcp_servers") if isinstance(config.get("mcp_servers"), dict) else {}
    existing = servers.get(SERVER) if isinstance(servers.get(SERVER), dict) else None
    if existing is not None and url is None:
        print(f"'{SERVER}' is already in config.yaml ({existing.get('url')}); keeping it. "
              "Pass --url to change its URL.")
    else:
        url = _checked(_ask_url() if url is None else url)
        entry = {**existing, "url": url} if existing is not None else {"url": url, "auth": "oauth", "enabled": True}
        config["mcp_servers"] = {**servers, SERVER: entry}
        save_config(config)
        print(f"Saved MCP server '{SERVER}' to config.yaml: {url}")
    print(f"Sign in to n8n: hermes mcp login {SERVER} (or Authorize in the desktop app or dashboard).")
    print("Then start a new session or run /reload-mcp to load the n8n tools.")
