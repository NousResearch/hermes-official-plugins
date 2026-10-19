# Changelog

## [0.3.1] - Unreleased

### Added

- Per-profile `claude_config_dir` setting selects the Claude login for every native spawn in a multiplexed gateway (#34).

### Fixed

- Restore gzip compression for large direct Anthropic requests, with a process-wide latch and one uncompressed resend when request encoding is rejected.
- Structured output no longer 400s upstream on schema keywords native rejects: `minimum`/`maximum` on integer and number, `maxItems`/`uniqueItems` on array, and `type` beside `enum` (a nullable enum) are dropped from the wire schema only. Hermes still validates the reply against the full schema, so the constraints hold, and core no longer retries without a schema (a doubled call).
- Keep one native session per conversation (`--session-id` from the system prompt and first frame): with feature flags on, native names a per-session scratchpad directory in the environment message after the first user turn, so a new session per spawn changed it on every request and each round re-wrote the conversation to the prompt cache.
- Short model names such as `sonnet-5-5`, `sonnet-5.5`, `opus-4.8` and `haiku-4-5` resolve to their pinned routes (and thinking policy) instead of reaching Claude Code verbatim and 404ing into the fallback chain (#103).
- `/usage` asks Claude Code for the plan limits (`get_usage` control request) instead of reading its login: it now works with `auth.adopt_external_logins: false`, follows the per-profile `claude_config_dir`, and the plugin never holds the CLI's token (#82).
- Return completed per-call answers at native's result event while its shutdown drains in the background with a bounded grace period (~0.6 s per request under the default telemetry setting). Only a clean admitted answer takes this path; its exit code is no longer read.
- Accept Hermes tool names of 51–64 characters through stable 50-character aliases, mapping native tool calls back to the original names and preserving replay.
- Accept Hermes Relay's host-local `traceparent` request metadata without forwarding it to the native CLI or upstream, preventing full agent and fallback requests from failing on `extra_headers`.
- Continue rejecting arbitrary headers, credential overrides, and malformed header containers at the transport boundary.
- Carry the login identity Claude Code's local check reads (`USER`/`LOGNAME`, `USERNAME` on Windows) into the child env, derived from the parent process or the running uid: without it a perfectly logged-in CLI reports `loggedIn: false` instantly, with no stderr and no upstream request — the confirmed mechanism behind gateway-only `ClaudeCodeLoggedOut` failures (#44).
- Capture native stderr (bounded, drained off-thread) and include its first lines in `ClaudeCodeLoggedOut` and native-failure errors, so a gateway-only failure is diagnosable instead of vanishing into `DEVNULL` (#44).
