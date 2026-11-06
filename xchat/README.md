<p align="center"><img src="../assets/xchat.jpg" alt="X Chat: official Hermes Agent plugin" width="100%"></p>

# X Chat (encrypted X DMs) for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent.

[X Chat](https://docs.x.com/xchat/introduction) is X's end-to-end encrypted direct-message system. The Hermes adapter connects your agent to one X account's DMs: message bodies are encrypted and decrypted **locally** with the official Chat XDK — X only ever routes ciphertext, and every message is signed so recipients can verify the sender.

## Which X account does Hermes use?

**Hermes runs as the X account you connect during setup.** It reads that account's encrypted DMs and its replies are sent from that account. Pick one of two setups:

| Setup | How you talk to Hermes | Who else can reach it |
|---|---|---|
| **Your own account** | Open your chat with yourself on X ("message yourself") and type there. Hermes answers in the same chat. | Anyone who DMs you and is on `XCHAT_ALLOWED_USERS` (or approved by pairing) gets answers from Hermes, sent as you. |
| **A separate account for the agent** (e.g. `@yourname_hermes`) | DM that account from your personal account, like any other chat. | Whoever you allow. Your personal DMs are never touched. |

The self-chat works out of the box: `hermes xchat setup` adds the connected account to `XCHAT_ALLOWED_USERS`, and Hermes never answers its own replies. Turn it off with `XCHAT_ALLOW_SELF_CHAT=false`. Messages you send from your own account in your *other* conversations are never treated as prompts.

Setup registers a new encryption key on the connected account, so try it on a separate account first if you are unsure.

## Install

From the Hermes plugin catalog:

```bash
hermes plugins install xchat
```

Or straight from this repo (tracks `main`):

```bash
hermes plugins install NousResearch/hermes-official-plugins#xchat
```

The install asks once before preparing the plugin's Python dependencies: `chatxdk` (X's official
Chat XDK, a native E2EE binding, pinned to `0.4.1`) and `httpx`. Needs Hermes Agent `0.21.5` or newer.

Then run `hermes xchat setup` for a guided walk-through, or pick **X Chat** in `hermes gateway setup`.

## Prerequisites

