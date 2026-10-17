<p align="center"><img src="../assets/homeassistant.jpg" alt="Home Assistant: official Hermes Agent plugin" width="100%"></p>

# Home Assistant for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly [`NousResearch/hermes-homeassistant`](https://github.com/NousResearch/hermes-homeassistant), which is archived; this directory carries its full history.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for
[Home Assistant](https://www.home-assistant.io/). It provides two things:

1. **Gateway platform `homeassistant`**: subscribes to Home Assistant's WebSocket event bus and
   forwards `state_changed` events to the agent, with domain/entity filters and per-entity cooldowns.
   The agent's replies show up as Home Assistant persistent notifications titled "Hermes Agent".
   Cron jobs can deliver to `deliver=homeassistant[:target]` through the target's own notify service
   (`notify.mobile_app_my_phone` for `target=mobile_app_my_phone`), without a running gateway.
2. **Toolset `homeassistant`**: four tools the model can call to query and control devices over the
   REST API: `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service`.

This used to be built into Hermes core. It now ships as a standalone plugin with the same platform
name, toolset, tool names and schemas, env vars and config keys. **If you already used Home
Assistant with Hermes, you don't need to change anything:** `hermes update` (or the first start
after updating) installs and enables this plugin for you.

## Install

From the Hermes plugin catalog:

```bash
hermes plugins install homeassistant
```

The plugin's only Python dependency is `aiohttp` (`>=3.9,<4`, resolved under Hermes's own pins). It
needs a Hermes Agent release of `0.21.5` or newer.

## Configuration

### 1. Create a Long-Lived Access Token

1. Open your Home Assistant instance.
2. Go to your **Profile** (click your name in the sidebar).
3. Scroll to **Long-Lived Access Tokens**.
4. Click **Create Token**, name it something like "Hermes Agent", and copy it.

### 2. Environment variables

```bash
# ~/.hermes/.env

# Required: your Long-Lived Access Token
HASS_TOKEN=your-long-lived-access-token

# Optional: HA URL (default: http://homeassistant.local:8123)
HASS_URL=http://192.168.1.100:8123

# Optional: default notify target for cron deliver=homeassistant
# (a notify service target, e.g. mobile_app_my_phone)
HASS_HOME_CHANNEL=mobile_app_my_phone
```

| Variable | Required | Purpose |
|---|---|---|
| `HASS_TOKEN` | yes | Long-Lived Access Token. Enables both the tools and the gateway platform. |
| `HASS_URL` | no | Base URL of your instance. Default `http://homeassistant.local:8123`. Seeds `platforms.homeassistant.extra.url` when the platform is enabled from env. |
| `HASS_HOME_CHANNEL` | no | Default target for cron `deliver=homeassistant`. `HASS_HOME_CHANNEL_NAME` sets a display name. |

### 3. Tools

The four tools only appear when `HASS_TOKEN` is set. Turn the `homeassistant` toolset on or off per
platform with `hermes tools`.

### 4. Gateway platform

```bash
hermes gateway
```

By default, **no events are forwarded**. Configure at least one of `watch_domains`, `watch_entities`
or `watch_all`, otherwise a warning is logged at startup and every state change is dropped. Settings
go under the platform's `extra` section in `~/.hermes/config.yaml`:

```yaml
platforms:
  homeassistant:
    enabled: true
    extra:
      watch_domains:
        - climate
        - binary_sensor
        - alarm_control_panel
        - light
      watch_entities:
        - sensor.front_door_battery
      ignore_entities:
        - sensor.uptime
        - sensor.cpu_usage
        - sensor.memory_usage
      cooldown_seconds: 30
      # Reuse one persistent notification for routine Hermes replies.
      # Set an empty string to let Home Assistant create a new one per reply.
      notification_id: hermes_agent
```

| Setting | Default | Description |
|---|---|---|
| `url` | `HASS_URL` | Home Assistant base URL |
| `watch_domains` | *(none)* | Only watch these entity domains (e.g. `climate`, `light`, `binary_sensor`) |
| `watch_entities` | *(none)* | Only watch these specific entity IDs |
| `watch_all` | `false` | Receive **all** state changes (not recommended for most setups) |
| `ignore_entities` | *(none)* | Always ignore these entities (applied before domain/entity filters) |
| `cooldown_seconds` | `30` | Minimum seconds between events for the same entity |
| `notification_id` | `hermes_agent` | Persistent-notification ID for agent replies; set to an empty string to create a new notification per reply |

Events come from the instance the adapter authenticated to with `HASS_TOKEN`, so they are not
subject to a user allowlist. The adapter reconnects automatically with 5s → 10s → 30s → 60s backoff.

## Security

`ha_call_service` refuses service domains that allow arbitrary code execution on the Home Assistant
host or requests to your local network. Home Assistant has no service-level access control, so this
blocklist is the only guard:

- `shell_command`: arbitrary shell commands
- `command_line`: sensors/switches that execute commands
- `python_script`: scripted Python execution
- `pyscript`: broader scripting integration
- `hassio`: add-on control, host shutdown/reboot
- `rest_command`: HTTP requests from the HA server (SSRF vector)

Domain and service names must match `^[a-z][a-z0-9_]*$` and entity IDs
`^[a-z_][a-z0-9_]*\.[a-z0-9_]+$`, which rules out path traversal into other REST endpoints
(e.g. `shell_command/../light`).

What the plugin does on your network: it talks only to the Home Assistant URL you configure (REST and
WebSocket) using your `HASS_TOKEN`. It stores no state and sends no telemetry.

## Development

Tests need a hermes-agent checkout on `PYTHONPATH` plus `pytest` and `pytest-asyncio`:

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest tests -q
```

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Nous Research.
