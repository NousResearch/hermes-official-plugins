<p align="center"><img src="../assets/weixin.jpg" alt="Weixin: official Hermes Agent plugin" width="100%"></p>

# Weixin (personal WeChat) for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly built into [Hermes Agent](https://github.com/NousResearch/hermes-agent) core (`plugins/platforms/weixin`); this directory carries its full history.

Gateway platform `weixin`: connects a personal WeChat account to Hermes through Tencent's iLink Bot API.
It long-polls `getupdates` for inbound messages, echoes each peer's `context_token` on replies, and moves
images, video, voice and documents through the AES-128-ECB encrypted CDN. Cron jobs and `send_message`
deliver with `deliver=weixin` (home channel `WEIXIN_HOME_CHANNEL`), with or without a running gateway.

This plugin is **installed and enabled by default on every Hermes home** (catalog `default: true`). It does
nothing until you log in: with no `WEIXIN_*` credentials the gateway never starts the adapter.

**If you already used Weixin with Hermes, you don't need to change anything:** same platform name, same
`WEIXIN_*` env vars, same `platforms.weixin` config keys, same QR-login account files under
`<HERMES_HOME>/weixin/accounts/`. `hermes update` (or the first start after updating) installs it for you.

## Install

It is already there on a current Hermes. To reinstall after removing it:

```bash
hermes plugins install weixin
```

Python dependencies: `aiohttp` and `cryptography` (resolved under Hermes's own pins). Needs Hermes Agent `0.22.3` or newer.

## Set up

```bash
hermes gateway setup   # pick Weixin / WeChat
```

The wizard opens Tencent's iLink QR login in the terminal; scan and confirm with WeChat. Hermes saves
`WEIXIN_ACCOUNT_ID`, `WEIXIN_TOKEN` and `WEIXIN_BASE_URL` to the profile's `.env`, then asks how DMs and
groups are authorized.

| Env var | Meaning |
|---|---|
| `WEIXIN_ACCOUNT_ID`, `WEIXIN_TOKEN` | iLink bot identity from the QR login (both required) |
| `WEIXIN_BASE_URL`, `WEIXIN_CDN_BASE_URL` | iLink API / media CDN base URLs |
| `WEIXIN_DM_POLICY` | `pairing` (default), `open`, `allowlist`, `disabled` |
| `WEIXIN_GROUP_POLICY` | `disabled` (default), `open`, `allowlist` |
| `WEIXIN_ALLOWED_USERS`, `WEIXIN_GROUP_ALLOWED_USERS`, `WEIXIN_ALLOW_ALL_USERS` | access lists |
| `WEIXIN_HOME_CHANNEL`, `WEIXIN_HOME_CHANNEL_NAME` | default chat for cron / notifications |
| `WEIXIN_SPLIT_MULTILINE_MESSAGES` | send each line of a reply as its own message |

The same keys work under `platforms.weixin.extra` in `config.yaml`.

QR login connects an iLink bot identity (e.g. `...@im.bot`), not a scriptable personal account. Ordinary
WeChat groups usually cannot invite it, so DMs are the reliable channel.

## Tests

```bash
cd weixin && PYTHONPATH=/path/to/hermes-agent python -m pytest -q
```
