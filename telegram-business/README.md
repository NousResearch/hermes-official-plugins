<p align="center"><img src="../assets/telegram-business.jpg" alt="Telegram Business Mode: official Hermes Agent plugin" width="100%"></p>

# hermes-telegram-business

> Part of [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly [`NousResearch/hermes-telegram-business`](https://github.com/NousResearch/hermes-telegram-business), which is archived; this directory carries its full history.

**Observe-with-approval Telegram Business Mode (secretary bot) for [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

Connect your Telegram Business account to your Hermes bot and it becomes your secretary: every incoming customer message gets an LLM-drafted reply delivered to **your** DM with inline buttons — **nothing reaches the customer until you tap Send.**

```
Customer → your Business account (via Business Mode)
            ↓ debounce 8s, draft once
Bot → your DM
       💬 Carol wrote:
         > Hey, are you free Friday afternoon?

       📝 Suggested reply:
       Friday afternoon works — anywhere between 2 and 5pm?

       [✓ Send]   [✎ Edit]   [✕ Discard]
            ↓
Send    → delivered to the customer via your Business connection
Edit    → your next DM text becomes the outgoing message
Discard → dropped, the customer sees nothing
```

## Safety properties

- **No auto-send exists.** Every reply requires an owner button tap, even when `can_reply` is granted.
- **Owner-only buttons.** Callbacks are authorized against the connection's `owner_user_id`; anyone else gets `⛔`.
- **Drafts expire after 24h** (configurable). Stale buttons no-op.
- **Approval is claimed atomically.** Concurrent Send/Edit callbacks cannot deliver the same draft twice.
- **Ambiguous send failures stay closed.** A draft is retryable only when delivery is known not to have been attempted.
- **Ownership is revision-bound.** Every draft records an immutable owner user/chat/revision snapshot, so reassignment — including A→B→A — invalidates old work.
- **Delivery is serialized with connection updates.** Within the single gateway/plugin manager, final ownership/enable/reply validation, customer delivery, and the terminal draft transition share a per-connection async critical section.
- **Without `can_reply`, the Send button is hidden** — you're told to copy/paste manually instead of getting a button that silently fails.
- **Typing bursts coalesce** — new messages within the debounce window supersede the prior draft, one draft per coherent thought.

## Requirements

- Hermes Agent with plugin Telegram-handler support (`ctx.register_telegram_handler`, hermes-agent PR #59159 / v0.18+)
- A Telegram **Business** subscription on your personal account
- Your Hermes Telegram bot with **Business Mode** toggled on in [@BotFather](https://t.me/BotFather)
- python-telegram-bot ≥ 21.1 (already installed with the Hermes Telegram platform)

## Install

```bash
git clone https://github.com/NousResearch/hermes-official-plugins/tree/main/telegram-business \
    ~/.hermes/plugins/telegram-business
hermes plugins enable telegram-business
```

Restart the gateway (`hermes gateway restart`). Then:

1. @BotFather → your bot → **Business Mode** → enable.
2. Telegram → **Settings → Business → Chatbots** → add your bot → pick which chats it can see, and whether it can **reply on your behalf** (needed for the Send button).
3. Message your bot `/biz` to confirm the connection is live.

## Owner controls

| Command | Effect |
|---|---|
| `/biz` | status dashboard |
| `/biz pause` / `/biz resume` | global drafting kill switch |
| `/biz off <chat_id>` / `/biz on <chat_id>` | per-customer-chat mute |

## Configuration

Optional block in `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    telegram-business:
      debounce_seconds: 8        # coalesce typing bursts
      draft_ttl_hours: 24        # stale-draft expiry
      max_customer_text_chars: 4000
      owner_persona: >
        You are drafting replies for a freelance photographer.
        Friendly, brief, always suggest a concrete next step.
```

Drafting uses your active Hermes model through the host-owned plugin LLM surface (`ctx.llm`) — no separate API key.

## State

Plugin-owned SQLite at `~/.hermes/telegram-business/state.db` (two tables: connections + drafts). Hermes' core state is never touched. Schema upgrades are automatic and preserve connection preferences and terminal draft history; legacy unresolved drafts without trustworthy ownership revisions are closed fail-safe. Delete the file to reset.

## v1 limits

- **Text only** — customer media (photos, voice, documents) is skipped; captions do trigger drafts.
- **No conversation history** — each customer message is drafted in isolation. The Edit button absorbs the gap.
- **No persona learning from edits** — your overrides go to the customer but don't train future drafts.
- **Edit capture is in-memory** — restarting the manager terminally invalidates unrecoverable `awaiting_edit` rows instead of leaving them actionable-looking until a TTL; a fresh draft is required after restart.
- **Dispatch serialization is process-local** — Hermes runs one gateway/plugin manager process; the per-connection async lock does not claim cross-process network-send serialization. Durable SQLite claims and guarded retry release remain safe across independent database connections.

## Tests

```bash
python3 -m pytest
```

97 tests covering connection lifecycle, ownership revisions and legacy migration, stale debounce/generator races, per-connection delivery serialization, A→B→A retry release, draft supersession, atomic approval claims across independent SQLite connections, expiration, edit-capture restart/replacement, delivery-failure semantics, all three button paths, owner-scoped callback authorization, and `/biz` subcommands. No network, no live Telegram.

## Credits

Based on the design and implementation from [hermes-agent#30055](https://github.com/NousResearch/hermes-agent/pull/30055), replatformed as a standalone plugin. Related earlier community proposals: [#26654](https://github.com/NousResearch/hermes-agent/pull/26654) by @evgyur (earliest submission), [#35342](https://github.com/NousResearch/hermes-agent/pull/35342) by @MilekhinAV, [#46728](https://github.com/NousResearch/hermes-agent/pull/46728) by @kxnkxv.

## License

MIT
