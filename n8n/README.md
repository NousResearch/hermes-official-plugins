<p align="center"><img src="../assets/n8n.jpg" alt="n8n: official Hermes Agent plugin" width="100%"></p>

# n8n

> Part of [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly the `n8n-official` entry in Hermes' built-in MCP catalog.

Connect Hermes to your [n8n](https://n8n.io) instance's official MCP server,
self-hosted or n8n Cloud. Hermes can then search, inspect, run and edit the
workflows your n8n account lets it use. Sign-in is OAuth in your browser; no n8n
API key.

## Install

```bash
hermes plugins install n8n
hermes n8n setup
hermes mcp login n8n-official
```

Before `setup`, in n8n (owner or admin):

1. **Settings > Instance-level MCP**: enable MCP access.
2. Open **Connect** and copy the full **Server URL**. It ends in
   `/mcp-server/http`. Use that URL, not your editor URL. The machine running
   Hermes must be able to reach it.

`hermes n8n setup` asks for that URL (or takes `--url <url>`) and writes it to
config.yaml as the `n8n-official` MCP server. `hermes mcp login n8n-official`
(or **Authorize** in the desktop app or dashboard) opens n8n's approval page.
Then start a new session or run `/reload-mcp`.

`setup` writes this entry:

```yaml
mcp_servers:
  n8n-official:
    url: https://<your-instance>/mcp-server/http
    auth: oauth
    enabled: true
```

From there it is an ordinary Hermes MCP server: `hermes mcp test n8n-official`,
`hermes mcp configure n8n-official` to choose tools, `hermes mcp login n8n-official`
to sign in again, `hermes mcp remove n8n-official` to remove it. If your
instance restricts OAuth callback URLs, allow the one Hermes uses.

## Why a setup command and not a bundled server

Every n8n instance has its own URL. A portable plugin's `mcp.json` can only hold
a fixed URL, so the server has to go in your config.yaml, where `${VAR}` and
per-user values work. The plugin writes it there once; disabling or uninstalling
the plugin leaves the server in place. Remove it with `hermes mcp remove n8n-official`.

## Already used the MCP catalog entry?

Earlier Hermes versions installed this server with `hermes mcp install n8n-official`.
That entry has the same name and shape, so you do not need to do anything. If
you run `hermes n8n setup` anyway, it keeps your entry and its tool choices.
`--url` replaces just the URL.

The older stdio n8n bridge used a server named `n8n`; that entry is untouched.

## Access

n8n controls access through your account's permissions and its MCP settings.
Search can show previews of workflows you can view. Turn on **Available in MCP**
for each workflow you want Hermes to inspect fully, execute or modify. Which
tools exist depends on your n8n version. Some tools change or run workflows.

## Credits

Nous Research. n8n's MCP server by n8n. MIT.
