<p align="center"><img src="../assets/camofox.jpg" alt="Camofox: official Hermes Agent plugin" width="100%"></p>

# Camofox for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly built into Hermes core (`tools/browser_camofox.py`).

[Camofox](https://github.com/jo-inc/camofox-browser) is a self-hosted Node.js server wrapping
Camoufox, a Firefox fork with C++ fingerprint spoofing. This plugin drives it over its REST API, so
Hermes browses with a local anti-detection browser and no cloud dependency.

It registers the browser tools skills already call, in the `camofox` toolset:
`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_scroll`,
`browser_back`, `browser_press`, `browser_get_images`, `browser_vision` and `browser_console`, plus
`browser_import_cookies` when a Camofox API key is set.

**If you already used Camofox with Hermes, you don't need to change anything:** `CAMOFOX_URL`, the
other `CAMOFOX_*` variables and every `browser.camofox.*` config key work as before, and with
`managed_persistence: true` the plugin sends the same profile-scoped Camofox `userId` core sent, so
your saved logins carry over.

## Install

```bash
hermes plugins install camofox                                      # from the plugin catalog
hermes plugins install NousResearch/hermes-official-plugins#camofox  # or straight from this repo
```

The installer asks for `CAMOFOX_URL`. Hermes never installs or starts the Camofox server; run it
yourself (next section), then check it:

```bash
hermes camofox setup     # save CAMOFOX_URL (and an optional CAMOFOX_API_KEY) to this profile's .env
hermes camofox status    # configured URL, identity mode, reachable or not, VNC address
```

The tools appear once `CAMOFOX_URL` is set and the `camofox` toolset is enabled
(`hermes tools`). On a Hermes release that still has Camofox built in, the plugin stays inactive
(one log line, no tools) and the built-in backend keeps working.

## Run the Camofox server

Camofox's own quick start is native Node, no Docker; that path also works on Windows:

```bash
git clone https://github.com/jo-inc/camofox-browser
cd camofox-browser
npm install && npm start
# -> http://localhost:9377
```

The first run downloads Camoufox (hundreds of MB). `GET http://localhost:9377/health` should report
`ok: true`. Shorthand: `npx @askjo/camofox-browser`. To use Hermes-managed Node instead of a system
install, put `~/.hermes/node` on `PATH` (Windows default profile: `%LOCALAPPDATA%\hermes\node`).

**Headed vs headless (native installs).** The lever is `interactive.mode` in `camofox.config.json`
(or the `CAMOFOX_INTERACTIVE` env var): `off` (default, headless), `desktop` (a visible Camoufox
window for manual logins), `novnc`, or `auto`. Restart the Camofox server after changing it.

### Docker

```bash
docker run -d -p 9377:9377 -e CAMOFOX_PORT=9377 ghcr.io/jo-inc/camofox-browser:latest
```

Docker's headed path is VNC (`ENABLE_VNC=1`); builds with VNC, a persistent profile directory or a
larger Node heap are described in the [camofox-browser README](https://github.com/jo-inc/camofox-browser).

Then point Hermes at the server:

```bash
# ~/.hermes/.env
CAMOFOX_URL=http://localhost:9377
```

### Docker: opening pages served from the host

`CAMOFOX_URL` points at the host-published control API, but page URLs are opened by the browser
inside the container, where `127.0.0.1` is the container. Rewrite loopback page URLs to the host:

```yaml
# ~/.hermes/config.yaml
browser:
  camofox:
    rewrite_loopback_urls: true
    loopback_host_alias: host.docker.internal  # default; use a LAN IP if needed
```

(or `CAMOFOX_REWRITE_LOOPBACK_URLS=true`, `CAMOFOX_LOOPBACK_HOST_ALIAS=...`). Only `localhost`,
`127.0.0.1` and `::1` page URLs are rewritten, `CAMOFOX_URL` itself is unchanged, and the tool result
tells the model about the rewrite. Leave it off for native installs.

## Persistent browser sessions

By default each session gets a random Camofox identity, so cookies and logins don't survive a
restart. To keep them:

```yaml
browser:
  camofox:
    managed_persistence: true
```

Hermes then sends a deterministic `userId` derived from the profile's `browser_auth/camofox/` path,
so each Hermes profile gets its own Camofox browser profile, and it skips the server-side session
delete at the end of a session. The Camofox server must map that `userId` to a persistent profile.

Ending a Hermes session does **not** close the Camofox session: Hermes only drops its local entry and
the browser context keeps running, with live cookies held in that open context, not yet on disk.
Camofox writes them to disk at a checkpoint: `POST /stop` (needs `CAMOFOX_ADMIN_KEY`, sent as
`x-admin-key`) or a graceful Node shutdown (`SIGINT` / `SIGTERM`). Killing Node (`kill -9`,
`taskkill /F`, Task Manager) skips the shutdown hook and drops logins that were never checkpointed.

Headed login, then headless reuse:

1. Start Camofox with `interactive.mode` (or `CAMOFOX_INTERACTIVE`) set to `desktop`.
2. Start Hermes, open a login site in a browser task, and sign in in the visible window.
3. Checkpoint without killing Node:
   ```bash
   curl -X POST http://localhost:9377/stop -H "x-admin-key: $CAMOFOX_ADMIN_KEY"
   ```
   State lands under `~/.camofox/profiles/` on the server. `POST /stop` answers 403 when
   `CAMOFOX_ADMIN_KEY` is unset: shut Node down gracefully instead, then start Camofox again.
4. Set `interactive.mode` to `off`, restart Camofox, and start a new Hermes browser task.

## Externally managed sessions

When another app drives the visible Camofox browser, Hermes can work inside that same identity:

| Setting | Env var | Effect |
|---------|---------|--------|
| `browser.camofox.user_id` | `CAMOFOX_USER_ID` | Camofox `userId` for Hermes' tabs; setting it opts into externally managed mode (no server-side delete at session end). |
| `browser.camofox.session_key` | `CAMOFOX_SESSION_KEY` | `sessionKey`/`listItemId` sent on tab creation and matched during adoption. Defaults to a per-task value. |
| `browser.camofox.adopt_existing_tab` | `CAMOFOX_ADOPT_EXISTING_TAB` | Reuse an open tab of that user (`GET /tabs?userId=...`) before creating one. |

Env vars take precedence over `config.yaml`.

Adoption only reuses a tab whose `listItemId` equals the session key. With no such tab, the next
`browser_navigate` opens a new tab under the session key in the same Camofox user (same cookies and
profile). To make Hermes attach to the tab another app opened, set `session_key` to the key that app
used when it created the tab.

## Cookie import

`browser_import_cookies` loads a Netscape-format `cookies.txt` (exported with a "cookies.txt"
browser extension) into the current Camofox user, so the agent can reach sites you are signed in to
without an interactive login. It is offered only when `CAMOFOX_API_KEY` is set, and the Camofox
server must run with the same key (`-e CAMOFOX_API_KEY=...`):

```bash
openssl rand -hex 32          # put the value in ~/.hermes/.env as CAMOFOX_API_KEY and on the server
mkdir -p ~/.camofox/cookies
cp ~/Downloads/linkedin_cookies.txt ~/.camofox/cookies/linkedin.txt
```

Then ask: *"Import my LinkedIn cookies from linkedin.txt, then open my feed."* Files must sit under
`browser.camofox.cookies_dir` (default `~/.camofox/cookies`); absolute paths and `../` are refused,
files are capped at 5 MB and 500 cookies, `domain_suffix` filters to one site, and cookie values are
never logged. Imported cookies belong to the session's Camofox user: with
`browser.camofox.managed_persistence: true` later navigations keep that identity; with the default
ephemeral identity they last only while the same Camofox session lives.

## Configuration reference

| Key | Default | Meaning |
|-----|---------|---------|
| `CAMOFOX_URL` (`.env`) | — | Camofox server URL. Required. |
| `CAMOFOX_API_KEY` (`.env`) | — | Sent as `Authorization: Bearer` to a server started with an API key. |
| `browser.command_timeout` | `30` | Per-request timeout in seconds (floor 5; navigation allows 60). |
| `browser.snapshot_threshold` | `15000` | Snapshots longer than this are cut at a line boundary; the full tree is saved under `cache/web` and the cut says how to page it with `read_file`. |
| `browser.restrict_evaluate` | `false` | Refuse `browser_console(expression=...)` that names sensitive primitives (cookies, storage, network calls). `browser.allow_unsafe_evaluate: true` overrides it. |
| `browser.camofox.cookies_dir` | `~/.camofox/cookies` | Where `browser_import_cookies` reads cookie files. |
| `auxiliary.camofox_vision.*` | inherits `auxiliary.vision` | Model used by `browser_vision`. |

Navigation refuses URLs that carry a Hermes secret, cloud-metadata endpoints, and hosts in your
`security.website_blocklist`. Private/LAN addresses stay reachable, as they were with core's Camofox
backend.

## What differs from core's other browser backends

- `browser_snapshot` has no compact/full switch (the Camofox snapshot is a single view).
- `browser_vision` always answers through the auxiliary vision model; it has no numbered-label overlay.
- `browser_console` evaluates JavaScript; the Camofox REST API has no console log capture.

## Core seams this plugin asks for

The plugin uses only documented plugin APIs except for these core helpers, which have no public
plugin seam yet. Each is imported at its point of use:

| Import | Used for |
|--------|----------|
| `tools.url_safety.is_always_blocked_url`, `normalize_url_for_request` | the cloud-metadata floor and URL normalization on navigate |
| `tools.website_policy.check_website_access` | the user's website blocklist |
| `agent.redact.redact_sensitive_text`, `agent.redact._PREFIX_RE` | secret redaction in tool output; refusing URLs that embed a secret |
| `tools.credential_files.to_agent_visible_cache_path` | the stored-snapshot path as the agent's file tools see it (Docker/SSH terminals) |

Vault fill (`browser_vault` filling a login form) reaches the Camofox tab on Hermes cores that have
the browser page backend seam (`ctx.register_browser_page_backend`): the vault reads the tab's origin,
inspects the form and writes the password through Camofox's `/tabs/<id>/evaluate`, with the same
exact-origin checks as on a CDP browser. It only fills a tab `browser_navigate` already opened (it
never opens one), and sends the password only when `CAMOFOX_URL` is loopback or HTTPS. A value the
vault filled shows as `«redacted-vault-secret»` in `browser_snapshot`. On older cores, type
credentials with `browser_type`. Snapshots also mask values that look like secrets (API keys,
tokens), but Camoufox prints every other filled field's value and does not mark which field is a
password: a plain password typed into the page (by the agent, or by you over VNC) shows up in
`browser_snapshot` and the navigate result.

## License

MIT
