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

- **Subjects** — topics extracted from session titles, file paths, tool names, slash commands,
  quoted phrases and CamelCase identifiers; each subject page lists related sessions and snippets.
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

The routes capture `get_hermes_home()` from the request context before dispatching work to a
worker. Database and memory-file reads use that captured home. Profile selection requires the
host to supply a scoped context; `?profile=` alone does not establish one in this plugin.
The inspected host SDK does not add management profiles to plugin URLs, and its plugin routes
have no profile-scoping middleware. Dashboard profile-switch integration remains a host gap.

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
npm ci                      # build tools and dev-only rendered UI test dependencies
npm run build               # rebuild the tracked dashboard/dist/index.js
npm run check               # syntax-check the bundle
npm run test:ui             # rebuild and render with React DOM/jsdom (Node 22.13+ or 24+)
HERMES_AGENT_REPO=~/.hermes/hermes-agent \
  ~/.hermes/hermes-agent/.venv/bin/python -m pytest tests/ -o "addopts="
```

`dashboard/dist/index.js` is a plain IIFE; the dashboard caches it aggressively, so after a
rebuild restart the server or navigate away and back. Adding a manifest at runtime needs
`GET /api/dashboard/plugins/rescan`; changing `plugin_api.py` needs a server restart.

## Search reuse and verification

`/subjects` caches complete finalized subjects before query filtering, sorting and truncation.
Keys contain the absolute database path and the original session scope
`max(limit * 5, limit, 100)`. Different queries with that scope reuse extraction and snippets;
search never filters only the unfiltered top results. Extraction, weighting and ordering are
unchanged. Overview (50 subjects), searches (100 by default), details and daily logs retain
their original scopes. Overview, detail and day aggregation remain uncached.

The process-local LRU keeps at most eight entries, with a combined 16 MiB serialized-payload
budget (Python object overhead is additional). Database and WAL device/inode, size, modification
and change timestamps invalidate reuse. Empty WAL and absent WAL are equivalent because a
read-only SQLite attach can create an empty WAL. Expiry after five seconds is a fallback for
coarse filesystem timestamps. Expired entries are removed on the next search. Missing databases
are not cached. Work raced by a metadata change is returned only to that in-flight caller and
is not cached; concurrent writes can affect that response just as in the original aggregation.
No full database hashing or persistent connections are used. Connections close in their worker,
including error paths. Callers receive detached copies.

A single lock coalesces cold search requests and bounds retained work. This also serializes
cold searches across profiles; it is a deliberate simple tradeoff. All database aggregation,
search filtering and memory-file reads run off the event loop. UI tests render the tracked
bundle and exercise immediate debounce status, errors, empty results, stale replies, transport
cancellation, clearing and unmount cleanup through the SDK boundary.

Run the synthetic benchmark from the repository root:

```bash
HERMES_AGENT_REPO=~/.hermes/hermes-agent \
  ~/.hermes/hermes-agent/.venv/bin/python scripts/benchmark_search.py
```

It loads the baseline API/core from `9bc3913` (override with `--baseline`), creates a temporary
600-session / 7,200-message database, and compares exact API outputs for five queries, then
repeats five times. Only synthetic content is used. A representative local run measured
806 ms baseline first query, 849 ms current cold query, and repeated medians of 772 ms versus
32 ms (about 24x faster). **The improvement is warm-cache only; cold work is slightly slower.**
Timing varies by machine and activity; the script asserts output parity and prints its digest.

The Python suite includes a strict expected failure for the existing real-host `?profile=` gap.
The real-host discovery/auth test and overlapping A/B/A ContextVar-scoped router tests are
separate passing checks. Do not interpret those scoped tests as proof of dashboard profile
selection support.

React and jsdom are dev-only; production still uses host SDK React. If registry access is
unavailable but the host has these dependencies installed, the rendered tests can use them
read-only without changing the host:

```bash
NODE_PATH="$HERMES_AGENT_REPO/node_modules" npm run test:ui
```

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
