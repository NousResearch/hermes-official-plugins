# Hermes Mobile

Official Hermes Agent plugin for paired mobile apps: push notifications (with encrypted previews and Live Activity snapshots) and device capabilities (Calendar, Reminders, Health, Contacts, location, alarms and timers).

`hermes-mobile` is installed by default and loads quietly when no phone is paired and no push relay is configured. If you do not use the mobile app and want it turned off:

```bash
hermes plugins disable hermes-mobile
```

Requires Hermes 0.21.5 or later (`requires_hermes` in `plugin.yaml`).

## What it does

### Push notifications and Live Activity

Registers the send-only `push` gateway platform (`deliver: push` for cron jobs and webhooks) and 12 lifecycle hooks:

- **Notifications (`POST {relay}/notify`)**: finished turns (`reply`), waits on the user (`approval`, `clarify`, `sudo`, `secret`, `input`), cron or webhook deliveries (`delivery`, one push however long the output), and gateway restarts (`system`). Turns on messaging platforms whose own apps notify natively (or, when `HERMES_MOBILE_PUSH_HOOK_PLATFORMS` is set, platforms it does not list), Kanban workers, and silent cron/webhook runs do not send push notifications.
- **Live Activity snapshots (`POST {relay}/activity`)**: each gateway process posts full snapshots of active interactive turns (counts, start times, attention flag, generic status, and HMAC-keyed profile/session ids — never names or text). Background reviews, `/btw` forks, cron, Kanban, subagents, webhooks, and natively notifying messaging platforms stay out.

### Paired device capabilities

Provides 16 tools in the `mobile_device` toolset backed by a per-profile durable SQLite queue. The mobile app claims requests, executes them against the phone's native APIs, and posts timestamped results. The phone never runs the agent.

| Capability | iOS | Android |
|---|---|---|
| Calendar | EventKit | Calendar provider |
| Reminders | EventKit | Not available (answers `unavailable`) |
| Health | HealthKit | Health Connect (`stand_minutes` has no record type) |
| Contacts | Contacts (limited access counts as access) | Contacts provider |
| Location | Foreground only | Foreground only |
| Alarms and timers | AlarmKit | Exact app-owned alarms with notifications |
| Background wake | Silent APNs push | FCM data message |

| Tool | Grant | Result |
|---|---|---|
| `mobile_devices` | — | Paired devices for the current profile, their grants, selected calendars, and shared Health categories |
| `mobile_calendar_events` | Calendar read | Up to 50 events from shared calendars over at most 31 days |
| `mobile_calendar_create` | Calendar write | One new event |
| `mobile_reminders_list` | Reminders read | Page of reminders with the user's lists |
| `mobile_reminder_create` | Reminders write | One reminder |
| `mobile_reminder_update` | Reminders write | Changes to one reminder (advertised when a paired device supports `reminders.update`) |
| `mobile_reminder_complete` | Reminders write | Completion of one reminder |
| `mobile_health_summary` | Health read | Shared categories over up to two years |
| `mobile_health_log` | Health write | 1 to 20 new samples in categories shared for logging |
| `mobile_contacts_search` | Contacts read | Up to 10 people matching a query |
| `mobile_location` | Location read | Current location, timestamp, and accuracy |
| `mobile_alarms_list` | Alarms | Alarms and timers this agent set |
| `mobile_alarm_create` | Alarms | One alarm, once or weekly |
| `mobile_timer_start` | Alarms | One countdown timer |
| `mobile_alarm_cancel` | Alarms | Cancellation of one alarm or timer |
| `mobile_request_result` | Current profile | Status and result of an earlier request |

