"""
Feishu document comment access-control rules.

3-tier rule resolution: exact doc > wildcard "*" > top-level > code defaults.
Each field (enabled/policy/allow_from) falls back independently.
Config: ~/.hermes/feishu_comment_rules.json (mtime-cached, hot-reload).
Pairing store: ~/.hermes/feishu_comment_pairing.json.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Resolved at import time: this module is lazy-imported by the comment event handler,
# long after profile/HERMES_HOME overrides have been applied, so freezing is safe.
RULES_FILE = get_hermes_home() / "feishu_comment_rules.json"
PAIRING_FILE = get_hermes_home() / "feishu_comment_pairing.json"

_VALID_POLICIES = ("allowlist", "pairing")


@dataclass(frozen=True)
class CommentDocumentRule:
    """Per-document rule.  ``None`` means 'inherit from lower tier'."""
    enabled: Optional[bool] = None
    policy: Optional[str] = None
    allow_from: Optional[frozenset] = None


@dataclass(frozen=True)
class CommentsConfig:
    """Top-level comment access config."""
    enabled: bool = True
    policy: str = "pairing"
    allow_from: frozenset = field(default_factory=frozenset)
    documents: Dict[str, CommentDocumentRule] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedCommentRule:
    """Fully resolved rule after field-by-field fallback."""
    enabled: bool
    policy: str
    allow_from: frozenset
    match_source: str  # e.g. "exact:docx:xxx" | "wildcard" | "top"


class _MtimeCache:
    """Mtime-based JSON file cache: ``stat()`` per access, re-read only on change."""

    def __init__(self, path: Path):
        self._path = path
        self._mtime: float = 0.0
        self._data: Optional[dict] = None

    def load(self) -> dict:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._mtime = 0.0
            self._data = {}
            return {}
        if mtime == self._mtime and self._data is not None:
            return self._data
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            logger.warning("[Feishu-Rules] Failed to read %s, using empty config", self._path)
            data = {}
        self._mtime = mtime
        self._data = data
        return data


_rules_cache = _MtimeCache(RULES_FILE)
_pairing_cache = _MtimeCache(PAIRING_FILE)


# --- Config parsing ---

def _parse_frozenset(raw: Any) -> Optional[frozenset]:
    """Parse a list of strings into a frozenset; None if absent or not a list."""
    if isinstance(raw, (list, tuple)):
        return frozenset(str(u).strip() for u in raw if str(u).strip())
    return None


def _parse_policy(raw: Any, default: Optional[str]) -> Optional[str]:
    """Normalize a policy value; unknown/invalid values fall back to *default*."""
    if raw is None:
        return default
    policy = str(raw).strip().lower()
    return policy if policy in _VALID_POLICIES else default


def _parse_document_rule(raw: dict) -> CommentDocumentRule:
    enabled = raw.get("enabled")
    return CommentDocumentRule(
        enabled=None if enabled is None else bool(enabled),
        policy=_parse_policy(raw.get("policy"), None),
        allow_from=_parse_frozenset(raw.get("allow_from")),
    )


def load_config() -> CommentsConfig:
    """Load comment rules from disk (mtime-cached)."""
    raw = _rules_cache.load()
    if not raw:
        return CommentsConfig()
    raw_docs = raw.get("documents", {})
    documents = {
        str(key): _parse_document_rule(rule_raw)
        for key, rule_raw in (raw_docs.items() if isinstance(raw_docs, dict) else ())
        if isinstance(rule_raw, dict)
    }
    return CommentsConfig(
        enabled=raw.get("enabled", True),
        policy=_parse_policy(raw.get("policy", "pairing"), "pairing"),
        allow_from=_parse_frozenset(raw.get("allow_from")) or frozenset(), documents=documents,
    )


# --- Rule resolution (field-by-field fallback) ---

def has_wiki_keys(cfg: CommentsConfig) -> bool:
    """Check if any document rule key starts with 'wiki:'."""
    return any(k.startswith("wiki:") for k in cfg.documents)


def resolve_rule(cfg: CommentsConfig, file_type: str, file_token: str, wiki_token: str = "") -> ResolvedCommentRule:
    """Resolve effective rule: exact doc → wiki key → wildcard → top-level → defaults."""
    exact_key = f"{file_type}:{file_token}"
    exact = cfg.documents.get(exact_key)
    if exact is None and wiki_token:
        exact_key = f"wiki:{wiki_token}"
        exact = cfg.documents.get(exact_key)
    layers = [(exact, f"exact:{exact_key}"), (cfg.documents.get("*"), "wildcard")]

    def _pick(field_name: str):
        # First non-None document-layer value wins; otherwise the top-level value (even if None).
        for layer, src in layers:
            if layer is not None and getattr(layer, field_name) is not None:
                return getattr(layer, field_name), src
        return getattr(cfg, field_name), "top"

    enabled, en_src = _pick("enabled")
    policy, pol_src = _pick("policy")
    allow_from, _ = _pick("allow_from")
    # match_source = highest-priority tier that contributed enabled or policy
    priority_order = {"exact": 0, "wildcard": 1, "top": 2}
    best_src = min([en_src, pol_src], key=lambda s: priority_order.get(s.split(":")[0], 3))
    return ResolvedCommentRule(enabled=enabled, policy=policy, allow_from=allow_from, match_source=best_src)


# --- Pairing store ---

def _load_pairing_approved() -> set:
    """Return set of approved user open_ids (mtime-cached)."""
    approved = _pairing_cache.load().get("approved", {})
    if isinstance(approved, dict):
        return set(approved.keys())
    if isinstance(approved, list):
        return {str(u) for u in approved if u}
    return set()


def _save_pairing(data: dict) -> None:
    PAIRING_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAIRING_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(PAIRING_FILE)
    _pairing_cache._mtime = 0.0  # invalidate so the next load re-reads
    _pairing_cache._data = None


def _mutate_pairing(user_open_id: str, add: bool) -> bool:
    """Add/remove *user_open_id* in the approved dict; True when the store actually changed."""
    data = _pairing_cache.load()
    approved = data.get("approved", {})
    if not isinstance(approved, dict):
        if not add:
            return False
        approved = {}
    if (user_open_id in approved) == add:
        return False
    if add:
        approved[user_open_id] = {"approved_at": time.time()}
    else:
        del approved[user_open_id]
    data["approved"] = approved
    _save_pairing(data)
    return True


def pairing_add(user_open_id: str) -> bool:
    """Add a user to the pairing-approved list. Returns True if newly added."""
    return _mutate_pairing(user_open_id, add=True)


def pairing_remove(user_open_id: str) -> bool:
    """Remove a user from the pairing-approved list. Returns True if removed."""
    return _mutate_pairing(user_open_id, add=False)


def pairing_list() -> Dict[str, Any]:
    """Return the approved dict  {user_open_id: {approved_at: ...}}."""
    approved = _pairing_cache.load().get("approved", {})
    return dict(approved) if isinstance(approved, dict) else {}


# --- Access check (public API for feishu_comment.py) ---

def is_user_allowed(rule: ResolvedCommentRule, user_open_id: str) -> bool:
    """Check if user passes the resolved rule's policy gate."""
    if user_open_id in rule.allow_from:
        return True
    if rule.policy == "pairing":
        return user_open_id in _load_pairing_approved()
    return False


