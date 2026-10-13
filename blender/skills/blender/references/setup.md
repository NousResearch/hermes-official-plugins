# Prerequisites and connection

Official source: https://www.blender.org/lab/mcp-server/

This wrapper targets upstream revision `2cea8d566dde07fbac28a61d698909d69724e853` (v1.0.3): https://projects.blender.org/lab/blender_mcp

## Components

Hermes launches a stdio MCP server using uv. That process connects to the official Blender add-on over a loopback TCP bridge at 127.0.0.1:9876. The add-on runs inside Blender; installing this Hermes package does not install the add-on.

Required: Blender >=5.1, uv and Git available to the Hermes backend, network access on first MCP startup to fetch the pinned server and its Python dependencies. Upstream uses an isolated uv tool environment; dependencies are resolved by uv and are not fully locked by this spike package.

## User setup

1. Install the official Blender Lab MCP add-on using the instructions on the official page. Its **Drag and Drop into Blender** button needs two drops into the Blender window: the first adds the Blender Lab repository, the second installs the add-on. Alternatively use Blender's Install from Disk action with the official extension ZIP (the page's "download" link).
2. Enable the MCP extension and Blender's **Allow Online Access** preference (Edit > Preferences > System > Network) after reviewing the page's security warning. Without it the add-on reports "Online access must be enabled in the system preferences" and the bridge does not start.
3. Start the bridge from the add-on preferences (Edit > Preferences > Add-ons > MCP): **Auto Start** is on by default; otherwise click **Start MCP Bridge Server** and look for "Server is running". Keep it bound to localhost with port 9876, the defaults this package expects.
4. Enable this Hermes plugin for the intended profile and refresh/restart as required by your Hermes release. Check a real `get_objects_summary` response before claiming Blender is connected.

Never install an unrelated PyPI project named blender-mcp: this package deliberately fetches Blender Lab's source by full Git SHA.

## Failures

- `uv` or Git missing: prerequisite missing; do not silently install executables.
- Tool listing works but scene query fails: MCP process is up, but the Blender bridge is unavailable.
- Blender missing or too old: install/update is a separate user-approved action.
- `*_for_cli` tool reports `Blender executable not found at 'blender'`: these tools start `blender --background` from `BLENDER_PATH`, else `PATH`, and Hermes does not pass on a `BLENDER_PATH` set in a shell or `.env`. Put a `blender` command on the backend's `PATH`, for example in the directory that holds `uv`. On macOS, use a wrapper script that runs `/Applications/Blender.app/Contents/MacOS/Blender "$@"`. On Windows, add the folder containing `blender.exe` to `PATH` and restart Hermes.
- Port already used: identify the owner; do not kill it or attach to an arbitrary process.
- Remote Hermes backend: localhost means the backend's machine, not the Desktop client's machine. Both Blender and MCP must run on the intended host.