A tool waits up to 20 seconds (`wait_seconds`) for the phone, then returns a pending status. Requests expire after five minutes (or when a phone's 35-second claim lease lapses, if later). When push is configured, the plugin asks the relay to wake the phone (`POST /devices/{id}/wake`), and `notify_when_ready` (default true) schedules one follow-up cron reply in the same profile once the result arrives or expires.

## Setup from the app

Pair your Hermes install from the mobile app's notification and Device Access settings. The app configures the relay URL and install key remotely as environment variables or scoped secrets, and registers per-profile device grants and scopes (every agent starts with no device access).

| Env var | Purpose |
|---|---|
| `HERMES_MOBILE_PUSH_RELAY_URL` | Base URL of the push relay (`https://`, or `http://` to localhost only, since the install key travels with every request) |
| `HERMES_MOBILE_PUSH_INSTALL_KEY` | Install key minted by the mobile app pairing this Hermes install with your devices |
| `HERMES_MOBILE_PUSH_HOME_CHANNEL` | Optional label so `deliver: push` resolves (default `device`) |
| `HERMES_MOBILE_PUSH_HOOK_PLATFORMS` | Optional comma-separated gateway platforms whose finished turns notify; unset notifies every gateway platform except natively notifying messaging apps |

### While you are at the computer

When Hermes reports the submitting client and active viewers (`turn_client()` and `session_viewers()`), replies to messages sent from Desktop or the Webapp include `hermes.source: "desktop"` and `hermes.desktop_active`. You can also suppress reply pushes while active at a computer via `config.yaml`:

```yaml
platforms:
  push:
    active_seconds: 300        # how recent your last input must be; positive number, default 300
    quiet_while_active: false  # true: hold back reply pushes while active at the computer
```

Approvals, questions, password/secret prompts, failed turns, cron and webhook deliveries, subagent completions, and gateway notices are never held back.

## Privacy and data

- **Encrypted notification previews**: Previews are encrypted end-to-end with AES-256-GCM using a key stored on the Hermes host and on paired devices. Without the key or `cryptography`, the plugin falls back to a generic content-free alert and never sends plaintext over the relay.
- **Opaque Live Activity snapshots**: Profile and session identifiers are HMAC-SHA256 digests keyed by the install key; no names, raw session ids, tool names, or message text leave the server.
- **Device access isolation**: Grants and selections on the phone are authoritative; revoking a grant or narrowing a scope immediately cancels queued work and clears cached results on the server. Device pairing tokens are stored hashed on the server. Arguments and results are pruned after 24 hours; spent idempotency-key hashes are retained for 30 days to prevent duplicate writes.
- **Preserved legacy data paths**: To keep existing pairings working without re-pairing, data stays at its original locations:
  - Preview key (shared across profiles): `<root>/plugin-data/hermes-mobile-push/preview_key`
  - Device queue (per profile, `0700` dir / `0600` db): `<profile>/plugin-data/hermes-mobile-device-access/bridge.db`

## Dashboard routes

Mounted under `/api/plugins/hermes-mobile/` (see `openapi.yaml` for the full contract):

- `GET /api/plugins/hermes-mobile/sessions/silent` — newest scheduled and webhook runs (up to 1,000) whose final reply was a silence marker.
- `GET /api/plugins/hermes-mobile/setup` — protocol version, plugin version, readiness state, and live profile names.
- `PUT /api/plugins/hermes-mobile/devices/{device_id}` — register or update a device's grants, scope, token, and features for a profile.
- `POST /api/plugins/hermes-mobile/devices/{device_id}/pending` — claim up to four waiting requests under a 35-second lease (requires `X-Hermes-Mobile-Device-Token`).
- `POST /api/plugins/hermes-mobile/devices/{device_id}/results/{request_id}` — submit the result for a claimed request (requires `X-Hermes-Mobile-Device-Token`).
- `GET /api/plugins/hermes-mobile/requests/{request_id}` — inspect a request's recorded state and result.

## Tests

From `hermes-mobile/`:

```bash
python -m pytest -q
```

With a `hermes-agent` checkout on `PYTHONPATH`, the real-Hermes integration tests run alongside the stubbed unit suite.
