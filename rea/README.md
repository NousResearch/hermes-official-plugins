<p align="center"><img src="../assets/rea.jpg" alt="REA: official Hermes Agent plugin" width="100%"></p>

# REA

> Part of [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent.

Reverse engineer programs from Hermes with [REA](https://rea.tools)
([morluto/rea](https://github.com/morluto/rea), MIT): inspect native
executables and libraries, .NET assemblies, Electron and JavaScript
applications, Android packages, firmware images, packet and browser captures,
and the running behaviour of browser, Electron and terminal programs. REA
returns structured Evidence (decompiled code, call graphs, application graphs,
captures) and Hermes explains it or builds from it.

This is a portable [Agent Plugins v1](https://agent-plugins.org) package. One
install ships:

- `mcp.json`: the REA MCP server (`rea`), launched as
  `npx -y rea-agents@6.4.0 mcp` over stdio for every new session while the
  plugin is enabled. No `mcp_servers` edit in config.yaml.
- `skills/reverse-engineer-anything/`: REA's own workflow skill (target
  routing, summary-first evidence paging, investigation planning), adapted for
  Hermes: it connects through this plugin instead of `rea setup`, finds tools
  through Hermes' tool search, and asks before steps that run or change
  something.
- `skills/crash-triage/`: from a Linux core dump and the binary that crashed to
  a root cause and a bug report you can paste into an issue. It reads the crash
  with `inspect_recorded_crash`, places the program counter with
  `inspect_binary_layout` and a small standard-library helper that decodes the
  core's mapped-file list, classifies the crash (NULL dereference, stack
  overflow, abort, ...), and never runs the program. Adapted from
  [crashlens](https://github.com/mingleiw/crashlens) (MIT).

## Install

```bash
hermes plugins install rea        # from the Nous plugin catalog
hermes plugins enable rea
```

Then start a new Hermes session and ask, for example, "Use REA to inspect
/Applications/Notes.app and explain how CSV export works." or "./parser
crashed and left ./core. Why?"

Requirements:

- Node.js 22.19+, 24.11+ or 26+ with `npx` on PATH (REA does not support Node
  23 or 25). The first start downloads the pinned package into npm's cache.
- Nothing else for static JavaScript/Electron/ASAR analysis, archive and
  package inventory, Go/Rust binary metadata, PE resources, .NET metadata and
  saved HAR captures.
- For core dumps and offline ELF layout (`inspect_recorded_crash`,
  `inspect_binary_layout`): a Python with pwntools 4.15.0, pyelftools 0.33 and
  Unicorn 2.1.2, named in `~/.hermes/.env` as
  `REA_PWNTOOLS_PYTHON=/path/to/venv/bin/python`. Hermes passes MCP servers only
  a small set of environment variables, so `mcp.json` forwards this one
  explicitly; a value exported only in your shell does not reach REA.
- Bring your own engine for the rest: Ghidra plus a JDK, Hopper or IDA for
  native decompilation; JADX and Java for Android code; Binwalk/Unblob for
  firmware extraction; TShark, sigrok or WABT for their formats. REA never
  installs these, and the skill tells the agent not to either.

Do not also run `npx rea-agents setup --client hermes`. It writes its own
`mcp_servers.rea` entry to config.yaml, which replaces this plugin's pinned
server.

## Native analysis wait budget

Every REA call runs under Hermes' normal MCP tool deadline (300 seconds by
default). The plugin sets Ghidra's internal startup deadline to 900 seconds
so a large import is not cut short on REA's side. A per-server Hermes call
budget for REA ships with Hermes v0.23.0; this package will declare it once
that release is out, so it installs on every current Hermes release today.

`open_binary` validates a target, but the first Ghidra-backed query imports and
analyzes it. Large executables can take several minutes. Subsequent queries
reuse that session; reopening or restarting discards its ephemeral analysis.
Cancellation remains available, and a completed open is not a progress signal.

In a binary-first test on WinSCP 6.7, the console launcher imported in about
30 seconds. The GUI executable exceeded the upstream 330-second default and
completed in 663 seconds with a 1000-second Hermes budget, which needs
Hermes v0.23.0 or newer. A subsequent real Hermes
chat using the same native target completed its first query in 580 seconds,
then made successful follow-up decompilation and caller queries. Both runs
recovered 28,701 procedures. The Windows executable was analyzed statically,
never launched. These are observations on one Linux host, not completion-time
guarantees. See the bundled
[recovery guide](skills/reverse-engineer-anything/references/connection-and-recovery.md)
for deadline troubleshooting.

Pseudocode is decompiler output, not reconstructed original source. The skill
asks Hermes to cross-check consequential claims with assembly and references,
label inferred names/types, and report missing symbols and analysis warnings.

## What you get

About 170 tools from REA 6.4.0, registered as `mcp__rea__<tool>`
(`mcp__rea__open_binary`, `mcp__rea__analyze_function`,
`mcp__rea__analyze_javascript_application`, `mcp__rea__inspect_android_package`,
...). Their schemas total roughly 460 KB, so Hermes keeps them behind its
on-demand tool search instead of the model's visible tool list: a session pays
only for the tools it looks up. REA's six MCP prompts are available through
the server's prompt tools.

## What runs on your machine

Most REA tools read files you name and never execute the target. A few run or
change things, and Hermes does not prompt before them:

- `capture_process_scenario` runs a command you or the agent choose under a
  PTY and records what it does.
- `capture_browser_scenario`, `capture_electron_scenario` and
  `capture_native_ui_scenario` launch or drive an application window.
- `install_adb_package`, `uninstall_adb_package`, `push_adb_file`,
  `start_adb_app`, `start_adb_activity` and `stop_adb_app` change a connected
  Android device.
- `export_evidence_bundle` and `close_binary` can overwrite a file at a path
  you give them.

The bundled skill tells the agent to name the exact command, app or device and
ask you before any of these, and to skip them in unattended runs. Analyse
untrusted programs in an environment you chose for it. REA sends no
telemetry. Its tools reach the network only for the target you point them at:
a debugging endpoint, a device, or a web page whose source maps a browser
capture fetches.

## Version pinning

`mcp.json` pins `rea-agents@6.4.0` (published 2026-10-11) and the skill is
REA's `reverse-engineer-anything` skill from the same release tag. Bumping
either is a commit to this repo followed by a reviewed SHA bump in the
[hermes-agent plugin catalog](https://github.com/NousResearch/hermes-agent/tree/main/plugin-catalog).
`@latest` is deliberately not used.

## Attribution

REA is made by [morluto](https://github.com/morluto) and released under the MIT
license. The skill in `skills/reverse-engineer-anything/` is adapted from REA's
bundled skill; its license is reproduced in [LICENSE](LICENSE). The workflow in
`skills/crash-triage/` is adapted from
[crashlens](https://github.com/mingleiw/crashlens) by mingleiw (MIT).
