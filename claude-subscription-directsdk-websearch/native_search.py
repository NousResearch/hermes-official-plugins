"""``web_search`` served by the user's Claude subscription: one isolated ``claude -p --tools WebSearch`` call.

Opt-in (``native_web_search`` in the claude-subscription-directsdk plugin's settings), separate from the
main turn, and it never touches the directsdk relay. The call has exactly one native tool, an empty MCP
config under ``--strict-mcp-config``, its own scratch working directory and no Hermes tools.

Rows come from the CLI's structured WebSearch result (``tool_use_result``), mapped field for field into
Hermes' ``{title, url, description, position}`` schema. No model text is returned: the process is stopped as
soon as the tool result arrives, before Claude writes its own summary, and nothing here ranks, trims or
rewrites a title or URL. The titles and URLs are untrusted text, exactly like any other provider's.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from typing import Any

try:  # real Hermes
    from agent.web_search_provider import WebSearchProvider
except ImportError:  # pragma: no cover - unit tests without core
    class WebSearchProvider:  # type: ignore[no-redef]
        pass

logger = logging.getLogger(__name__)

PROVIDER_NAME = "claude-subscription-websearch"
# The settings live with the model provider, in the same profile entry.
DIRECTSDK_PLUGIN_ID = "claude-subscription-directsdk-experimental"
ENABLE_SETTING = "native_web_search"
MODEL_SETTING = "native_web_search_model"
TIMEOUT_SETTING = "native_web_search_timeout"
CONFIG_DIR_SETTING = "claude_config_dir"
DEFAULT_MODEL = "haiku"
DEFAULT_TIMEOUT = 60
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,63}$")

# One native tool, no settings sources, no MCP servers of any kind, no slash commands, no stored session.
SEARCH_FLAGS = (
    "-p", "--tools", "WebSearch", "--allowedTools", "WebSearch",
    "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--disable-slash-commands", "--no-session-persistence",
    "--output-format", "stream-json", "--verbose",
)
PROMPT = ("Call the WebSearch tool immediately with exactly this query, unchanged, and nothing else. "
          "Do not summarise.\nQuery: ")

# Never inherited: an API credential would bill the API instead of the subscription.
API_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY")
# A gateway or cloud-backend override would send this call somewhere the user never opted into.
BACKEND_OVERRIDES = ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                     "CLAUDE_CODE_USE_FOUNDRY")
QUIET = {"DISABLE_AUTOUPDATER": "1", "DISABLE_FEEDBACK_COMMAND": "1", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
         "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
         "CLAUDE_CODE_ENTRYPOINT": "hermes-agent", "CLAUDE_CODE_DISABLE_ARTIFACT": "1"}
_HOME_PREFIXES = (".local/bin", ".npm-global/bin", ".claude/local", "node_modules/.bin")
_SYSTEM_PREFIXES = ("/usr/local/bin", "/opt/homebrew/bin")


def _setting(key: str, default: Any = None) -> Any:
    """One setting of the active profile, read per call (a change applies to the next search)."""
    try:
        from hermes_cli.config import load_config_readonly
        value: Any = load_config_readonly()
    except (ImportError, OSError, ValueError):
        return default
    for segment in ("plugins", "entries", DIRECTSDK_PLUGIN_ID, "settings", key):
        if not isinstance(value, Mapping) or segment not in value:
            return default
        value = value[segment]
    return value


def enabled() -> bool:
    value = _setting(ENABLE_SETTING, False)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return isinstance(value, (bool, int)) and bool(value)


def _model() -> str:
    value = _setting(MODEL_SETTING, DEFAULT_MODEL)
    value = value.strip() if isinstance(value, str) else ""
    return value if _MODEL_RE.match(value) else DEFAULT_MODEL


def _timeout() -> float:
    try:
        return float(max(5, min(int(_setting(TIMEOUT_SETTING, DEFAULT_TIMEOUT)), 300)))
    except (TypeError, ValueError):
        return float(DEFAULT_TIMEOUT)


def _resolve_cli(env: Mapping[str, str]) -> str | None:
    head = env.get("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND") or "claude"
    if os.path.isabs(head):
        return head if os.access(head, os.X_OK) else None
    found = shutil.which(head, path=env.get("PATH") or os.defpath)
    if found or head != "claude":
        return found
    home = env.get("USERPROFILE" if os.name == "nt" else "HOME")
    prefixes = [os.path.join(home, p) for p in _HOME_PREFIXES] if home and os.path.isabs(home) else []
    prefixes += [] if os.name == "nt" else list(_SYSTEM_PREFIXES)
    return shutil.which(head, path=os.pathsep.join(prefixes))


def _child_env(env: Mapping[str, str]) -> dict[str, str]:
    """The search child's environment: the profile's own Claude login, never an API key."""
    child = {k: v for k, v in env.items() if k not in API_CREDENTIALS}
    override = child.pop("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", None)
    setting = _setting(CONFIG_DIR_SETTING)
    config = (setting.strip() if isinstance(setting, str) else "") or override or child.get("CLAUDE_CONFIG_DIR")
    if config:
        child["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config)
    try:  # the profile's own token (core's secret scope), as the model provider reads it
        from agent.secret_scope import get_secret
        token = get_secret("CLAUDE_CODE_OAUTH_TOKEN")
    except ImportError:  # a core without the secret scope: the process environment stands
        token = None
    if isinstance(token, str) and token.strip():
        child["CLAUDE_CODE_OAUTH_TOKEN"] = token.strip()
    child.update(QUIET)
    return child


def _kill_group(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        elif process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _rows_from_links_text(text: str) -> list[dict[str, str]]:
    """Fallback for a CLI that omits the structured result: the ``Links: [...]`` JSON line of the tool text."""
    match = re.search(r"^Links:\s*(\[.*\])\s*$", text or "", re.MULTILINE)
    if not match:
        return []
    try:
        items = json.loads(match.group(1))
    except ValueError:
        return []
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _links(row: Mapping[str, Any]) -> list[dict[str, str]] | None:
    """The ``{title, url}`` entries of one stream-json ``user`` row carrying the WebSearch tool result, else None."""
    blocks = [b for b in (row.get("message") or {}).get("content") or []
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    if not blocks:
        return None
    structured = row.get("tool_use_result")
    links: list[dict[str, str]] = []
    if isinstance(structured, Mapping):
        for entry in structured.get("results") or []:
            # A dict entry is the search hits; a plain string entry is model-written commentary and is dropped.
            if isinstance(entry, Mapping) and isinstance(entry.get("content"), list):
                links += [i for i in entry["content"] if isinstance(i, Mapping)]
    if not links:
        for block in blocks:
            content = block.get("content")
            text = content if isinstance(content, str) else "".join(
                b.get("text", "") for b in content or [] if isinstance(b, Mapping))
            links += _rows_from_links_text(text)
    return links


def _web_rows(links: list[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in links:
        url, title = item.get("url"), item.get("title")
        if not isinstance(url, str) or not url:
            continue
        out.append({"title": title if isinstance(title, str) else "", "url": url,
                    "description": "", "position": len(out) + 1})
        if len(out) >= limit:
            break
    return out


def run_search(argv: list[str], query: str, env: dict[str, str], timeout: float) -> dict[str, Any]:
    """Run one search; stop the child the moment its WebSearch result arrives.

    Returns ``{"links": [...], "searched": <query the tool ran>, "timing": {...}}`` or ``{"error": str}``.
    """
    scratch = tempfile.TemporaryDirectory(prefix="claude-directsdk-websearch-", ignore_cleanup_errors=True)
    started = time.monotonic()
    process = None
    searched = None
    timing: dict[str, float] = {}
    timed_out = threading.Event()
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", cwd=scratch.name, env=env,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}))

        def _expire() -> None:
            timed_out.set()
            _kill_group(process)

        watchdog = threading.Timer(timeout, _expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                process.stdin.write(PROMPT + query)
                process.stdin.close()
            except OSError:
                pass  # exited without reading; its output still counts
            for line in process.stdout:
                if not line.startswith("{"):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                kind = row.get("type")
                if kind == "assistant":
                    for block in (row.get("message") or {}).get("content") or []:
                        if isinstance(block, Mapping) and block.get("type") == "tool_use" and block.get("name") == "WebSearch":
                            timing["tool_use_s"] = round(time.monotonic() - started, 2)
                            searched = (block.get("input") or {}).get("query")
                elif kind == "user":
                    links = _links(row)
                    if links is not None:
                        timing["tool_result_s"] = round(time.monotonic() - started, 2)
                        _kill_group(process)  # before Claude's own summary turn
                        return {"links": links, "searched": searched, "timing": timing}
                elif kind == "result":
                    return {"error": str(row.get("result") or "native WebSearch ended without a search result")}
        finally:
            watchdog.cancel()
        if timed_out.is_set():
            return {"error": f"native WebSearch timed out after {timeout:.0f}s"}
        return {"error": "native WebSearch ended without a search result (is Claude Code logged in?)"}
    finally:
        if process is not None:
            _kill_group(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        scratch.cleanup()


class ClaudeSubscriptionWebSearchProvider(WebSearchProvider):
    """``web_search`` backend served by the user's Claude subscription through Claude Code's WebSearch tool."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Claude Subscription WebSearch"

    def is_available(self) -> bool:
        # No network: the opt-in setting and a resolvable CLI.
        return enabled() and _resolve_cli(os.environ) is not None

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        if not enabled():
            return {"success": False, "error": "native_web_search is off for this profile"}
        env = _child_env(os.environ)
        if any(env.get(key) for key in BACKEND_OVERRIDES):
            return {"success": False, "error": "refused: an inherited ANTHROPIC_BASE_URL / backend switch would send "
                    "this search somewhere other than your Claude subscription; unset it"}
        exe = _resolve_cli(env)
        if not exe:
            return {"success": False, "error": "Claude Code CLI not found; install @anthropic-ai/claude-code"}
        model = _model()
        started = time.monotonic()
        outcome = run_search([exe, "--model", model, *SEARCH_FLAGS], query, env, _timeout())
        if "error" in outcome:
            logger.info("Claude WebSearch query=%r failed after %.1fs: %s", query, time.monotonic() - started, outcome["error"])
            return {"success": False, "error": outcome["error"]}
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 5
        rows = _web_rows(outcome["links"], limit)
        logger.info("Claude WebSearch query=%r model=%s results=%d (%.1fs; tool_use %s, result %s)",
                    query, model, len(rows), time.monotonic() - started,
                    outcome["timing"].get("tool_use_s"), outcome["timing"].get("tool_result_s"))
        if outcome.get("searched") not in (None, query):
            logger.info("Claude WebSearch ran %r for query %r", outcome["searched"], query)
        return {"success": True, "data": {"web": rows}}
