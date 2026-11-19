# Claude Subscription DirectSDK (Experimental) — Hermes plugin

> Part of [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly [`NousResearch/hermes-plugin-claude-subscription-directsdk`](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk), which is archived; this directory carries its full history.

![Claude Subscription DirectSDK — a Hermes model-provider plugin: Hermes → claude CLI → stream-json, on your Claude Pro/Max subscription](assets/hero-technohellenic.png)

Standalone Hermes Agent model-provider plugin: `claude-subscription-directsdk-experimental`, displayed as **Claude Subscription DirectSDK (Experimental)**. It drives the unmodified official Claude Code executable as the model client for your Claude Pro/Max subscription, one kept-alive process per Hermes agent. Hermes keeps its normal agent loop, tools, approvals and compaction. Despite the name, the implementation speaks native stream-json directly and does not require the Python Agent SDK package.

## Requirements

- Hermes Agent 0.21.4 or newer, or `main` at/after `118984d7a02f` (hermes-agent PR #117451, the core half of #105863). On an older Hermes the plugin fails to load with a clear `Failed to load user provider plugin` warning rather than half-working.
- Python 3.10+ on Linux, macOS or Windows. Native runs in its own process group (POSIX session / Windows `CREATE_NEW_PROCESS_GROUP`) and cancellation kills the whole tree (`killpg` / `taskkill /T`), so the `node` child behind the npm `claude.cmd` shim never outlives a cancelled request. The suite runs on all three in CI.
- **The official Claude Code CLI, installed and logged in.** This plugin has no credentials of its own; everything goes through `claude`. Fable 5.2 (the `fable` alias) needs Claude Code **2.1.321** or newer, Haiku 5.5 (the `haiku` alias) needs **2.1.293** or newer, Sonnet 5.5 (the `sonnet` alias) needs **2.1.284** or newer and Opus 5.5 (the `opus` alias) needs **2.1.280** or newer; Mythos 5.1 is qualified on **2.1.316** (Anthropic documents no minimum); Mythos 5.1 and 5.2 are available only to organizations verified through Anthropic's verification programs ([Mythos 5.1](https://platform.claude.com/docs/en/models/mythos-5-1/overview), [Mythos 5.2](https://platform.claude.com/docs/en/models/mythos-5-2/overview)). An older CLI answers `400 Claude Code … does not support this model`, so run `claude update` first ([model configuration](https://code.claude.com/docs/en/model-config#model-aliases)).

The plugin checks for `claude` at every seam and never guesses:

| When | What happens without `claude` |
| --- | --- |
| plugin load | one warning in the log; the provider stays registered so setup can explain what is missing |
| `hermes model` → this provider | the flow stops with the install hint and writes nothing to config |
| `/model`, Desktop/TUI picker | the pinned catalog is listed (no live account picker) |
| a request | fails immediately with the install hint instead of a `Popen` traceback |

Install Claude Code with `npm install -g @anthropic-ai/claude-code`. `claude` is looked up on `PATH`, then in the usual install directories (`~/.local/bin`, `~/.claude/local`, `~/bin`, `~/.npm-global/bin`, `~/.bun/bin`, `~/.volta/bin`, `/opt/homebrew/bin`, `/usr/local/bin`), so a backend started by a LaunchAgent or Hermes Desktop with a minimal `PATH` still finds it. Point `CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND` at the binary if it lives anywhere else. The `plugin.yaml` declares the same requirement under `external_dependencies`.

## Install

```sh
hermes plugins install claude-subscription-directsdk      # from the Hermes plugin catalog (pinned, reviewed)
# or straight from GitHub (tracks main):
hermes plugins install NousResearch/hermes-official-plugins#claude-subscription-directsdk

claude auth login
hermes model          # pick "Claude Subscription DirectSDK (Experimental)"
```

Per-profile installs go under `~/.hermes/profiles/<profile>/plugins/` instead. The plugin declares `kind: model-provider`, so Hermes' provider discovery imports it directly; no `plugins.enabled` entry is needed.

## Status

**Single-request admission is implemented and live-qualified.** Native 2.1.263 can attempt extra generations despite `--max-turns 1` and disabled HTTP retries. A request-scoped loopback relay now forwards only the first Messages request and rejects subsequent attempts locally. Hermes receives the first completed upstream response, its actual usage and native stop reason; blocked native recovery does not turn a completed response into an exception. Truncated/incomplete streams and upstream HTTP errors remain failures. One exception: when an upstream response is itself rejected with a 401 authentication error (a revoked or rotated OAuth login), the relay admits native's follow-up request so the CLI can refresh the shared login and retry with a fresh token; a 401 on the retried attempt closes the gate again (#162).

The integrated subscription-backed review completed seven Hermes calls with exactly seven upstream requests, twelve Hermes tool executions and twelve durable tool results. Context grew from 138,498 to 165,697 tokens; follow-up cache reads averaged 97.13%. Native accounting reported $0.8793728 in list-price equivalent, not a verified subscription charge. An earlier attempt began at 187K but exceeded the current native route's 200K bound after tools; that incomplete attempt is not counted as a successful review.

A real subscription-backed Hermes task built and tested a CSV auditor. Separate live qualifications exercised streaming tool rounds, restart/resume, host-side denial, authentic steering, cancellation during generation, and a real CLI subagent completion. This is a review build, not a full-parity or production-readiness claim. See the remaining limitations below.

Native Claude Code **2.1.263** is qualified. Replay acknowledgments and extra-body behavior are version-sensitive interfaces, not a public arbitrary-history SDK guarantee. The generic host support (external-process providers, replay carriers, setup gating) lives in Hermes core; this repository is only the provider.

## Login and select

```sh
claude auth login
hermes --provider claude-subscription-directsdk-experimental -m sonnet
```

Authentication belongs to the official CLI. The plugin never opens, copies, refreshes, or prints its credential files. The one thing it touches in the login directory is an empty `.oauth_refresh.lock` older than Claude Code's own 60 s stale window, which a Claude Code that exited mid-refresh leaves behind and never reclaims (anthropics/claude-code#95236); it is removed before each spawn. No Hermes API key is required or sent by the plugin. Anthropic API credentials (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_FOUNDRY_API_KEY`) inherited from Hermes' environment are dropped from every Claude Code child, so a key Hermes holds for its own `anthropic` provider keeps working there while this provider stays on the subscription. The normal Hermes client path rejects inherited custom Anthropic endpoint and cloud-backend overrides before spawning; the error names conflicting environment variables without printing their values. Remove those overrides from the launching environment when selecting OAuth. There is no silent HTTP/API-key fallback in this client.

Subscription entitlement and extra-usage settings still belong to the account and native service. Disable extra usage in the account if you do not want overage billing. A native list-price cost estimate is not proof of a subscription charge.

For a separately CLI-managed auth directory:

```sh
CLAUDE_CONFIG_DIR=/path/to/official-cli-config claude auth login
export CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR=/path/to/official-cli-config
```

An inherited `CLAUDE_CONFIG_DIR` also works; the per-profile `claude_config_dir` setting takes precedence over both environment directories. To select an executable outside PATH, set `CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND` to its absolute path. There is no unrestricted public CLI-flags setting; isolation and denial flags are plugin-owned. The low-level Python `Client(env=...)` injection is available for explicitly controlled local fixtures and does not apply the inherited-environment guard. It is not the normal Hermes provider path or an OAuth certification mechanism.

A request without tools mounts no MCP server at all, and no request starts a helper process: Hermes' tools are served
from inside the plugin (#42).

**Supervised gateways (launchd, systemd, containers, cron):** Claude Code's local login check reads the process identity — without `USER` in its environment a logged-in CLI reports `loggedIn: false`, instantly and with no upstream request. The plugin derives `USER`/`LOGNAME` (`USERNAME` on Windows) from the parent process or the running uid before every spawn, so a filtered gateway environment no longer surfaces as a false `ClaudeCodeLoggedOut`. When a logged-out error does fire, it now carries native's first stderr lines, which makes a gateway-only failure diagnosable instead of silent (#44).

Persistent configuration:

```yaml
model:
  provider: claude-subscription-directsdk-experimental
  default: sonnet
```

Auxiliary/fallback routing remains owned by Hermes. Configure those routes explicitly if they must also use the subscription; this provider does not silently change other selected providers.

### Proxies

The relay opens the upstream connection itself, so it applies native's proxy variables from the Hermes process environment: `https_proxy`/`HTTPS_PROXY`, then `http_proxy`/`HTTP_PROXY`, with `NO_PROXY` exceptions (standard-library matching; comma- or space-separated). The proxy must be an `http://` CONNECT proxy; `user:pass@` in its URL becomes a `Proxy-Authorization` header. TLS still terminates at `api.anthropic.com`, so the subscription bearer never reaches the proxy. A SOCKS or TLS-to-proxy URL is ignored with a warning and the relay connects directly, as it did before proxy support. For a proxy that must never be bypassed, use the `upstream_proxy` setting below, which fails closed.

## Settings

**Claude login config directory** (`claude_config_dir`, string, empty by default). Select a separately logged-in Claude config directory for the active Hermes profile, including profiles served by one multiplexed gateway:

```sh
hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.claude_config_dir <dir>
```

Read on every native spawn, including auth-status and model-discovery probes. Precedence: nonempty per-profile setting after trimming whitespace → nonempty `CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR` → inherited `CLAUDE_CONFIG_DIR` → Claude CLI default. The selected value gets `~` expansion only; the plugin does not check whether the directory exists. An empty or whitespace-only setting falls through. A missing login produces the usual native authentication error. The process environment is never changed.

Claude Code prefers `CLAUDE_CODE_OAUTH_TOKEN` over the login in its config directory, so the token decides first. The active profile's own token (its `.env`, which a multiplexed gateway does not load into the process environment) is passed to the child. A nonempty `claude_config_dir` setting removes any token inherited from Hermes' process environment so the selected directory's login is used. A profile served by a process that was launched for another profile (a multiplexed gateway, the Desktop backend) never inherits that process's `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_CUSTOM_HEADERS` or `CLAUDE_CONFIG_DIR`: it uses the values in its own `.env`. A served profile with neither its own `CLAUDE_CODE_OAUTH_TOKEN` nor its own config directory (the `claude_config_dir` setting, or `CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR` in its `.env`) is refused with a login error that names both: Claude Code would otherwise fall back to `~/.claude` under the inherited home, which is the launch profile's login and allowance. If the profile's secrets cannot be read at all, the request fails instead of starting Claude Code on another profile's login. On a single-profile install the profile's token is the inherited one, so nothing changes there.

**Plan limits (`/usage`)** are asked of Claude Code itself (its `get_usage` control request), so they follow this per-profile setting, work with `auth.adopt_external_logins: false`, and the plugin never reads or sends the Claude login. Besides the session, weekly and per-model limits, `/usage` lists extra usage (on or off, used and monthly limit), usage credits spent, and this week's share per surface (Claude Code, chats, Cowork). The probe sends no Messages request, and it leaves `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` unset (Claude Code skips the fetch under it) unless you exported it yourself, in which case `/usage` says why the limits are missing.

**Claude Code telemetry and error reporting** (`claude_code_telemetry`, off by default). Off, the plugin starts Claude Code with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_TELEMETRY` and `DISABLE_ERROR_REPORTING` set (#331). Two zero-inference probes are the exception, because those flags also switch off a fetch they exist for: the model picker's `initialize` handshake runs without `DISABLE_TELEMETRY` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, since either one stops Claude Code's feature-flag fetch and shrinks its picker from every model the account runs to each family's current one (on 2.1.319, 13 rows become 5, #86); and the `/usage` probe runs without `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`. A flag you export yourself is never lifted. Turn the setting on to let Claude Code send its telemetry and error reports on every spawn; that adds about 0.25 s to each spawn (measured on Linux and Windows). Change it in the Desktop app under **Capabilities → Plugins**: the settings gear on the `claude-subscription-directsdk-experimental` row opens the switch. Or from the CLI:

```sh
hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.claude_code_telemetry true
```

It is read for the active profile on every spawn, so it applies to the next request without a restart. Whatever the setting, the plugin never lets the CLI auto-update itself, use its feedback upload path or send its own session-title request (`DISABLE_AUTOUPDATER`, `DISABLE_FEEDBACK_COMMAND`, `CLAUDE_CODE_DISABLE_TERMINAL_TITLE`), and an opt-out you export yourself (`DISABLE_TELEMETRY`, `DO_NOT_TRACK`, ...) is passed through untouched.

**Claude Code calls at once** (`max_concurrent`, default 4; #329). At most this many Claude Code turn processes run at the same time in one Hermes process, whatever started them: the main agent, parallel subagents (`delegate_task` runs up to 10 at once), auxiliary calls such as titles and compression, and every profile a multiplexed gateway serves. Each process uses about 250 MB, and all of them draw on one subscription, whose rate limits a burst can trip together (the plugin turns Claude Code's own retries off so Hermes owns retry policy). A call past the limit waits for a free place in arrival order, within its own request timeout; it is not failed and not sent to a fallback provider. Interrupting a waiting call ends it at once without starting Claude Code. A call that waits longer than 2 s writes a warning to the log (`hermes logs`) naming the limit; a model provider has no way to put a line on Hermes' status bar, so the CLI shows the usual spinner meanwhile. A place is freed when its process exits, so a crashed or killed process never holds one; a kept process (see [Transport](#transport)) gives its place back at the end of each step. `/usage`, the model picker and the login check do not count: they send no model request and finish within seconds. The setting is read per call from the profile that makes it; with profiles that set different limits, each call waits until fewer than its own limit are running.

```sh
hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.max_concurrent 6
```

**Extra usage** (`extra_usage`, `allow` | `notify` | `ask` | `stop`, default `notify`). What a request does once Claude Code reports that this account has passed its plan limit and is drawing paid extra usage (#43). Claude Code sends that signal with every request (`rate_limit_event`: `isUsingOverage`, `resetsAt`); the plugin keeps it in memory per process, per Claude login, and writes no files.

- `allow`: nothing changes.
- `notify`: requests continue; one warning per limit window goes to the log (`hermes logs`).
- `ask`: requests are refused before Claude Code starts, with a message that names the reset time and the command that allows extra usage until then: `hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.extra_usage_allowed_until <reset time>`.
- `stop`: requests are refused until the plan limit resets; the message names the reset time and how to switch to `ask` or `allow`.

A refusal is a plan-limit stop: Hermes shows the message and does not switch to a fallback provider. The request that crosses the limit is already billed when Claude Code reports it; the setting acts on the next one. In `ask` and `stop`, a process that has not yet seen a request for the login asks Claude Code once (`get_usage`, no Messages request, about 0.5 to 0.9 s) and counts the account as spilling when extra usage is on and the session or weekly limit is at 100%. That check runs on the turn's own login and route. If it cannot tell (Claude Code gives no usage answer, or `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is exported so it skips the fetch), the request is refused with the reason, since these modes exist to prevent spend you did not approve; the next request asks again. An `ask` window you already allowed with `extra_usage_allowed_until` skips the check. An unknown value counts as `stop`. `extra_usage_reserve_percent` (for example `97`, default unset = 100) moves the stop earlier: in `ask` and `stop`, once the session or weekly window reaches that percent and extra usage is on, requests are refused before the one that would first be billed, read from the same `get_usage` check and from each turn's own `rate_limit_event`.

A 400 that says "Third-party apps now draw from your extra usage" is reported the same way (a billing stop whose message points at the plan settings and names the `hermes-agent` caller), instead of Hermes silently answering from a fallback provider (P#296).

```sh
hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.extra_usage stop
```

**Organization gateway** (`upstream_url`, `upstream_proxy`, `upstream_ca_file`, all empty by default; #46). One explicit opt-in per profile for organizations that route model traffic through their own LLM gateway, an egress CONNECT proxy, or a TLS-inspecting proxy:

```sh
S=plugins.entries.claude-subscription-directsdk-experimental.settings
hermes config set $S.upstream_url https://llm-gateway.example.com/anthropic
hermes config set $S.upstream_proxy http://egress.example.com:3128
hermes config set $S.upstream_ca_file /etc/ssl/corp-root.pem
```

- `upstream_url` replaces `https://api.anthropic.com` for every relay the plugin opens: turns, `/usage` and the picker handshake (the picker is then Claude Code's generic list rather than the account's own). It must be `https://` (`http://` only for a loopback host) with no `user:password@`, query or fragment. **The Claude login's bearer token goes to this host**: one warning per profile and route names it in the log.
- `upstream_proxy` is an `http://host:port` CONNECT proxy for that hop; TLS still ends at the upstream host. Its credentials are a secret: put `user:password` in `CLAUDE_SUBSCRIPTION_DIRECTSDK_PROXY_AUTH` in the profile's `.env`. With this setting the `HTTPS_PROXY` variables are not used for the relay.
- `upstream_ca_file` adds a PEM bundle to the system roots; certificate and hostname checks stay on. The warning then says the TLS is inspected.
- Gateway auth headers go in `ANTHROPIC_CUSTOM_HEADERS` in the profile's `.env`; Claude Code sends them and the relay forwards them. A profile served by a multiplexed gateway gets only its own `.env` values.
- Fail closed: a setting that is set but invalid (a non-HTTPS URL to a non-loopback host, credentials in a URL, a SOCKS proxy, an unreadable CA file, a value that is not a string such as `upstream_proxy: false`) refuses the turn, `/usage` and setup with an error naming the setting. It never falls back to the direct route.
- An inherited `ANTHROPIC_BASE_URL` stays refused with or without these settings; no environment variable selects the upstream.
- What goes to the gateway: Claude Code sends only Messages requests (`POST /v1/messages`) to its base URL, and the relay forwards that path and nothing else. Its own account calls go to `api.anthropic.com` whatever the base URL: `/usage` (`/api/oauth/usage`), the picker handshake and every spawn's bootstrap, MCP directory and, with `claude_code_telemetry` on, feature-flag and event logging (measured on 2.1.319 and 2.1.292). Those carry the Claude login to Anthropic itself, never to another host. With `upstream_proxy` or `upstream_ca_file` set, Claude Code is started with that proxy (`HTTPS_PROXY`, credentials included) and that CA (`NODE_EXTRA_CA_CERTS`), so these calls leave through the same egress; the loopback relay stays in `NO_PROXY`. A network that allows no direct route to `api.anthropic.com` therefore needs `upstream_proxy`; with only `upstream_url` set, `/usage` and those calls go out directly.

**Session mode** (see [Transport](#transport)). These are read from the serving profile's config on every call:

| Setting | Default | What it does |
|---|---|---|
| `session_mode` | `true` | Keep one Claude Code process per agent between model calls. `false`: a new process for every call, as before 0.4.0. |
| `session_min_rounds` | `8` | A conversation starts a kept process only once it has had this many tool rounds (sets of tool results); before that every call runs per-call. Short chats and most subagents then hold none. A process kept already meets every later step, and ends if the history no longer fits it (a compaction). `0`: keep from the first step. |
| `max_sessions` | `4` | At most this many kept processes in one Hermes process. At the cap, the one that has waited longest for its user gives its place to the step asking now; if every one is serving a step or holds a parked tool call, that step runs per-call. |
| `session_idle_seconds` | `3600` | A kept process that waits this long for its user ends. A parked tool call does not count as waiting: the process stays while Hermes runs the tool, and ends at the next step if that step cannot continue it, or when its client closes. The default is the prompt cache's lifetime. |
| `session_unlisted_versions` | `honor` | On a Claude Code release the tested list does not name: `honor` runs every call per-call; `self-test` keeps processes on it once the plugin's own offline test of that binary has passed; `force` keeps processes on it, with one warning. See below. |

What it costs: memory between calls. During a call both modes hold one Claude Code process, about 200 to 250 MB. A kept
process holds it between calls too, and it grows with the history it carries: 245 to 290 MB at round 200 on Linux, 284
to 307 MB on macOS, 325 MB the most seen. So `max_sessions` 4 can hold about 1 to 1.3 GB while agents are idle, until
`session_idle_seconds` ends them. A kept process's request goes upstream only as per-call's bytes (one the relay
cannot make so is refused before upstream, and the step replays on a new process), so cost and caching do not change.

Session mode runs only on the Claude Code versions its tests passed on, listed in `plugin.yaml`
(`session_versions` on the `claude` dependency: 2.1.292, the CI pin, plus 2.1.322 to 2.1.328). Any other version
runs every call per-call, and the log says so once (`session mode runs only on Claude Code ...; this is ..., so every
call runs per-call`). An update of Claude Code then costs latency, never correctness. A kept process ends at its
next step when its binary changes or its version leaves the list, and a process that reports another version than the
one the gate allowed is not kept (restart reason `version` for both). The version is the one Claude Code reports in its own
init event (`claude_code_version`), kept per binary until the binary changes, so the first step on a new or updated
binary runs per-call; no extra process is started to ask.

`session_unlisted_versions` decides what happens on a release the list does not name:

- `honor` (the default): every call runs per-call, as above.
- `self-test`: the plugin tests that binary itself before it keeps a process on it. In the background, one test at a
  time on the machine, it runs one Hermes-shaped conversation twice, per-call and kept, against a loopback upstream
  that answers the same way every time: several tool rounds, two calls in one step, error results, a
  whitespace-only and a large result, a `/steer` after three of them, a reply that leads with a whitespace-only
  block, then a compacted history with an empty block and a cache mark and a new turn. Every kept request must equal
  per-call's, byte for byte, on a process kept across the steps, and the binary must report the version it is
  tested as. About 8 s per release on the Linux test box (7.8 to 9.9 s on each listed release), 4 to 5 s on an
  Apple M4 Mac. Until it has a verdict, calls run per-call; no call waits
  for it. Pass: session mode keeps processes on that binary. Fail (twice: one failing run is tried again), a timeout
  or a test that cannot run: per-call, with the reason in the log. The verdict belongs to that binary's files (an
  update in place, another install or a wrapper is tested anew), the plugin version and the platform, and is kept
  under Hermes' home (`cache/claude-subscription-directsdk-experimental/session-selftest.json`); delete the file to
  test again.
- `force`: keep processes on any release, with one warning in the log.

How the test is kept away from your login and the network: it runs in a child Python process with a throwaway home,
Claude config directory and Hermes home, a placeholder API key, and every plugin setting at its default (no profile
or managed Hermes setting reaches it). On Linux, where unprivileged user namespaces are allowed, it runs in network
and pid namespaces of its own: loopback is its only network, and everything in it ends with it. Elsewhere (macOS,
Windows, a Linux that refuses namespaces) Claude Code's traffic is pointed at loopback by its settings and proxy
variables only; a binary that ignored them could reach the network, holding no login. It is bounded in time (240 s)
and memory, it ends when Hermes does, and it ends with everything it started (with namespaces; elsewhere, everything
it saw start).

A pass shows those shapes, on this machine, for this binary; it does not stand in for the list, which names the
releases the whole session suite passed on. On a release not on the list (`self-test` or `force`) each kept
process's first request is also held to per-call's own build, as its first resumed one always is, and every kept
step is still checked before upstream, so a release that frames a request differently costs a restart into
per-call, not different bytes. Each of these says so once in the log (`hermes logs`), never per call: the release
and the mode, a self-test's pass or its failure with the reason, a value of the setting that is none of the three
(taken as `honor`).

```sh
hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.session_mode false
hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.session_idle_seconds 600
hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.session_unlisted_versions self-test
```

## Transport

Hermes keeps one model client per agent, and subagents get their own. With `session_mode` on (the default) that client
keeps one Claude Code process alive between model calls:

- Hermes' tools are an SDK MCP server that runs inside the plugin and talks to Claude Code over its control protocol.
  When the model calls a tool, Claude Code asks that server, and the request waits until Hermes has run the tool and
  sends the next model call. Claude Code never runs a tool itself.
- The next upstream request then leaves in about 0.03 s instead of the 0.5 to 1.0 s a fresh process needs to start and
  replay the history (offline, Linux; more with longer histories). A live tool round is about 1.7 to 2.3 s, so this is
  roughly a quarter to a half of each round.
- What goes upstream is meant to be byte for byte what a fresh process would send for the same history, so prompt
  caching and cost are the same either way; where the relay cannot make it so, it refuses the request before it
  reaches Anthropic, and the step is replayed into a new process. A new process's first request is the replay itself
  and goes up as built. On later requests the relay rewrites each frame of tool results to Hermes' own bytes (key
  order included, a steer after them too), leaves earlier user turns of text as the process holds them, requires
  Claude Code's record of each reply to be Hermes' frame byte for byte, and refuses any request whose rows no longer
  line up with Hermes' history, whose newest frame it cannot restore to Hermes', or that does not extend the previous
  one. A kept process's first resumed request is the first with
  rows it built itself past its launch's replay, so no earlier request vouches for them: it goes upstream only if it
  equals the request a per-call step builds for the same history. A one-shot builds that request into a relay that
  sends nothing upstream, so each kept process pays one per-call launch once, at that step (two Claude Code processes
  for a moment, on that step's one `max_concurrent` slot). The identity is measured offline: the bench's loopback
  answers a minimal stream (no `ping`, no cache usage detail, no stop sequence) to an API-key login, and the session
  tests add a claude.ai login with its account reminder on the opening turn. A real login has not been run through
  the bench.
- A `/steer` after a tool round continues the same process. The plugin writes the steer to Claude Code's input just
  before the tool results, so Claude Code reads both and sends one request. It holds the steer as a system row of its
  own ("The user sent a new message while you were working: ..."); the relay puts it back as the text block after the
  results, where Hermes has it, in that request and every later one, so the bytes are per-call's (checked on 2.1.292,
  2.1.322 and 2.1.323, with one to three tool calls in the step). A steer row the relay cannot match is refused before
  upstream and the step is replayed. At round 200 the steer step takes 0.04 to 0.05 s kept, against 1.0 to 1.2 s when
  it restarted the process (as before) and 0.9 to 1.1 s per-call (offline, Linux, bench below).
- Any other change Claude Code cannot continue from restarts it with a replay, exactly as a fresh call would: an edited
  or compacted history, a steer of more than one text block, a different model, system prompt, tool list, effort or
  output cap. Each restart logs its reason at debug level (`launch`, `edited`, `results`, `steer`, `exited`,
  `diverged`, `unproven`, `no_tool_id`, `tool_id`, `control`, `event`, `version`).
- A kept process that does something the plugin does not know is not trusted again: a tool call without
  `claudecode/toolUseId`, with an id its step never received or one it asked for before, a control request other than
  the hermes server's MCP traffic (another server, a tool Hermes did not offer, no JSON-RPC id), or an output line
  that is not a JSON object. Its call is never parked or waited on. One that comes after its step returned, or the end
  of its output, ends the process at once. A kept process's relay forwards nothing until that process's own init
  event has named the Claude Code version the gate allowed (`version` otherwise).
  If its request had not gone upstream, the step replays on a fresh one-shot; if it had, the process is not kept
  after the step (the answer stands, or the step fails if native's output no longer parses). `unproven` means the per-call request for the first resumed step could not be built.
- A kept process counts against `max_concurrent` only while it serves a step. Between steps, idle or with a tool
  parked, it holds no place, so idle agents do not block title and summary calls; `max_sessions` bounds how many
  stay alive, across every profile the Hermes process serves.
- These calls still start a fresh process for one step (a one-shot): a conversation's steps before it has had
  `session_min_rounds` tool rounds (sets of tool results; the step that brings the last of them starts the kept
  process), auxiliary calls (titles, summaries), calls without tools, a second call on the same client while one is
  running, and a call at `max_sessions` when no kept process can give its place up (each is serving a step or holds a
  parked tool call).
- The process ends on `cancel()`, `close()`, Hermes releasing the client, an agent call without tools (which it
  cannot serve), turning `session_mode` off, eviction at `max_sessions` (the one that has
  waited longest for its user), `session_idle_seconds` waiting for its user (an hour by default, the prompt cache's
  lifetime), and when Hermes exits, even by `SIGKILL`: its standard input closes, which ends Claude Code's session.
- A subagent's process ends when the subagent finishes: Hermes closes the child agent on delegate completion
  (`child.close()`, `tools/delegate_tool_child_run.py`), which closes its model clients (`client.close()` in
  `agent/client_lifecycle.py`, for the shared client and the cached per-request one), and `close()` ends the kept
  process at once. A client Hermes drops without closing ends its process when it is collected.
- A tool parked on a human (an approval, a hand-off) keeps its call open: the plugin holds Claude Code's `tools/call`
  until Hermes brings the result, and Claude Code waits for it (`MCP_TOOL_TIMEOUT` is set to 24 hours on a kept
  process). Hermes' request timeout and stale timer run only inside a model call, not while a tool is parked, and the
  idle limit does not run while a call is parked either. Measured on Linux and macOS: a call parked for 150 s continued
  on the same process with the same upstream bytes.
- Claude Code reports list price per process and only when a turn ends, so a tool step has no price of its own and the
  turn's last step carries the turn's.

Tool calls stream to Hermes as Claude writes them: the tool's name as soon as the call starts (Hermes shows "Preparing
<tool>"), then each piece of its arguments (#326). A call whose arguments do not parse, cut at the output cap or invalid
JSON from the API, reaches Hermes with its raw arguments and finish `length`. Hermes never runs a call from a `length`
reply; it sends the request again with a larger output cap (#164, #221). After invalid JSON (not a cut), that
conversation's next requests ask the API to validate tool arguments before sending them, as Hermes' own Anthropic
transport does. A refusal that interrupts a call stays one refusal: the plugin closes the streamed arguments so they
parse, and the call is never run.

## Ownership and replay

A new process starts once a `max_concurrent` place is free and runs in the plugin's stable working directory, with
its system prompt and settings in private launch files that are removed after its first step (and swept on the next start if Hermes was killed). Native tools, skills and setting sources are disabled. MCP advertises only the current Hermes tool inventory, and native `dontAsk` denies anything else. Full descriptions and schemas are supplied through tools plus validated generation fields in `CLAUDE_CODE_EXTRA_BODY`, applied from a private native settings file; the system prompt uses a private file too. This avoids the OS per-argument/environment-string limit. Authentication and identity fields are never replaced.

For per-call replay, native moves today's date line from its environment message into a trailing `role: system` message, and puts its context reminder ahead of the newest user turn; neither recurs on the next request. The relay returns that exact date line to the end of the environment message (other trailing text stays put) and drops plain-text reminders before the newest queried user frame after an assistant turn, as native's own tool-result steps already do. Later requests then replay the context each step's signed thinking was generated against, and keep the previous turn's cache prefix (#40, #36). A frame that cannot be found exactly once is left as native built it. Removed cache markers transfer without increasing their count.

Canonical history is replayed in order. Historical user frames use `shouldQuery:false`, each with a zero-turn acknowledgment; the final user/tool-result frame queries. A kept process receives only the new user frame or the parked tool results (with a `/steer` written just before them). There is no native approval wait, and the adapter adds no synthetic continue prompt. The local admission relay prevents native recovery from issuing another upstream request. The native token-budget reminder is disabled because Hermes owns budgets and replay reconstructs that reminder across the cache boundary. Native's silent-turn reminder ("The user hasn't heard from you in a while...", added after five tool steps without text) is disabled too (`CLAUDE_CODE_SILENT_TURN_REMINDER=0`): a kept process counts those steps live and a replayed one only at the next user turn, so the two would send different requests, and Hermes' own UI already shows tool progress. Other native annotations remain present, so the wire prompt is not byte-identical Hermes-only context. On the conversation's first request native prepends its per-request context (the account-email reminder) to Hermes' first user turn but replays that turn without it afterwards; the relay drops that plain text before Hermes' frame, so the first turn is sent exactly as later requests replay it, is cached once instead of being re-written on call #2 (#77), and Claude never reads Claude Code's context as part of the user's message (#195). Native's auto memory and CLAUDE.md files (including a managed one) stay off (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`): `--setting-sources ''` does not cover auto memory, and Hermes owns memory and instructions.

The relay restores the newest tool-result frame from Hermes' query when it can match the native frame unambiguously, so a native account-email reminder cannot masquerade as tool output. A frame it cannot match losslessly is forwarded exactly as native built it, and the mismatch is reported in the response usage (`native_admission.unrestored`). Restoration is limited to queried frames containing tool results; ordinary user turns and separate native system messages are untouched. The relay treats omitted `is_error` as `false`, restores Hermes' exact original result shape after matching equivalent plain-text encodings, and admits only unmarked plain-text additions after the matched frame. Images, metadata-bearing text, unexpected results, ambiguous cache-marker mappings, or changed tool identity/content leave the native frame untouched (fail open), so a CLI that reshapes its tool results costs an uncached tail, never a failed turn. Marker-bearing additions are validated before breakpoint relocation, preventing cache-marker laundering onto host content. When native marks more than one message block (Claude Code 2.1.287+ marks the last `tool_use` and a trailing per-request `role: system` message), marks after the last block the next request replays are folded into one breakpoint on that block; marks inside the replayed span, and every system/tools mark, stay as native placed them. Once a user message carries more than tool results (the next turn, or a `/steer` row), cache entries written after the finished turn's first thinking block stop matching, and Anthropic's lookup walks back at most 20 blocks from a breakpoint, so after a longer tool loop the next turn's first call read only tools and system and wrote the whole conversation again (#122). The relay therefore keeps one more message breakpoint on the last block of the user message right before the open turn's first assistant message with thinking (usually the turn's opening frame, or a `/steer` row), where an earlier request of the turn already wrote an entry, once that block is more than 20 blocks (counted without merging tool runs) before the recurring breakpoint; the next turn then re-writes only the finished loop from its first thinking block. A shorter loop, or one without thinking, keeps the recurring breakpoint alone. Native 2.1.285 places three breakpoints (two system, one message), so the extra one is the fourth; on a request already at Anthropic's limit of four (2.1.287+), a native mark between it and the recurring breakpoint moves there instead, and with neither it is skipped. Every marker in the request takes a slot, including a top-level one and markers nested in tool-result content. So the count grows by at most one and never past four, the extra mark takes the TTL of the next mark after it, and system/tools marks are never touched. Synthetic loopback tests establish wire-history continuity across requests and client recreation, not vendor cache hits or savings.

The relay binds an ephemeral loopback port with a random per-request route. Native authorization headers pass through memory directly to the upstream and are never logged or persisted. The upstream request body and native identity headers are preserved, while HTTP transfer encoding is normalized. The relay captures streamed text, signed thinking, tool arguments, usage and stop reason before native recovery can replace them. Cancellation shuts down the active upstream connection and the native process; request teardown removes the listener. No external relay service or bundled vendor executable is required.

### Long-context caching qualification

A real Sonnet 5 Hermes review task reproduced poor cache reuse: its first request had 187,049 input tokens; the next tool round read only 6,989 of 190,840 input tokens from cache (3.66%), rewriting 183,849 tokens into the one-hour cache. Identical-request testing at 179K tokens had passed, but did not exercise this replay boundary.

Disabling the native reminder restored stable prefixes without adding cache markers or changing cache TTL. A completed seven-call review used ten real Hermes tool executions, ran six offline tests, and wrote its review artifacts. Context grew from 187K to 212K; follow-up requests averaged 97.99% cache reads, or 85.45% including the cold start. Native list-price accounting totalled $1.1030034, not a verified subscription charge. These are Sonnet 5 results: Fable 5.1 required usage credits on the qualification account and was not exercised with paid credits. Model entitlement and allowance consumption remain native-account dependent.

Text streams incrementally. A complete tool batch is published only after assistant completion, `message_stop`, final usage and native exit. Hermes then applies its own hooks, approvals, tools and persistence. Tool names map through `mcp__hermes__`; original names must be unique ASCII alphanumeric/underscore/hyphen identifiers of at most 64 characters. Names longer than 50 characters are shown to native under a stable hash-suffixed 50-character alias that maps back to the original name.

In a one-shot, `--max-turns 1` is a logical native step; the relay supplies the HTTP admission boundary. `error_max_turns` is accepted with a complete tool batch, usage and exit code 1. A kept process has no `--max-turns`: its step ends at the upstream `message_stop` (with tool calls parked) or at native's result. Native `num_turns` may be 2 at that boundary. A completed first response also survives a locally denied recovery attempt or native refusal rendering; Hermes receives the actual refusal, not the CLI's synthetic error text. Other failures remain failures.

A versioned `reasoning_details` envelope retains ordered native assistant messages and signed thinking. Unchanged projections preserve native blocks, including harmless surrounding-whitespace normalization. Transformed assistant text/tool projections replay canonical text and tool-use blocks instead of stale signed thinking; foreign provider reasoning carriers are ignored. Edited-assistant replay passed against the real service. Native autocompaction is disabled so Hermes retains compaction ownership; this does not establish parity for every history transformation or cross-model signed replay.

Mid-turn `/steer` uses Hermes' standard delivery: a standalone typed user row appended after the newest tool result, which this provider sends as a text block after the tool results in the same user message. A one-shot replays it that way; a kept process takes it on its input before the results, and the relay restores the same bytes (see Transport). Natural change-of-plan steering passed live in the real loop; transport fidelity cannot guarantee model obedience.

## Lifecycle and request support

Outside an event loop, `create` is synchronous; inside an event loop, it returns an offloaded coroutine. Streams also support `async for`. One client should belong to one independently cancellable Hermes owner.

`cancel()` kills the owned process tree (POSIX process group / Windows `taskkill /T`) without closing another thread's active descriptors. `Client.close()` leaves running requests alone and the client keeps accepting new ones: Hermes' auxiliary cache closes a client another event loop is still using, and each request owns and reaps its own process (#378). A stream's own `close()` / `aclose()` stops that request and is required for early stream exit; active consumers unwind after cancellation. Live interruption stopped generation and the observed native PID exited.

Supported translation includes text, base64/native images and documents, canonical tools/results, output-token limits, stop sequences, reasoning enable/disable and effort, and JSON-schema response-format projection. Unsupported native sampling fields are omitted rather than forwarding deprecated `temperature` from auxiliary callers. Reasoning effort is clamped to native-supported levels, including Hermes minimal/ultra inputs. Native thinking deltas surface as `reasoning_content`. An upstream refusal (`stop_reason: refusal`) finishes as `content_filter` with `stop_details`' explanation, or its category, and the request id (`(request_id=req_...)`) as `message.refusal`, and logs one warning with the model, request id and category (#341), so Hermes treats it as a terminal refusal (one fallback try) rather than retrying an empty reply. A tool call the refusal cut off is left out of `tool_calls` (Hermes never runs it, and shows the refusal's reason instead of "no explanation"); the signed native turn in the replay carrier keeps it. Model/service restrictions still apply.

Pinned routes declare vision, so user images attach as native image parts and `vision_analyze` returns its image inside the tool result instead of calling `auxiliary.vision`. Only base64 images are sent; a remote `http(s)` image URL becomes a text hint to call `vision_analyze`. An explicit `auxiliary.vision`, `agent.image_input_mode: text` or `model.supports_vision: false` still routes through text.

The relay and provider forward every `text_delta` as it arrives, but text that follows a tool result usually reaches them late and all at once - measured across runs, all of the answer's deltas came in a single upstream read (~6 s) where the same turn without tool history spread evenly - so a client rendering incrementally shows a pause and then a wall of text after tool rounds (#33). The hold is upstream-facing (native's request or the API's handling of replayed tool history), not a relay buffer; pacing the display is the client's job.

Unknown parameters fail explicitly. Hermes Relay's `extra_headers.traceparent` is accepted as host-local tracing metadata and discarded before native request translation; it is never sent to Claude Code or the upstream. Empty/null header containers also work. Any other header or invalid container is rejected, so authorization and native identity remain CLI-owned. Unsupported surfaces include assistant prefill, strict function mode, forced tool choice, `parallel_tool_calls=False`, `n>1`, JSON-object-only mode, arbitrary headers/body fields, remote image downloads, and cross-model signed-history parity. The read-idle timeout defaults to 180 seconds, resets on native output, and accepts Hermes' finite HTTPX read-timeout shape. Large prompts remain subject to native/OS limits.

Hermes chooses retry or fallback from an error's `status_code`, so failures carry one. An upstream rejection carries the first attempt's real HTTP status. A 429 that rejects a used-up plan window (`anthropic-ratelimit-unified-status: rejected`) also carries the window's reset, the `anthropic-ratelimit-unified-reset` epoch native's own limit message is built from, as `body['resets_at']`: Hermes cannot parse that message's clock time (`resets 3:45pm`), and with the field it benches the route until the window reopens instead of on a blind backoff and names the reset time in its limit notice. A 429 that rejects no window (a throttle while the window is open) carries no reset. An error native answers itself carries the status for native's own error code: `rate_limit` 429 (a plan or session limit), `billing_error` 402, `authentication_failed` and a missing login 401, `overloaded` 529, `server_error` 503; a missing Claude Code CLI is 503. A request this transport cannot serve is refused before Claude Code starts with `RequestRejected` (a `ValueError`), which the provider profile classifies as a non-retryable format error that a different fallback provider may still serve (#328); schema-less `json_object` is declared unsupported, so Hermes drops it from auxiliary calls up front (#327); malformed native output and other process failures carry no status. Hermes' non-streaming stale-call watchdog stops a hung call through `cancel()`; the provider profile classifies that kill as a timeout rather than an unknown error.

An incomplete upstream response also carries the captured Anthropic request ID in the error text (`request_id=req_...`) and the exception's `request_id` attribute, when available. Only IDs starting with `req_`, containing ASCII letters, digits, underscores or hyphens, and at most 128 characters long are included. Missing or invalid IDs are omitted. This adds no header dump or prompt/response logging and requires no debug setting. It cannot recover an ID from an earlier failure that did not record it.

In the agent loop Hermes passes its own request timeout as that read-idle limit (`providers.<provider>.request_timeout_seconds`, 1800 seconds by default). Since Hermes 0.22.5 core streams this provider, so its stale timer measures silence since the last chunk, not the whole call (#35). The plugin sends a chunk for every native event that shows the model working (tool arguments, thinking, block boundaries), so a long tool call is not a silence. `process://` counts as a local endpoint, so the stream stale default is the 900 s local ceiling. To change it:

```yaml
providers:
  claude-subscription-directsdk-experimental:
    stale_timeout_seconds: 900
```

## Setup: `hermes model` → Claude Subscription DirectSDK (Experimental)

Selecting the provider asks the Claude CLI itself, never Anthropic, before anything is saved:

1. **Installed?** `claude` must resolve on PATH, in a usual install directory, or through `CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND`. Otherwise one line: install with `npm install -g @anthropic-ai/claude-code`, and the flow stops without touching config.
2. **Logged in?** `claude auth status` (local credential store, ~0.3s). Logged in shows `credentials: ✓ (Claude Pro)`. Logged out on a terminal starts `claude auth login` inline; it opens the browser and takes the pasted code, then the flow re-checks and continues. Without a TTY it prints the instruction and stops.
3. **Which models?** The CLI's `initialize` handshake returns the account's own picker. Claude Code serves that picker only when it talks to Anthropic directly: behind any `ANTHROPIC_BASE_URL`, the relay included, it logs `[servedCatalog] off (not_first_party)` and answers a generic list without the models granted to the account. The handshake therefore runs without the relay; it writes only `initialize` and sends no user turn, so it makes no Messages request (on Claude Code 2.1.316 its debug log shows only its bootstrap, MCP-directory and telemetry fetches). Without the relay nothing counts its upstream calls, so these rows report `upstream_requests: null` (unobserved) rather than a counted zero. An inherited auth or backend override (`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, Bedrock/Vertex/Foundry) keeps the handshake behind the relay. Every row is listed; the pinned catalog below only adds metadata to the models it knows. Pinned rows are mapped to Hermes route ids and deduplicated (`opus` and `opus[1m]` are one 1M route). A model the catalog does not pin yet keeps the id and label the CLI announced and carries a dim `· unpinned` note; it gets `[1m]` only when the CLI itself offers that form, and a plain + `[1m]` pair collapses onto `[1m]` as pinned models do. Rows the CLI marks "Draws from usage credits", plus Fable 5, 5.1 and 5.2 on plans that bill them to usage credits per [Anthropic's plan rule](https://support.claude.com/en/articles/15424964) (Pro; on Team and Enterprise the handshake names only the plan, so those rows read `· usage credits on standard seats`, since premium seats include Fable), carry a dim `· usage credits` note (`· usage credits · unpinned` on an unpinned row); nothing is hidden. Access-gated models (Mythos 5.1 and 5.2) are pinned for their window and thinking policy but listed only when the account's picker offers them: they are never appended from the catalog and never in the fallback list. If the handshake fails the pinned catalog below, minus access-gated models, is used.

The same `discover_models()` feeds `provider_model_ids()`, so the TUI/Desktop pickers and `/model` list the account's picker too.

## Cache lifetime

Claude Code chooses the prompt-cache tier per request: a `claude -p` turn is its main conversation, which asks for the one-hour tier on a subscription within plan usage (and the five-minute tier once the account draws extra usage), at 2x the input rate per write against 1.25x for five minutes. Hermes core never gives a delegated child the one-hour tier, since a subagent calls every few seconds and is gone, but it can only clamp markers it builds; here native places them. The provider therefore asks for five minutes on a delegated child's requests by starting its Claude Code with `CLAUDE_CODE_PROMPT_CACHE_TTL=5m` (a value you export yourself wins). The child is recognised by core's child cache scope (`sub_<hash>`); every other request keeps native's default.

## Subscription usage

Claude Code names its caller on every request, and subscriptions meter the 5-hour window by that name: `claude -p` defaults to `sdk-cli`, which draws the window about 2x faster per token than the interactive CLI or a client's own name ([anthropics/claude-code#97074](https://github.com/anthropics/claude-code/issues/97074)). The plugin runs native as `hermes-agent`, overwriting any name inherited from a host (`sdk-py`, `claude-desktop`). Measured Oct 31 2026 on a Max plan with Opus 5.5 and Claude Code 2.1.316, four arms of four fresh ~43K-token requests each ($1.09 list-equivalent per arm, output pinned), 5-hour utilization read after each arm:

| arm | name on requests | 5-hour Δ (two arms) | total for $2.19 |
|---|---|---|---|
| plugin before this change | `sdk-cli` | +6% / +6% | 12% |
| plugin now | `hermes-agent` | +4% / +3% | 7% |

The same work used about 1.7x less of the window under the plugin's own name. Anthropic has not documented the weighting, so it can change.

The readings below predate the change and describe the `sdk-cli` rate.

Measured Sept 9 2026 on a Pro plan (Sonnet 5 1M, ~500K fresh input tokens per arm, output pinned to `Hello!`, session meter read on claude.ai before/after each arm):

| arm | list-$ pushed | session meter Δ | points per $ |
|---|---|---|---|
| `claude -p` (two windows) | 2.12 / 1.94 | +19 / +17 | 8.9 / 8.8 |
| this provider | 2.36 | +21 | 8.9 |
| Claude Code interactive TUI (two runs) | 2.22 / 2.25 | +12 / +11 | 5.4 / 4.9 |

This provider draws subscription usage at exactly the `claude -p` / Agent SDK rate; there is no Hermes-specific penalty. Interactive Claude Code is metered at roughly 0.58x that rate, so plan for about 60% of the TUI's throughput per 5-hour window (≈ $6.5 vs ≈ $11 list-equivalent of Sonnet 5 on Pro). The wire-level usage buckets are identical across arms, so the weighting is server-side and applies to every `-p`/SDK harness. In that Sonnet 5 comparison Hermes sent ~0.6x the tokens native Claude Code did on identical coding tasks. If your drain looks higher, compare Hermes `/usage` with Claude Code `/cost` on the same task.

Re-checked Oct 14–15 2026 on a Max plan with Opus 5.5 and Claude Code 2.1.299. The API reports 5-hour utilization in 0.01 steps, so the list dollars between consecutive steps were measured inside stretches of one kind of traffic: this provider $2.70 per step (4 intervals) and `claude -p` $2.87 and $2.44 (1 interval each), the same rate within noise. A block of interactive TUI conversations took about $6.65 per step, which suggests the TUI drew usage roughly 2.3x more slowly per list dollar than `claude -p` on this plan. That figure rests on about one meter step of TUI traffic, so treat it as an early reading.

## Model metadata and accounting

The picker exposes these explicit native routes:

| Model | Native selection | Context |
| --- | --- | --- |
| Sonnet 5.5 | `claude-sonnet-5-5[1m]` | 1,000,000 |
| Sonnet 5 | `claude-sonnet-5[1m]` | 1,000,000 |
| Haiku 5.5 | `claude-haiku-5-5[1m]` | 1,000,000 |
| Haiku 4.5 | `claude-haiku-4-5-20251001` | 200,000 |
| Opus 5.5 | `claude-opus-5-5[1m]` | 1,000,000 |
| Opus 5 | `claude-opus-5[1m]` | 1,000,000 |
| Opus 4.8 | `claude-opus-4-8[1m]` | 1,000,000 |
| Fable 5.2 (requires Claude Code 2.1.321 or later) | `claude-fable-5-2[1m]` | 1,000,000 |
| Fable 5.1 | `claude-fable-5-1[1m]` | 1,000,000 |
| Mythos 5.2 (verified organizations; listed only when the account's picker offers it) | `claude-mythos-5-2[1m]` | 1,000,000 |
| Mythos 5.1 (verified organizations; listed only when the account's picker offers it) | `claude-mythos-5-1[1m]` | 1,000,000 |

Short names `sonnet`, `haiku`, `opus` and `fable` resolve to the corresponding pinned routes above (`fable` is Fable 5.2; select `claude-fable-5-1` to keep Fable 5.1). Known 1M model IDs also receive the native `[1m]` suffix automatically; Haiku 4.5 does not. Unknown model IDs pass through unchanged and are never promised 1M: a plain one reports the 200K window native Claude Code applies to an unverifiable id behind the relay, so Hermes' own family-name guess (which would size `claude-opus-5-5` at 1M before it was pinned) cannot budget past it; an unknown `[1m]` id reports nothing, and no Hermes estimate for it exceeds the native 1M. An explicit Hermes `model.context_length` still overrides the host's window, including a smaller compaction budget.

The local relay sets `ANTHROPIC_BASE_URL`, which makes Claude Code apply its gateway defaults. Its documented Sonnet 5 gateway default is 200K unless `[1m]` is selected; this was the cause of the earlier downgrade, not evidence of a general subscription limit. Both native argv and Hermes metadata now select the same window. See [Claude Code model configuration](https://code.claude.com/docs/en/model-config#sonnet-5-context-window).

Native initialization can enumerate the current picker without a Messages request, but its returned list did not include Opus 4.8. The plugin therefore keeps the version-pinned entries as the fallback catalog, and when the live picker answers it appends every pinned route the picker did not name after the advertised rows, so older pinned models (Opus 5, Opus 4.8) stay selectable instead of disappearing once the CLI moves the family alias. A model the live picker offers before this table pins it is listed, `unpinned`, the day the CLI ships it; pinning adds its 1M route and qualified window, it does not decide visibility. It does not spawn a model or query an HTTP `/models` endpoint when opening Hermes' picker. Existing installations may need **Refresh models** to discard the previous cached list; the live picker above is extended with this static list whenever the CLI is logged in.

The follow-up probes used native **2.1.258**. The Sonnet 5 1M route accepted **902,783 actual input tokens** and returned the requested response, with native `contextWindow: 1000000` and exactly one upstream request. Native list-price equivalent was **$3.611238**. A subsequent short `sonnet` request verified automatic routing to `claude-sonnet-5[1m]`; a real `haiku` request resolved to Haiku 4.5 with a 200K window. Opus 5 and Opus 4.8 were then smoke-tested through the integrated relay: each resolved to its `[1m]` route, reported a 1,000,000-token native window, returned the requested reply in exactly one upstream request, and cost $0.001485 list-equivalent each. Fable 5.1 was rejected by the native client on the qualification account: `Fable 5.1 requires usage credits`. That is Anthropic's documented plan rule, not a routing defect: on Pro (and Team standard seats) Fable 5 / 5.1 bill to pay-as-you-go usage credits from the first request, while Max plans include them up to 50% of weekly limits ([Claude Fable models on your plan](https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan)). The entry stays in the picker because every paid plan can reach it; the provider surfaces the native error verbatim and never falls back to another model. Native discovery also labels Opus 1M as drawing usage credits on Pro. Plan-aware picker badges from the native `initialize` handshake (which reports `subscriptionType` without an upstream request) are a follow-up.

### Hermes compaction qualification

A real multi-turn run with a **200K Hermes window** and `compression.threshold: 0.75` automatically compacted after a turn with 154,038 input tokens, at an approximately 168K next-request preflight estimate. The real summarizer completed, SQLite archived fifteen original rows and retained a summary, and the next ordinary request succeeded at 144,568 input tokens. Two additional model calls executed a real `read_file` and returned both the remembered project codename and the exact file value; that tool result was verified in SQLite. This final-routing run used eleven upstream requests total and **$1.221155** native list-price equivalent. Native autocompaction remained disabled: the summary and durable commit were owned by Hermes. The earlier native-200K control also passed; no compressor implementation change was needed.

Token usage retains native uncached/cache-read/cache-write/output components. Completed responses also retain native `total_cost_usd` and `modelUsage`. When every reported model has `costBasis: list` and the total is finite and nonnegative, Hermes records that exact native amount as **estimated API list-price equivalent**, not an actual subscription invoice or extra-usage charge. It is never marked free/included or replaced with guessed alias prices. Missing or invalid final accounting remains unknown; interrupted requests must not be interpreted as free or zero-token service work. Hermes' iteration and runtime budgets remain host-owned; this provider does not add an account-level overage cap.

## Verification

```sh
# HERMES_AGENT_REPO defaults to ~/.hermes/hermes-agent (a hermes-agent checkout supplies `providers`/`agent`)
~/.hermes/hermes-agent/.venv/bin/python3 -m pytest tests/
python evals/directsdk_admission.py /path/to/claude
python evals/directsdk_cache_wire.py /path/to/claude
```

CI runs both evals and the session tests that need the CLI (`tests/test_directsdk_session.py`, `_lifecycle.py`, `_parked.py`, `_steer.py`, `_doubt.py`, `_version.py`, `_memory.py` and `_bytes.py`, which skip where no Claude Code is installed) on Linux and macOS through `evals/ci.sh`, against a pinned Claude Code installed from npm (`CLAUDE_CODE_VERSION`, default 2.1.292). They need no credentials and make no inference calls. Bump the pin to qualify a new Claude Code release: a change in native's wire shape fails here before it reaches users. Session mode then runs on that release once it is added to `session_versions` in `plugin.yaml` (`tests/test_directsdk_session_version.py` fails on a pin that is not listed).

The long-session bench compares the per-call and kept-process paths on the real CLI against a scripted loopback
upstream, with no credentials and no inference:

```sh
python evals/directsdk_session_bench.py /path/to/claude --rounds 200 --park 150 --out bench.json
python evals/directsdk_session_bench.py /path/to/claude --rounds 200 --steer 50,200 --out steer.json
python evals/directsdk_replay_bench.py        # the per-call replay cost alone, on the synthetic CLI fixture
```

It runs one turn of 200 tool rounds, a compaction and a second turn, then an edited tool result, in both modes with one
HOME, and reports wall time per model step as the history grows (min, median, p95, by fifty-round bucket), Claude Code
processes started and alive at once, the kept process's RSS, whether the kept process restarted after the compaction
and the edit, whether a tool parked for `--park` seconds continued on the same process, and whether every upstream body
was byte-identical to per-call's. Linux, 2.1.292 and 2.1.322: a per-call tool round takes 0.35 s (median) in the first
fifty rounds and 0.86 to 0.93 s in rounds 150 to 199; a kept-process round 0.020 to 0.033 s; one process per model
step per-call (209 for the scenario) against 3 kept; the kept process grows from about 180 MB to 280 to 306 MB RSS over
200 rounds; a 150 s park
continues on the same process; every body identical. With `--steer 50,200` Hermes delivers a `/steer` after rounds 50
and 200 and a third mode runs, `session_restart` (the steer restarts the process, as before this release). Linux,
2.1.292, 2.1.322 and 2.1.323, the steer step after round 50 / round 200: kept 0.027 to 0.028 s / 0.043 to 0.051 s,
restarted 0.50 to 0.56 s / 1.03 to 1.18 s, per-call 0.43 to 0.50 s / 0.92 to 1.10 s; every body identical to per-call's
in both session modes. The replay bench on the synthetic CLI puts the plugin's own share
of a per-call replay at 0.06 s for one frame and 0.11 s for 401 frames. It runs a fake CLI, so it bounds the plugin's
share only; the rest of the growth above is Claude Code's start-up and history processing, measured on the real CLI.
`tests/test_directsdk_session_parked.py` covers a parked call and idle eviction during a park (`DIRECTSDK_PARK_SECONDS`,
default 2 s).

Transport invariant tests cover signed replay and harmless normalization, transformed projections, final tool batches/usage, async use, lazy failure, invalid parameters, conflicting auth, and active/paused/unstarted stream cleanup. The admission regression fails on the previous implementation (two upstream requests) and passes with one request, preserving first-response usage including zero values. Its cancellation control verifies upstream socket closure. A real-native ten-case loopback qualification covers normal text, tools, output/context limits, thinking-only recovery, refusal, HTTP errors, disconnects and cancellation, with one upstream request per call. Its responses are synthetic protocol fixtures, not paid-model evidence.

Installed-plugin discovery (`$HERMES_HOME/plugins/<name>/`) and every missing-`claude` path are covered against a temporary `HERMES_HOME`, including constructing the bundled client without spawning native or accessing auth. A fresh subscription-backed AIAgent loop completed two API calls with host `read_file` execution and SQLite persistence; separate service requests accepted edited-assistant replay. Native loopback qualification also accepted 182K of tool schemas plus a 176K system prompt through file-backed settings. Loopback responses remain fixtures, not paid-model evidence.

The subscription-backed task, CLI delegation, streaming, resume, denial, steering and interruption receipts are separate private artifacts. No auth data or trajectories are committed to this repository. Remaining qualification includes broader history transformations, cross-model signed replay, native versions/platforms, and adversarial steering reliability. Subscription invoice/overage reconciliation is not available from native list-price accounting.

## Attribution

Extracted from hermes-agent PR #105863 (`feat: add Claude Subscription DirectSDK (Experimental) provider`) by @teknium1 and @unsupportedpastels. The provider-agnostic parts of that PR (external-process provider profiles, provider-native replay carriers, setup-flow gating, picker plumbing) land in Hermes core via hermes-agent PR #117451; the provider itself lives here as a standalone plugin because Hermes does not bundle vendor-specific subscription transports in-tree. The `claude` presence checks were added during extraction.

MIT licensed; see `LICENSE`.
