# Changelog

## [0.3.1] - Unreleased

### Added

- Stall watchdog (`stall_timeout_seconds`, default 300, `0` disables): kill the native call when no model-progress event — text/thinking/signature/tool-input deltas, block starts, results — arrives for that long. Ping and keepalive traffic does not reset it, so a stalled call is bounded (and Hermes retries immediately) even when the connection stays alive; a healthy long call is unaffected (#35).

### Fixed

- Accept Hermes Relay's host-local `traceparent` request metadata without forwarding it to the native CLI or upstream, preventing full agent and fallback requests from failing on `extra_headers`.
- Continue rejecting arbitrary headers, credential overrides, and malformed header containers at the transport boundary.