# --- CLI ---

def _print_status() -> None:
    cfg = load_config()
    print(f"Rules file: {RULES_FILE}\n  exists: {RULES_FILE.exists()}")
    print(f"Pairing file: {PAIRING_FILE}\n  exists: {PAIRING_FILE.exists()}\n")
    print(f"Top-level:\n  enabled:    {cfg.enabled}\n  policy:     {cfg.policy}")
    print(f"  allow_from: {sorted(cfg.allow_from) if cfg.allow_from else '[]'}\n")
    if cfg.documents:
        print(f"Document rules ({len(cfg.documents)}):")
        for key, rule in sorted(cfg.documents.items()):
            fields = (("enabled", rule.enabled), ("policy", rule.policy),
                      ("allow_from", sorted(rule.allow_from) if rule.allow_from is not None else None))
            parts = [f"{name}={value}" for name, value in fields if value is not None]
            print(f"  [{key}] {', '.join(parts) if parts else '(empty — inherits all)'}")
    else:
        print("Document rules: (none)")
    print()
    approved = pairing_list()
    print(f"Pairing approved ({len(approved)}):")
    for uid, meta in sorted(approved.items()):
        print(f"  {uid}  (approved_at={meta.get('approved_at', 0)})")


def _do_check(doc_key: str, user_open_id: str) -> None:
    cfg = load_config()
    parts = doc_key.split(":", 1)
    if len(parts) != 2:
        print(f"Error: doc_key must be 'fileType:fileToken', got '{doc_key}'")
        return
    rule = resolve_rule(cfg, parts[0], parts[1])
    allowed = is_user_allowed(rule, user_open_id)
    print(f"Document:     {doc_key}\nUser:         {user_open_id}\nResolved rule:")
    print(f"  enabled:      {rule.enabled}\n  policy:       {rule.policy}")
    print(f"  allow_from:   {sorted(rule.allow_from) if rule.allow_from else '[]'}")
    print(f"  match_source: {rule.match_source}\nResult:       {'ALLOWED' if allowed else 'DENIED'}")


