# hermes-mobile-push

Sends Hermes Agent notifications from a Hermes install to the user's phones. It is a
send-only `kind: platform` plugin with hooks and no tools: it posts to the push relay
([NousResearch/hermes-mobile-push-relay](https://github.com/NousResearch/hermes-mobile-push-relay)),
which delivers to every device paired with the install, iOS through APNs and Android through
FCM. The apps install and configure it from their notification settings. It needs Hermes
0.21.3 or later (`requires_hermes` in `plugin.yaml`).

## What it sends

- **Notifications** (`POST {relay}/notify`): a finished turn (`reply`), an
  agent waiting on the user, cron or webhook output delivered with `deliver: push`
  (`delivery`, one push however long the output) and gateway restarts (`system`). A wait on
  the user is an `approval` (a command), `clarify` (a question), `sudo` (an administrator
  password), `secret` (a secret or API key) or `input` (a password manager unlock, one-time
  code or saved login). Approvals notify on every surface, questions and password prompts in
  Hermes Agent's and the desktop's sessions; secret and `input` prompts notify only there,
  from one of the two sources that follow, since `on_human_input_request` never reports them.
  In those sessions each push carries the id that answers the wait: on a Hermes whose
  gateway reports the prompts it opens (`on_session_prompt`, `gateway.session_prompt_hooks`)
  the plugin notifies from that hook and not from `on_human_input_request`; in a
  `hermes serve` (sidecar) session it polls the requests the session puts to its client.
  Elsewhere `on_human_input_request` notifies, and its push opens the chat. A
  turn's reply is the final text `post_llm_call` reports wherever the agent runs, sent once
  `on_session_end` says the turn completed; turns on messaging platforms whose apps notify
  natively (with `HERMES_MOBILE_PUSH_HOOK_PLATFORMS` set: on gateway platforms it does not
  list), Kanban workers and silent cron or webhook runs send none. A cron run or webhook route
  that delivers to push (`deliver: push`, or cron's `all` or `origin` when they reach push)
  notifies through that delivery only, and a cron run that does not sends its output as a
  `delivery`. Each preview is encrypted for the device (`privacy.py`, AES-GCM); without the
  key or `cryptography` it falls back to the relay's generic alert and never sends plaintext.
- **Live Activity reports** (`POST {relay}/activity`, `activity.py`): each
  gateway process posts full snapshots of the interactive turns it runs, as counts, times, a
  generic status and HMAC-keyed profile and session ids; no names or text. A turn leaves at
  its `on_session_end`, or once no hook has touched it for 15 minutes (an hour while a tool
  runs or it waits on the person); background reviews and `/btw` forks do not count.

Every request carries the install key as `Authorization: Bearer <key>`, never in the URL.
The relay's README describes both bodies and how it delivers them.

## Configuration

| Env var | |
|---|---|
| `HERMES_MOBILE_PUSH_RELAY_URL` | Base URL of the relay (required); `https://`, or `http://` to localhost only, since the install key travels with every request |
| `HERMES_MOBILE_PUSH_INSTALL_KEY` | The install key the app minted, which pairs this install with its devices (required) |
| `HERMES_MOBILE_PUSH_HOME_CHANNEL` | Any label; only needed so `deliver: push` resolves (default `device`) |
| `HERMES_MOBILE_PUSH_HOOK_PLATFORMS` | Comma-separated gateway platforms (messaging platforms, `webhook`, `api_server`, …) whose finished turns notify; set, exactly those do and no others. Unset, every gateway platform except those whose apps notify natively. Turns in Hermes Agent, the desktop and the CLI, subagents, cron runs and `deliver: push` deliveries are not affected |

The app sets them remotely, as env vars or Hermes's scoped secrets.

### While you are at the computer

On a Hermes whose plugins can ask which client submitted a turn and which clients are attached
to its session (`hermes_cli.plugin_session.turn_client()` and `session_viewers()`), a finished
turn's reply names the client it came from (`hermes.source`: `desktop` for the Desktop app and
the Webapp, `ios` or `android` for a phone). A reply labelled `desktop` also says whether you
are at a computer (`hermes.desktop_active`); no other reply carries it. You are when the turn came from a Desktop or Webapp client, and a
Desktop or Webapp attached to the session reported your input less than `active_seconds` ago
and can get your attention itself (it shows the session in a focused window, or its own
notification for a finished turn is on). That need not be the computer that sent the message,
and a computer whose only recent input was sending it counts. On an older Hermes the reply
carries neither and is labelled as before.

A device with **Desktop Messages** off no longer gets replies to messages sent from Desktop or
the Webapp; before, those replies were labelled by where the conversation was created, and the
switch did not match them. Devices with it on (the default) see no change.

Whether a phone still buzzes for such a reply is each device's choice in its notification
settings (**Desktop Messages**), once the relay supports it. For a relay that does not,
`quiet_while_active` is a stopgap for the whole server: the plugin does not send the reply at
all. Approvals, questions, password and other input requests, failed turns, cron and webhook
deliveries, sub-agent completions and gateway notices are never held back.

Both settings live in the push platform's config in `config.yaml`. Desktop and the Webapp report
your input about every 45 seconds while you are active, so an `active_seconds` under about 60 makes you look
away while you are at the computer, and your phone is notified.

```yaml
platforms:
  push:
    active_seconds: 300        # how recent your last input must be; a positive number, default 300
    quiet_while_active: false  # true: no reply push while you are active at the computer
```

## Data

The apps also keep the install key in each profile's home, at
`plugin-data/hermes-mobile-push/install_key` (older installs kept it beside the plugin, in
`plugins/hermes-mobile-push/install_key`), so another device that pairs with the same install
adopts it instead of minting a new one; the plugin itself reads only
`HERMES_MOBILE_PUSH_INSTALL_KEY`.

The preview key is `plugin-data/hermes-mobile-push/preview_key` in the root Hermes home
(owner-only, shared by every profile's gateway). The apps read it over their Hermes connection
and keep it on the device; it never reaches the relay.

## Dashboard route

`GET /api/plugins/hermes-mobile-push/sessions/silent?profile=…` lists the profile's newest
scheduled and webhook runs whose final reply was a silence marker (`[SILENT]`), so the apps
can leave them out. The contract is `openapi.yaml`.

## Shipping and tests

Both apps bundle this folder at build time, the files `bundle-files.txt` lists (see
[plugins/AGENTS.md](../AGENTS.md)).

`tests/` covers the notification payloads, the Live Activity reporter and the silent-sessions
route, with Hermes's modules stubbed (`tests/push_plugin.py`). `plugins/scripts/test.sh`
runs them. With a Hermes checkout on `PYTHONPATH` the suites that need Hermes run too;
`test_hermes_turn_hooks.py` loads the plugin through Hermes's own plugin discovery.
