# hermes-mobile-device-access

Agent tools for data the user explicitly shares from a paired phone: calendar, reminders,
health, contacts, location, alarms and timers. The agent and the dashboard share a durable
SQLite queue; the Hermes Agent app claims each request, runs it against the phone's own APIs
and posts a timestamped result. The phone never runs the agent.

| | iOS | Android |
|---|---|---|
| Calendar | EventKit | Calendar provider |
| Reminders | EventKit | Not available (answers `unavailable`) |
| Health | HealthKit | Health Connect (`stand_minutes` has no record type) |
| Contacts | Contacts (limited access counts as access) | Contacts provider |
| Location | Foreground only | Foreground only |
| Alarms and timers | AlarmKit | Exact app-owned alarms with notifications |
| Background wake | Silent APNs push | FCM data message (`DeviceWakeWorker`) |

The app code is `apps/ios/HermesMobile/Features/DeviceAccess/` and
`apps/android/core/data/.../data/device/`; the iOS feature note is
[docs/product/ios/device-access.md](../../docs/product/ios/device-access.md).

## Install and grants

The plugin needs Hermes 0.21.5 or later (`requires_hermes`). In the app, Settings → Device
Access → Install Device Plugin uploads the bundled plugin to the server's profiles and enables
it, keeping other plugin settings; restart the dashboard and the profile gateways, then Check
Setup. Every agent starts with no access. Per agent the user chooses the shared calendars and
whether it may create events, Reminders read and write, location, each Health category to read
and, separately, to log, each contact detail category (phone, email, birthday, address), and
Alarms. The OS asks for its own permissions on top.

Grants and selections on the phone are authoritative and saved locally before they sync, so a
revocation works offline: it blocks execution at once and removes server results on the next
reconciliation. Changing a calendar or Health selection invalidates queued work and cached
results in that domain. Unshared calendars, categories and contact details are refused on the
server and on the phone.

The phone registers the `features` it supports (`reminders.update`, `reminders.query`); a
request that needs one goes only to a phone that has it, otherwise it answers
`status: unavailable` with `update_required: true`. Hermes offers `mobile_reminder_update`
only while a phone with that feature shares Reminders writes (`check_fn`).

## Tools

All tools are in the `mobile_device` toolset.

| Tool | Grant | Result |
|---|---|---|
| `mobile_devices` | — | Devices paired to the current profile, their grants, selected calendars, Health categories to read and to log |
| `mobile_calendar_events` | Calendar read | Up to 50 events from shared calendars in a range of at most 31 days |
| `mobile_calendar_create` | Calendar write | One new event |
| `mobile_reminders_list` | Reminders read | A page of reminders with the user's lists |
| `mobile_reminder_create` | Reminders write | One reminder |
| `mobile_reminder_update` | Reminders write | Changes to one reminder |
| `mobile_reminder_complete` | Reminders write | Completion of one reminder |
| `mobile_health_summary` | Health read | Shared categories over up to two years |
| `mobile_health_log` | Health write | 1 to 20 new samples in categories shared for logging |
| `mobile_contacts_search` | Contacts read | Up to 10 people matching one query |
| `mobile_location` | Location read | Current location, timestamp and accuracy |
| `mobile_alarms_list` | Alarms | The alarms and timers this agent set |
| `mobile_alarm_create` | Alarms | One alarm, once or weekly |
| `mobile_timer_start` | Alarms | One countdown timer |
| `mobile_alarm_cancel` | Alarms | Cancellation of one alarm or timer |
| `mobile_request_result` | Current profile | Status and result of an earlier request |

Each tool result includes `current_access`, the latest server-synced selections (not the scope
of an older result). An agent refreshes `mobile_devices` after sharing changes; an empty event
list does not prove missing access. When no device shares the data, the answer is
`status: unavailable`, `retryable: false`, `setup_required: true`: the user has to share it in
Settings → Device Access, and retrying cannot grant it.

### Arguments and results

Dates are ISO 8601 with a time zone. Calendar, reminder, Health log and alarm writes need an
`idempotency_key`, reused for every retry of the same action: the phone's write journal and
native markers survive a crash or a lost answer, and after the result is gone (a day) a hash of
the used key still stops the action from running twice, for 30 days.

- **Calendar.** Reads take an optional `calendar_ids` subset; creation takes `calendar_id`, or
  falls back to the shared default or the single shared writable calendar.
- **Reminders.** `mobile_reminders_list` takes `status` (`incomplete` by default, `completed`,
  `all`), `due_start` / `due_end` (end exclusive), `include_undated`, `completed_start` /
  `completed_end` (with `status: completed`; the start defaults to a year ago), `list_ids` (up
  to 50), `query` (title or notes), `limit` (1–100, default 50) and `offset`. The result has
  `total`, `offset`, `next_offset`, `truncated`, `lists` and reminders with `id`, `title`,
  `completed`, `completed_at`, `list`, `list_id`, `priority`, `notes` (cut to 280 characters)
  and `due` or `due_date` with `all_day`. Create and update take `title`, `notes`, `due` or
  `due_date` (or `clear_due` on update), `list_id` or `list` (a title, refused when ambiguous),
  `priority` (`none`, `low`, `medium`, `high`) and, on update, `completed`. An update changes
  only the given fields; moving a timed due moves the alert the app added.