def _pairing_cmd(args: list) -> int:
    """Handle ``pairing <add|remove|list> [user]``; returns the exit code."""
    if len(args) < 2:
        print("Usage: pairing <add|remove|list> [args]")
        return 1
    sub = args[1]
    if sub == "list":
        approved = pairing_list()
        if not approved:
            print("(no approved users)")
        for uid, meta in sorted(approved.items()):
            print(f"  {uid}  approved_at={meta.get('approved_at', '?')}")
        return 0
    ops = {"add": (pairing_add, "Added: {}", "Already approved: {}"),
           "remove": (pairing_remove, "Removed: {}", "Not in approved list: {}")}
    if sub not in ops:
        print(f"Unknown pairing subcommand: {sub}")
        return 1
    if len(args) < 3:
        print(f"Usage: pairing {sub} <user_open_id>")
        return 1
    fn, ok_msg, noop_msg = ops[sub]
    print((ok_msg if fn(args[2]) else noop_msg).format(args[2]))
    return 0


def _main() -> int:
    try:
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv()
    except Exception:
        pass
    usage = (
        "Usage: python -m gateway.platforms.feishu_comment_rules <command> [args]\n"
        "\n"
        "Commands:\n"
        "  status                              Show rules config and pairing state\n"
        "  check <fileType:token> <user>        Simulate access check\n"
        "  pairing add <user_open_id>           Add user to pairing-approved list\n"
        "  pairing remove <user_open_id>        Remove user from pairing-approved list\n"
        "  pairing list                         List pairing-approved users\n"
        "\n"
        f"Rules config file: {RULES_FILE}\n"
        "  Edit this JSON file directly to configure policies and document rules.\n"
        "  Changes take effect on the next comment event (no restart needed).\n"
    )
    args = sys.argv[1:]
    if not args:
        print(usage)
        return 1
    cmd = args[0]
    if cmd == "status":
        _print_status()
    elif cmd == "check":
        if len(args) < 3:
            print("Usage: check <fileType:fileToken> <user_open_id>")
            return 1
        _do_check(args[1], args[2])
    elif cmd == "pairing":
        return _pairing_cmd(args)
    else:
        print(f"Unknown command: {cmd}\n")
        print(usage)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
