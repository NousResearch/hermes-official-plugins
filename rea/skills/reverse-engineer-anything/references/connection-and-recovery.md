# Connection and task recovery

Read this guide only for an observed connection, command or provider blocker.
Keep the user's target and question in view. Explain what prevented the
operation and the next useful step; after repair, continue the investigation.

## Choose the remedy from the observed failure

| Observation | Meaning and next step |
| --- | --- |
| `tool_search` finds no REA tools | The plugin is disabled or the server failed to start. Ask the user to run `hermes plugins list` (the `rea` row must say enabled), then start a new session; a `mcp_servers.rea` entry in `config.yaml` replaces the plugin's server. Inspect whether it is an accidental setup-generated duplicate or an intentional engine configuration before proposing changes; preserve deliberate user settings. Do not run `rea setup`. |
| The server fails to start | REA runs through `npx -y rea-agents@6.7.0 mcp` and needs Node.js 22.19+, 24.11+ or 26+ (not 23 or 25) with `npx` on PATH. The first start downloads the package into npm's cache. Check `node --version` and `npx --version` in the shell Hermes uses. |
| REA returns `provider_unavailable` or an unavailable tool entry | Read that operation's provider diagnostics or `binary_session.result.tool_availability`. Expand each group's tool names and inherit the shared client requirements unless a group overrides them. Use the operation's availability, reason and remediation; resolve only the prerequisite the task needs. A complete tool catalog does not mean every operation is available. |
| A call is refused by Hermes | Hermes policy (a disabled toolset, a cron run, a profile restriction) blocked it before REA ran. Report it; do not retry the same call. |

Static JavaScript and ordinary ZIP inventory need neither Hopper nor Ghidra.
HAR inspection is offline and does not require a live browser. Native
decompilation (Ghidra plus a JDK, or Hopper, or IDA), Android analysis (JADX
and Java) and firmware extraction (Binwalk, Unblob) have their own
bring-your-own prerequisites; REA never installs them, and neither should you
without the user's go. Use supported inventory/static evidence when it answers
part of the question, and name the unavailable evidence separately. Do not
substitute another target. Label observations from ordinary shell tools as
such; only actual REA output supplies REA Evidence.

## Supply native engine locations explicitly

Hermes filters arbitrary inherited environment variables before launching MCP
servers. Exporting `GHIDRA_INSTALL_DIR` in the shell alone is not sufficient for
this pinned REA build. The plugin forwards `GHIDRA_INSTALL_DIR`, `JAVA_HOME`,
`REA_ANALYSIS_PROVIDER`, `REA_KUNA_COMMAND`, `REA_TSHARK_COMMAND`,
`REA_CAPINFOS_COMMAND`, `REA_SIGROK_COMMAND`, `REA_WABT_BIN_DIRECTORY` and
`REA_PWNTOOLS_PYTHON` when Hermes itself has them: in `~/.hermes/.env` (or the
profile's `.env`) or in the environment the gateway starts from. An unset one
is left out. After the user adds one, a new session restarts the server. Do not
weaken Hermes' environment filter.

To pin values per machine instead, set a complete native `mcp_servers.rea`
entry with the normal Hermes configuration interface. This is an intentional
override, not the duplicate registration made by `rea setup`:

```yaml
mcp_servers:
  rea:
    command: npx
    args: ["-y", "rea-agents@6.7.0", "mcp"]
    timeout: 1000
    env:
      GHIDRA_INSTALL_DIR: /absolute/path/to/ghidra_12.1.4_PUBLIC
      JAVA_HOME: /absolute/path/to/jdk-21
      REA_GHIDRA_STARTUP_TIMEOUT_MS: "900000"
```

Replace the engine paths with the user's actual installations. The explicit
entry replaces the plugin's MCP settings, so retain the reviewed package pin
and both deadlines, and revisit the pin when updating the plugin. The bundled
skill remains available. Restart the session after changing launch settings.
Static JavaScript and PE-resource inspection do not require these engines.

## Native first-query timeouts

The first Ghidra-backed query performs import and auto-analysis. `open_binary`
only validates the target; repeated opens do not make analysis faster. Keep one
session for related questions and explain the initial wait before starting it.

This plugin sets `REA_GHIDRA_STARTUP_TIMEOUT_MS` to `900000` in its server
environment and declares a 1000-second Hermes call deadline for REA, so a large
executable's first native query can run past the normal 300-second MCP tool
deadline. A query that still runs past 1000 seconds times out; explain the
limit, and offer a native `mcp_servers.rea` entry with a larger `timeout` only
if the user wants to wait longer.

Distinguish REA's structured `provider_timeout` from a Hermes/client timeout.
The client deadline must exceed the backend startup deadline so the backend can
finish and return its result. If a user's same-name `mcp_servers.rea` entry
replaces the plugin configuration, it must retain the pinned launch command,
server environment and appropriate native `timeout` setting. Do not modify
unrelated server budgets or silently override a deliberate user configuration.

Do not immediately retry a cancelled or timed-out analysis: it can discard the
Ghidra database and repeat the expensive import. Explain the observed limit,
check the effective configuration and provider diagnostics, and retry only
after resolving the cause. A longer budget permits waiting; it does not prove
progress or guarantee completion. Honour user cancellation and do not restart
cancelled work unless requested.

## Continue through the CLI

When MCP is unavailable but the shell works, the same workflows run as CLI
commands at the same pinned version:

```bash
npx -y rea-agents@6.7.0 analyze-javascript-application /absolute/path/to/app --json > app-evidence.json
jq -c '{source: {kind: "inline", evidence: .}, view: {kind: "summary"}}' app-evidence.json > app-view.json
npx -y rea-agents@6.7.0 inspect-analysis-view app-view.json
```

CLI JavaScript JSON is complete and can be very large; redirect it rather than
loading it into context. Use an existing JSON processor if `jq` is absent,
without installing one just for this recipe. Native CLI analysis still
requires its selected engine, and all CLI operations retain the host's
execution permissions.