- An **X developer account** with an app configured for **OAuth 2.0 user context** ([Developer Console](https://developer.x.com/en/portal/dashboard)). X Chat endpoints require API access on your developer plan.
- A **user access token** for the account Hermes should run as (see above) with scopes: `dm.read`, `dm.write`, `users.read`, `tweet.read` (add `offline.access` to receive a refresh token so Hermes can auto-renew the ~2-hour access token, and `media.write` to send encrypted file/image attachments).

## Setup

```bash
hermes xchat setup
```

The wizard:

1. Stores the OAuth2 access token (and optional refresh token + client id) in `~/.hermes/.env`.
2. Shows which account Hermes will run as (`@handle (id)`, from `GET /2/users/me`) and adds that account to `XCHAT_ALLOWED_USERS` so your self-chat works.
3. Generates the E2EE identity + signing keypairs with the Chat XDK, saves the private-key blob to `~/.hermes/xchat/private_keys.b64` (mode 600), and registers the public keys with the X API.

Key registration is **rate limited to a few writes per 24 hours** per account. The setup is resume-safe: the key blob and registration payload are persisted *before* any network call, so an interrupted or rate-limited run resumes the same identity instead of minting a new one.

Check state anytime:

```bash
hermes xchat status
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `XCHAT_ACCESS_TOKEN` | Yes | OAuth2 user access token for the account Hermes runs as (dm.read, dm.write, users.read, tweet.read) |
| `XCHAT_REFRESH_TOKEN` | Recommended | Refresh token — enables automatic access-token renewal (rotated on every refresh and re-persisted) |
| `XCHAT_CLIENT_ID` | With refresh | X app OAuth2 client id (required for token refresh) |
| `XCHAT_CLIENT_SECRET` | Optional | Only for confidential clients |
| `XCHAT_USER_ID` | Auto | Numeric id of the account Hermes runs as (derived by setup) |
| `XCHAT_SIGNING_KEY_VERSION` | Auto | Registered public-key version (written by setup) |
| `XCHAT_PRIVATE_KEYS_B64` | Optional | Key blob override — takes precedence over the blob file. ⚠️ **This is the account's entire E2EE identity for Hermes**: there is no forward secrecy in this protocol, so anyone holding the blob can decrypt all past *and* future messages. Keep it in `~/.hermes/.env` (never commit it, never pass it on a command line) |
| `XCHAT_ALLOW_SELF_CHAT` | Optional | `false` stops Hermes answering your self-chat (default `true`) |
| `XCHAT_ALLOWED_USERS` | Recommended | Comma-separated numeric X user ids allowed to talk to Hermes; setup adds the connected account itself |
| `XCHAT_ALLOW_ALL_USERS` | Optional | `true` allows every sender (dev only) |
| `XCHAT_CONVERSATION_IDS` | Optional | Pin specific conversation ids to poll; omit to auto-discover |
| `XCHAT_POLL_INTERVAL` | Optional | Seconds between event polls (default `10`, floor `2`) |
| `XCHAT_SEND_READ_RECEIPTS` | Optional | `true` sends read receipts for processed messages (default `false`) |
| `XCHAT_REQUIRE_MENTION` | Optional | In group conversations, only respond when a wake word matches (default `false`) |
| `XCHAT_MENTION_PATTERNS` | Optional | Custom wake-word regexes (JSON list or comma-separated) |
| `XCHAT_HOME_CHANNEL` | Optional | Default conversation/user id for cron delivery |
| `XCHAT_HOME_CHANNEL_NAME` | Optional | Human label for the home channel |

## How it works

- **Inbound** — each poll cycle lists the bot's conversations once (with each one's newest event id) and reads the events endpoint only for conversations that have something newer than the bot's **persistent per-conversation cursor** (`~/.hermes/xchat/cursors.json`), so idle conversations cost nothing. For a conversation that already existed when the gateway first started, it batch-decrypts the backlog (`decrypt_events`) to seed the SDK's verified conversation-key cache **without replying to old messages**; a conversation someone opens while the gateway is running is answered from its first message; after that every new event (including bursts larger than one page, and messages that arrived while the gateway was down) is processed exactly once. `KeyChange` events (conversation-key rotations) are verified and folded into the key cache automatically; message **edits** are treated as new messages.
- **Outbound** — replies are encrypted and signed locally (`encrypt_message` with the session identity), then POSTed as ciphertext. Replies to a specific message use the native threaded-reply event when the target is in the adapter's recent-event cache; inbound threaded replies reach the agent with the quoted message.
- **Media** — both directions are fully encrypted. Inbound attachments are downloaded, decrypted with the conversation key for the *event's* key version, and cached locally so vision/file tools can read them. The download is read under the gateway's inbound-media cap (`gateway.max_inbound_media_bytes`) as it streams, so an oversized attachment is refused while it is read rather than after it has been held in memory. Outbound `MEDIA:<path>` files (images, voice notes, videos, documents) are encrypted with the latest conversation key, uploaded through the 3-step chat-media flow, and attached by `media_hash_key` (requires the `media.write` scope).
- **Senders** — each new sender's public keys are fetched once and pushed into the XDK's signing-key store so their message signatures verify.
- **New conversations** — standalone sends (`hermes send xchat:<user-id>`, cron delivery) to a bare numeric user id perform the conversation-key handshake automatically: both parties' key bindings are verified, a fresh conversation key is wrapped for each participant, and the key change is POSTed before the first message.
- **Identity** — user ids are numeric X user ids; conversation ids look like `123-456` (1:1) or `g123…` (group).

## Authorization

By default only the connected account itself (its self-chat) is allowed; every other sender is denied. To let others in, either:

1. Add their numeric X user ids to `XCHAT_ALLOWED_USERS` (comma-separated), or
2. Use **DM pairing** — an unknown sender gets a pairing code; approve with `hermes pairing approve xchat <CODE>`.

An unknown sender gets a pairing code by default. On your personal account that code is sent from you, so to ignore strangers silently instead, set it in `config.yaml`:

```yaml
platforms:
  xchat:
    unauthorized_dm_behavior: ignore
```

## Group conversations

Group chats (`g…` conversation ids) work out of the box. To keep the bot quiet unless addressed:

```
XCHAT_REQUIRE_MENTION=true
```

The default wake words are `hermes` / `hermes agent`; override with `XCHAT_MENTION_PATTERNS`. DMs are never gated.

## Using X Chat with cron jobs

```python
cronjob(
    action="create",
    schedule="every 1h",
    deliver="xchat",            # uses XCHAT_HOME_CHANNEL
    prompt="Check for alerts and summarise."
)
```

Or target a conversation directly:

```bash
hermes send xchat:<conversation-id> "Done!"
```

Standalone delivery opens an ephemeral E2EE session, seeds the conversation key from the conversation's backlog, encrypts, and sends — no running gateway required.

## Limitations

- **Polling latency.** Inbound uses REST polling (default 10s). Webhook / activity-stream delivery may come later.
- **Group creation.** The bot participates in existing group conversations but does not create new groups or manage membership.
- **Access tier.** X Chat API availability depends on your X developer plan; media upload additionally requires the `media.write` scope.
