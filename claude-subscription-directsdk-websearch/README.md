# Claude Subscription WebSearch

An opt-in Hermes `web_search` backend for [claude-subscription-directsdk](../claude-subscription-directsdk): a **separate, isolated** `claude -p --tools WebSearch` call served by your own Claude Pro/Max subscription. Hermes owns approval, logging and the transcript; the directsdk main turn is not touched.

> **Egress disclosure.** Search queries leave through your own Claude subscription (Anthropic's WebSearch), so they **bypass Hermes-side web configuration and egress settings** (backend choice, proxies and allowlists configured for Hermes web tools do not apply to this call). Results are **untrusted text** and are handed back to Hermes exactly as Claude Code returned them: the plugin does not re-rank, trim, summarise or rewrite them.

## Why a second plugin directory

`claude-subscription-directsdk` is `kind: model-provider`. Hermes' plugin loader skips that kind (`hermes_cli/plugins_discovery.py`, "handled by providers/ discovery") and `providers/__init__.py` only imports its `__init__.py`; it never calls `register(ctx)`. A `web_search` provider can only be registered through `ctx.register_web_search_provider(...)`, so it lives in this small `kind: backend` companion. Its settings are read from the model provider's entry, so there is one place to configure it.

## Enable

```bash
hermes plugins install NousResearch/hermes-official-plugins#claude-subscription-directsdk-websearch
hermes plugins enable claude-subscription-websearch
hermes config set plugins.entries.claude-subscription-directsdk-experimental.settings.native_web_search true
hermes config set web.search_backend claude-subscription-websearch
```

```yaml
plugins:
  enabled: [claude-subscription-websearch]
  entries:
    claude-subscription-directsdk-experimental:
      settings:
        native_web_search: true          # default false: nothing registers as available, no process starts
        native_web_search_model: haiku   # default haiku
        native_web_search_timeout: 60    # seconds, 5-300
web:
  search_backend: claude-subscription-websearch
```

The provider never becomes the automatic fallback: it is only available while `native_web_search` is on, and you choose it with `web.search_backend`.

## What runs

```
claude --model haiku -p --tools WebSearch --allowedTools WebSearch --setting-sources "" \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands \
  --no-session-persistence --output-format stream-json --verbose
```

- Exactly one native tool; no Bash/Read/Edit, no Hermes MCP tools, no settings sources, no stored session.
- Its own scratch working directory per call, removed afterwards.
- No `ANTHROPIC_API_KEY`/`AUTH_TOKEN` is passed (it would bill the API, not the subscription), and an inherited `ANTHROPIC_BASE_URL` or cloud-backend switch refuses the search rather than sending it elsewhere. The profile's own `claude_config_dir` / `CLAUDE_CODE_OAUTH_TOKEN` is used, as for the model provider.
- The prompt asks Claude to call WebSearch at once with your query unchanged. The process is **stopped when the WebSearch tool result arrives**, before Claude writes its own summary turn, so no model-written text is returned and about 3 s is saved.
- Rows are mapped from Claude Code's structured result into Hermes' schema: `title`, `url`, `position`; `description` is empty because WebSearch returns no snippets.
- INFO log per search: `Claude WebSearch query='...' model=haiku results=N (...s)`.

Typical latency on haiku is 8-12 s (the search itself runs inside Claude Code's request).

## Tests

```bash
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

A live check against your own login (one haiku search; not part of CI):
`python ../claude-subscription-directsdk/evals/directsdk_websearch_live.py "$(command -v claude)"`.

MIT licensed; see `LICENSE`. Flags list, scratch-dir approach and settings shape follow the earlier proposal in PR #469 by @salch-cred.
