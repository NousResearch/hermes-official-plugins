"""Opt-in native WebSearch as a Hermes web-search provider (#389, Shape A).

A SEPARATE ``claude -p`` call with exactly one native tool, run by ``_probe`` in its own scratch
working dir with no Hermes MCP servers. The main turn is untouched: it still spawns with
``--tools ''`` and still forwards exactly one upstream ``/v1/messages`` through the admission relay.

Off by default (``native_web_search: false``), per profile. Queries leave through the user's own
Claude subscription and bypass Hermes-side web config and egress settings.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from directsdk_setup import (
    _child_env, _probe, _resolve, _setting, native_web_search_enabled,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "claude-subscription-websearch"
# One native tool, no slash commands, no persisted session, no MCP servers of any kind.
SEARCH_FLAGS = [
    "-p", "--tools", "WebSearch", "--allowedTools", "WebSearch",
    "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--disable-slash-commands", "--no-session-persistence",
    "--output-format", "stream-json", "--verbose",
]
SEARCH_TIMEOUT_SETTING = "native_web_search_timeout"


def _timeout() -> int:
    raw = _setting(SEARCH_TIMEOUT_SETTING, 90)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 90
    return max(5, min(value, 600))


def _rows(stdout: str) -> list[dict[str, Any]]:
    out = []
    for line in (stdout or "").splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _tool_result_text(rows: list[dict[str, Any]]) -> str:
    """The native WebSearch tool_result payload, verbatim.

    Results are untrusted text and are handed to Hermes as-is: no re-ranking, trimming or rewriting
    here, so the transcript shows what the subscription actually returned.
    """
    for row in rows:
        if row.get("type") != "user":
            continue
        for block in (row.get("message") or {}).get("content") or []:
            if block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
                if text.strip():
                    return text
    return ""


class ClaudeNativeWebSearchProvider:
    """``web_search`` backend served by the user's Claude subscription via Claude Code."""

    name = PROVIDER_NAME
    display_name = "Claude Subscription WebSearch"

    def is_available(self) -> bool:
        # Cheap, no network: the opt-in flag and a resolvable CLI.
        if not native_web_search_enabled():
            return False
        try:
            return _resolve(None, dict(os.environ)) is not None
        except Exception:
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        if not native_web_search_enabled():
            return {"success": False, "error": "native_web_search is off for this profile"}
        resolved = _resolve(None, dict(os.environ))
        if not resolved:
            return {"success": False, "error": "Claude Code CLI not found; install @anthropic-ai/claude-code"}
        prompt = (f"Use WebSearch to search the web for: {query}\n"
                  f"Return up to {max(1, int(limit))} results as titles and URLs. Do not summarise.")
        try:
            stdout = _probe(list(resolved) + SEARCH_FLAGS, input=prompt,
                            env=_child_env(dict(os.environ)), timeout=_timeout(),
                            prefix="claude-directsdk-websearch-")
        except Exception as exc:
            logger.debug("native WebSearch call failed: %s", exc)
            return {"success": False, "error": f"native WebSearch call failed: {exc}"}
        rows = _rows(stdout)
        result = next((r for r in rows if r.get("type") == "result"), {})
        if result.get("is_error"):
            return {"success": False, "error": str(result.get("result") or "native WebSearch returned an error")}
        text = _tool_result_text(rows)
        if not text:
            return {"success": False, "error": "native WebSearch returned no results"}
        return {"success": True, "data": {"web": [
            {"url": "", "title": "Claude Subscription WebSearch", "description": text, "position": 1}]}}
