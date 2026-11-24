<p align="center"><img src="../assets/wecom.jpg" alt="WeCom: official Hermes Agent plugin" width="100%"></p>

# WeCom (Enterprise WeChat) for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. This directory carries the adapter's full history from Hermes core.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for
[WeCom](https://work.weixin.qq.com/) (企业微信, Enterprise WeChat). One plugin, two gateway platforms:

1. **`wecom`: AI Bot mode.** Hermes connects out to WeCom's `openws` WebSocket gateway with a bot ID
   and secret, so no public endpoint is needed. Replies stream natively (WeCom's `stream` message
   type gives a typing animation and cumulative updates), images, files, video and AMR voice send as
   native media, and long inbound messages split by the client are merged back together.
   `hermes gateway setup` can fetch the bot credentials by scanning a QR code in the WeCom app.
2. **`wecom_callback`: self-built app mode.** Hermes serves an HTTP callback endpoint
   (`/wecom/callback`, port `8645` by default) that WeCom POSTs encrypted XML to; it verifies and
   decrypts each message (AES, `EncodingAESKey`), acknowledges at once, and replies through the
   app's `message/send` API. Several apps can share one endpoint (`platforms.wecom_callback.extra.apps`).

This used to be built into Hermes core. It now ships as a standalone plugin with the same platform
names, env vars and config keys. **If you already used WeCom with Hermes, you don't need to change
anything:** existing users are migrated automatically, and the plugin is installed by default.

## Install

From the Hermes plugin catalog:

```bash
hermes plugins install wecom
```

Python dependencies: `aiohttp`, `httpx`, `defusedxml` (safe parsing of untrusted callback XML) and
`cryptography`, resolved under Hermes's own pins. It needs a Hermes Agent release of `0.23.2` or newer.

## Configuration

Run `hermes gateway setup` and pick WeCom, or set the variables below in your profile's `.env`.

### AI Bot (`wecom`)

| Variable | Required | Purpose |
|---|---|---|
| `WECOM_BOT_ID` | yes | Smart Robot bot ID |
| `WECOM_SECRET` | yes | Smart Robot secret |
| `WECOM_WEBSOCKET_URL` | no | Gateway URL (default `wss://openws.work.weixin.qq.com`) |
| `WECOM_HOME_CHANNEL` (+ `_NAME`) | no | Chat ID for cron `deliver=wecom` and notifications |
| `WECOM_ALLOWED_USERS` | no | Comma-separated user IDs allowed to talk to the bot |
| `WECOM_ALLOW_ALL_USERS` | no | `true` lets anyone talk to the bot |
| `WECOM_DM_POLICY` / `WECOM_GROUP_POLICY` | no | `open`, `allowlist`, `pairing` (default) or `disabled` |

The platform enables once both `WECOM_BOT_ID` and `WECOM_SECRET` are set. Per-group sender
allowlists live in config.yaml under `platforms.wecom.extra.groups.<group_id>.allow_from`.

### Self-built app (`wecom_callback`)

| Variable | Required | Purpose |
|---|---|---|
| `WECOM_CALLBACK_CORP_ID` | yes | Corp ID |
| `WECOM_CALLBACK_CORP_SECRET` | yes | App secret |
| `WECOM_CALLBACK_AGENT_ID` | yes | App agent ID |
| `WECOM_CALLBACK_TOKEN` | yes | Callback verification token |
| `WECOM_CALLBACK_ENCODING_AES_KEY` | yes | Callback EncodingAESKey |
| `WECOM_CALLBACK_HOST` | no | Bind address (default: all interfaces, IPv4 and IPv6) |
| `WECOM_CALLBACK_PORT` | no | Bind port (default `8645`) |
| `WECOM_CALLBACK_ALLOWED_USERS` / `WECOM_CALLBACK_ALLOW_ALL_USERS` | no | Who may talk to the app |

The platform enables once both the corp ID and corp secret are set; a profile without them never
binds the callback port. Point the app's **Receive messages** URL at
`https://<your-host>/wecom/callback`.

## Development

The tests import Hermes core, so run them with a hermes-agent checkout on `PYTHONPATH`:

```bash
cd wecom
PYTHONPATH=/path/to/hermes-agent python -m pytest -q
```

## License

MIT, see [LICENSE](LICENSE).