- **Health.** `metrics` picks a subset of the shared categories; `interval` sets the bucket
  (`hour` up to 4 days, `day` up to 92 days, `week` and `month` up to two years; default `day`,
  or `week` past 92 days). Summed quantities give `unit`, `total` and a `series`; the others
  `unit`, `average`, `minimum`, `maximum`, `latest` and a `series`. `sleep` gives the stage
  hours and `nights`; `workouts` lists the 60 newest (`workouts_count`, `workouts_truncated`)
  with their statistics, but no routes. `steps` with a range also returns `daily_steps`. A
  value with no readable samples is null, never zero; an omitted key means not shared. An
  answer over 120 kB is refused: ask for fewer categories, a shorter range or a coarser
  interval.
- **Health logging.** `entries` (`metric`, `value`), an optional `date` (default now, at most
  a year back) and an optional `food` name. Units are fixed: water mL, caffeine and sodium mg,
  dietary energy kcal, protein, carbohydrates, fat, sugar and fiber g, weight kg, body fat %,
  mindful minutes (ending at `date`). Each entry is a new sample marked as user-entered, with a
  sync id from the request; nothing existing is changed.
- **Contacts.** A non-empty `query` (an `@` searches email, digits and phone punctuation search
  numbers, anything else names), optional `fields` and `limit` (1–10, default 5). There is no
  way to list the address book. Each match has the name, the organisation and only the shared
  details (at most five numbers, five emails, three addresses; a birthday without a year is
  `--MM-DD`); `truncated` says more matched.
- **Alarms.** `mobile_alarm_create` takes `at` (one alarm) or `time` (`HH:MM` on the phone's
  clock) with optional `weekdays`, and a `label` of at most 100 characters.
  `mobile_timer_start` takes `duration_seconds` (1–86400) and a `label`. `mobile_alarms_list`
  returns `id`, `label`, `kind`, `state` (`scheduled`, `counting_down`, `paused`, `ringing`) and
  the time; timers add `duration_seconds` and `ends_at`. The agent sees and cancels only the
  alarms it set, never the system clock's.

### Health categories

| Group | Categories |
|---|---|
| Activity | `steps`, `walking_running_distance`, `cycling_distance`, `flights_climbed`, `active_energy`, `resting_energy`, `exercise_minutes`, `stand_minutes` |
| Heart | `heart_rate`, `resting_heart_rate`, `walking_heart_rate`, `heart_rate_variability`, `vo2_max` |
| Vitals | `respiratory_rate`, `blood_oxygen`, `blood_pressure`, `blood_glucose`, `wrist_temperature` |
| Body | `weight`, `body_fat`, `lean_body_mass`, `body_mass_index`, `height` |
| Nutrition | `dietary_energy`, `protein`, `carbohydrates`, `fat`, `sugar`, `fiber`, `sodium`, `caffeine`, `water` |
| Sleep and mindfulness | `sleep`, `mindful_minutes` |
| Workouts | `workouts` |

## Waiting, wakes and replies

A tool waits up to 20 seconds (`wait_seconds`) for the phone, then returns a pending status;
a write is never resubmitted under a new key because it is pending. Requests expire after five
minutes; one the phone has claimed stays open until its 35-second lease lapses, so an answer to
a request claimed just before its deadline still counts. When the push plugin is configured (`HERMES_MOBILE_PUSH_RELAY_URL`,
`HERMES_MOBILE_PUSH_INSTALL_KEY`), the plugin asks the relay to wake the phone
(`POST /devices/{id}/wake`, the install key as its bearer token), and `notify_when_ready` (default true) schedules one
Hermes cron reply once the phone answers or the request expires; that reply reads
`mobile_request_result` in the original profile. Set `notify_when_ready: false` to poll instead.
The profile's own processes schedule it: the phone's claim or result, a timer the tool sets for
the request's expiry (which tries again, backing off for about two minutes, while another
process is scheduling replies or scheduling fails), or, after a restart, the profile's next
device tool call.

Wakes are best effort. The relay coalesces them per device (20 minutes on APNs, 2 minutes on
FCM), and the OS decides whether to deliver. While the app is open it fetches requests itself;
in the background a direct HTTP(S) connection can answer a wake, an SSH connection only in the
foreground. Location needs the app in the foreground, and Health can need an unlocked phone.

## Data on the server

Each profile keeps its state in its own home, in Hermes' per-plugin storage:
`plugin-data/hermes-mobile-device-access/bridge.db` (SQLite, in WAL mode where Hermes allows it
for its own databases, the folder `0700`, the file `0600`). A profile sees only its own
devices, requests and replies. A route opens the store of the profile the request is for. A
route in the dashboard process gets the home Hermes binds from `?profile=` (every supported
Hermes). A profile's plugin host (`plugins.isolation: host`) binds none and runs in its
profile's home: there the route reads `?profile=` itself, and without it (Hermes after 0.22.2
forwards a secondary profile's requests to its host without `?profile=`) serves the host's own
profile. Claims and completions need a device token, stored hashed on the server and in the
phone's keychain, on top of the dashboard's own authentication. Arguments and results are kept
at most 24 hours; used-key hashes outlive them. Shared data goes to the user's Hermes and its
model provider, as Settings explains. Chat shows device calls as status rows that carry no
data, so the tool descriptions ask the model to write out the answer.

## Tests

`tests/` (grants, the bridge, reminders, access refresh, locking) runs with
`plugins/scripts/test.sh`, or from this folder:

```sh
python -m pytest --import-mode=importlib --rootdir=tests tests -q
```
