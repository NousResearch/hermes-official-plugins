# Prerequisites and connection

Official source: https://www.blender.org/lab/mcp-server/

This wrapper targets upstream revision `2cea8d566dde07fbac28a61d698909d69724e853` (v1.0.3): https://projects.blender.org/lab/blender_mcp

## Components

Hermes launches a stdio MCP server using uv. That process connects to the official Blender add-on over a loopback TCP bridge at 127.0.0.1:9876. The add-on runs inside Blender; installing this Hermes package installs neither Blender nor the add-on.

Required: Blender >=5.1, uv and Git available to the Hermes backend, network access on first MCP startup to fetch the pinned server and its Python dependencies. Upstream uses an isolated uv tool environment; dependencies are resolved by uv and are not fully locked by this spike package.

## Setup

Hermes does most of this itself (see *Getting Blender ready* in the skill):

1. Blender 5.1 or newer. When none is found, Hermes asks once and, on yes, installs the newest official release from download.blender.org into the user's folders (Linux `~/.local/opt`, macOS `~/Applications`, Windows `%LOCALAPPDATA%\Programs\Blender Foundation`), never over an existing Blender, checked against blender.org's sha256 list, together with the add-on. Blender 5.1+ has no build for Intel Macs or Linux on ARM.
2. The official Blender Lab MCP add-on: with the user's consent Hermes installs the pinned v1.0.3 extension ZIP through Blender's own command line (`blender --command extension install-file -r user_default -e mcp-1.0.3.zip`) while Blender is closed. Otherwise the user installs it from the official page: its **Drag and Drop into Blender** button needs two drops into the Blender window (the first adds the Blender Lab repository, the second installs the add-on), or use Install from Disk with the extension ZIP.
3. Online access: the add-on starts its bridge only when Blender has online access. Hermes opens Blender with `--online-mode`, which grants it for that session only and leaves the saved **Allow Online Access** preference (Edit > Preferences > System > Network) alone. A Blender the user opens normally needs that preference on, or the add-on reports "Online access must be enabled in the system preferences" and the bridge does not start.
4. The bridge: **Auto Start** is on by default, so it binds `localhost:9876` a few seconds after Blender opens. Otherwise click **Start MCP Bridge Server** in Edit > Preferences > Add-ons > MCP and look for "Server is running". Keep the localhost/9876 defaults this package expects. Headless (no display): `blender --background --online-mode --command blender_mcp`.
5. Enable this Hermes plugin for the intended profile; its tools appear in sessions started afterwards (or after `/reload-mcp`). They are listed whether or not Blender is open. Check a real `get_objects_summary` response before claiming Blender is connected.

Never install an unrelated PyPI project named blender-mcp: this package deliberately fetches Blender Lab's source by full Git SHA.

## Failures

- `uv` or Git missing: prerequisite missing; do not silently install executables.
- Tool listing works but scene query fails: MCP process is up, but the Blender bridge is unavailable.
- Blender missing or too old: Hermes offers to install the newest release (skill: *Installing Blender*). If the user declines or the download fails, link https://www.blender.org/download/.
- Blender already running but the bridge is down: the user opened it without online access or the add-on is stopped. Never start a second Blender beside it; ask the user to quit it (Hermes then reopens it with `--online-mode`) or to start the bridge.
- `*_for_cli` tool reports `Blender executable not found at 'blender'`: these tools start `blender --background` from `BLENDER_PATH`, else `PATH`, and Hermes does not pass on a `BLENDER_PATH` set in a shell or `.env`. Put a `blender` command on the backend's `PATH`, for example in the directory that holds `uv`. On macOS, use a wrapper script that runs `/Applications/Blender.app/Contents/MacOS/Blender "$@"`. On Windows, add the folder containing `blender.exe` to `PATH` and restart Hermes.
- Port already used: identify the owner; do not kill it or attach to an arbitrary process.
- Remote Hermes backend: localhost means the backend's machine, not the Desktop client's machine. Both Blender and MCP must run on the intended host.
