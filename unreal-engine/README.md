<p align="center"><img src="../assets/unreal-engine.jpg" alt="Unreal Engine: official Hermes Agent plugin" width="100%"></p>

# Unreal Engine

> Part of [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly the `unreal-engine` entry in Hermes' built-in MCP catalog plus the optional `unreal-mcp` skill.

Drive a running [Unreal Editor](https://www.unrealengine.com) from Hermes Agent
through Epic's official **Unreal MCP** plugin, which runs an MCP server inside
the editor process (Streamable HTTP on `127.0.0.1:8000/mcp`, loopback only, no
authentication).

This is a portable [Agent Plugins v1](https://agent-plugins.org) package. One
install gives you:

- `mcp.json`: the `unreal-engine` MCP server, registered for every session
  while the plugin is enabled. No `mcp_servers` edits in config.yaml.
- `skills/unreal-mcp/`: the workflow skill. It covers tool discovery,
  one-call-at-a-time discipline on the editor's game thread, plain-English to
  scene translation, and visual verification. It also has reference files:
  toolsets, advanced workflows, scene-craft numbers, recipes and pitfalls.

## Install

```bash
hermes plugins install unreal-engine
```

Then, once per project, in Unreal Editor 5.8+:

1. **Edit > Plugins**: enable **Unreal MCP** and **AllToolsets** (Unreal MCP
   ships the server; AllToolsets ships the tools). Restart the editor.
2. **Edit > Editor Preferences > General > Model Context Protocol**: turn on
   **Auto Start Server** (or run `ModelContextProtocol.StartServer` in the
   editor console).

Start the editor first, then a new Hermes session. The tools register as
`mcp__unreal_engine__*`. The skill is listed by `skills_list` under the
plugin's namespace. To check that the server is up:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/mcp
```

`000` means nothing is listening. Hermes then logs
`Connect call failed ('127.0.0.1', 8000)` for `unreal-engine` and the session
has no Unreal tools.

## Changed port, longer timeouts, fewer tools

A server in `config.yaml` with the same name replaces the plugin's entry. Use
one when you changed the port or path in Editor Preferences, need a longer
per-call timeout for imports and renders, or want to filter tools:

```yaml
mcp_servers:
  unreal-engine:
    url: http://127.0.0.1:8000/mcp
    timeout: 600
```

## Already used the MCP catalog entry?

Earlier Hermes versions installed this server with
`hermes mcp install unreal-engine`, which wrote `mcp_servers.unreal-engine` to
config.yaml. The plugin uses the same server name on purpose: your existing
entry keeps working and replaces the plugin's entry, so you get one server,
not two copies of every tool. Hermes logs
`Portable MCP server 'unreal-engine' conflicts with native config; skipping`.
To use the plugin's entry instead, remove yours with
`hermes mcp remove unreal-engine`.

## Security notes

- Unreal MCP is experimental. Its tools change the open project, and some run
  Python or console commands inside the editor. Treat it like the terminal
  tool.
- The server is loopback-only and unauthenticated by Epic's design. Never bind
  it wider.
- Unreal logs on start that data sent through the plugin to an LLM service is
  Licensed Technology under the UE EULA. The user is responsible for making
  sure their model provider does not train on it.
- Nothing in this package phones home. Its only network access is the local
  MCP endpoint.

## Credits

Skill authored for Hermes Agent. Unreal MCP by Epic Games. MIT.
