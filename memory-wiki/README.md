<p align="center"><img src="../assets/memory-wiki.jpg" alt="Memory Wiki: official Hermes Agent plugin" width="100%"></p>

# hermes-memory-wiki

> Part of [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly [`NousResearch/hermes-memory-wiki`](https://github.com/NousResearch/hermes-memory-wiki), which is archived; this directory carries its full history.

Memory Wiki dashboard plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
a browsable **session-history wiki** (subject pages and daily logs derived deterministically from
your local `state.db`) plus a read-only **Persistent Memory audit panel** showing the `MEMORY.md` /
`USER.md` entries the agent actually carries into every session.

No LLM calls, no writes: the plugin opens `state.db` read-only through the public
`hermes_state.SessionDB` API and only reads memory files. Editing memory stays with the `memory`
tool (prompt-cache safe).

## What you get

A **Memory Wiki** tab in `hermes dashboard` (after *Sessions*) with:

- **Subjects** — topics derived primarily from every user message, with session titles as
  secondary evidence and a fallback when user content is absent. Plain-language phrases,
  explicitly discussed tool identifiers, paths, packages and commands are supported. Assistant
  text can supply matching context only; it cannot introduce or score topics. Tool, system,
  developer and tool-call metadata are excluded, as are unverified session previews.
  Subject snippets prefer user text; daily subjects and work items follow the same policy.
- **Daily logs** — per-day activity with sessions, subjects and "what we did" work items.
- **Recent activity** — the ten most recently active sessions.
- **Persistent Memory** — agent notes and user profile entries, rendered exactly as the memory tool
  parses them, so you can audit what the agent remembers.

Backend routes (mounted at `/api/plugins/memory-wiki/`, behind the dashboard's normal auth gate):

| Route | Returns |
|---|---|
| `GET /overview?subject_limit=&day_limit=` | subjects, daily logs, recent sessions, notes |
| `GET /subjects?q=&limit=` | subject list, optional substring filter |
| `GET /subjects/{slug}` | one subject (404 when unknown) |
| `GET /days?limit=` | daily logs |
| `GET /days/{date}` | one `YYYY-MM-DD` log (404 when unknown) |

Profile scoping is handled by the host (`?profile=<name>` on any REST call); the routes resolve
`HERMES_HOME` per request.

## Install

From the Nous plugin catalog (pinned, reviewed commit):

```bash
hermes plugins install hermes-memory-wiki --enable
```

Or directly from this repository:

```bash
hermes plugins install hermes-memory-wiki --enable
```

Then start (or restart) the dashboard — `hermes dashboard` — and open the **Memory Wiki** tab. The
plugin must be in `plugins.enabled` (the `--enable` flag or `hermes plugins enable memory-wiki`
does that); the web server never imports backend code from plugins you have not enabled.

Uninstall: `hermes plugins remove memory-wiki`.

Requires Hermes Agent ≥ 0.21 (dashboard plugin SDK with `fetchJSON`).

## Layout

```
hermes-memory-wiki/
├── plugin.yaml                  # plugin manifest (name: memory-wiki)
├── __init__.py                  # register(ctx) — no tools/hooks; dashboard-only plugin
├── dashboard/
│   ├── manifest.json            # tab declaration (/memory-wiki, icon Database)
│   ├── plugin_api.py            # FastAPI router, mounted at /api/plugins/memory-wiki/
│   ├── memory_wiki_core.py      # stdlib-only aggregation over SessionDB rows
│   ├── src/index.jsx            # UI source (JSX, uses window.__HERMES_PLUGIN_SDK__)
│   └── dist/index.js            # committed build artefact the dashboard loads
├── tests/                       # pytest (needs a hermes-agent checkout, see conftest.py)
└── package.json                 # `npm run build` → dashboard/dist/index.js (esbuild)
```

## Development

```bash
npm install                 # esbuild only; React comes from the host SDK at runtime
npm run build               # rebuild dashboard/dist/index.js — commit the result
HERMES_AGENT_REPO=~/.hermes/hermes-agent \
  ~/.hermes/hermes-agent/.venv/bin/python -m pytest tests/ -o "addopts="
```

`dashboard/dist/index.js` is a plain IIFE; the dashboard caches it aggressively, so after a
rebuild restart the server or navigate away and back. Adding a manifest at runtime needs
`GET /api/dashboard/plugins/rescan`; changing `plugin_api.py` needs a server restart.

## Credits

- **Abdullah Raja ([@Araja119](https://github.com/Araja119))** — original author of the Memory Wiki
  (aggregation engine, API and dashboard page) in hermes-agent PR
  [#31244](https://github.com/NousResearch/hermes-agent/pull/31244). Authorship is preserved via
  `Co-authored-by` trailers on the commits that carry that code.
- **Teknium / Nous Research** — salvage and hardening in
  [#89940](https://github.com/NousResearch/hermes-agent/pull/89940) (Persistent Memory audit panel),
  and the conversion into this standalone plugin.
- The "show curated memory in the dashboard" idea was independently proposed earlier by
  [@scotty87](https://github.com/scotty87) in hermes-agent #33043.

## License

MIT — see [LICENSE](LICENSE).
