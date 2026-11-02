"""What the Grok Bot desktop app caches on this computer, used as a checklist for the self-export.

The app keeps a UI cache in ``<user data>/sand-client-persistence/``: one plain-JSON file per
"slice" (``{"schemaVersion": n, "value": ...}``), named by the slice id in unpadded lowercase
base32 (``sand.client.slice.account.<account id>.<slice>``). Two slices are useful:

- ``roster.last-roster``: every Bot's id and name (no instructions, skills or routines);
- ``transcript.replicas.<bot id>``: a recent window of that Bot's chat, whose
  ``automation-changed`` events name routines it created (no schedule or prompt).

Routines, skills, memory and instructions live only in the Bot's cloud computer, so this cannot
replace the export; it tells the user which Bots to export and flags routines an export left out.
Login material in the same folder (``sand-secrets.json``, cookies) is never opened.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

# The app's user data folder per OS (Electron's userData for productName "Grok Bot").
USER_DATA_DIRS = ("AppData/Roaming/Grok Bot", "Library/Application Support/Grok Bot", ".config/Grok Bot")
_CACHE_DIR = "sand-client-persistence"
_PREFIX = "sand.client.slice."


@dataclass
class GrokBotCache:
    """Bot names (roster order) and, per Bot name, routine names seen in its cached chat."""
    bots: list[str] = field(default_factory=list)
    routines: dict[str, list[str]] = field(default_factory=dict)


def _slice_id(path: Path) -> Optional[str]:
    stem = path.stem.upper()
    try:
        return base64.b32decode(stem + "=" * (-len(stem) % 8)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _value(path: Path) -> Any:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # mid-rewrite or unreadable: the cache is only a hint
        return None
    return envelope.get("value") if isinstance(envelope, dict) else None


def read_cache(home: Optional[Path] = None) -> GrokBotCache:
    """The cached roster and routine names; empty when the app is not installed here."""
    cache = GrokBotCache()
    home = home or Path.home()
    folder = next((home / d / _CACHE_DIR for d in USER_DATA_DIRS if (home / d / _CACHE_DIR).is_dir()), None)
    if folder is None:
        return cache
    slices: dict[str, Path] = {}
    for path in sorted(folder.glob("*.blob")):
        sid = _slice_id(path)
        if sid and sid.startswith(_PREFIX + "account."):
            # account.<id>.<kind...>; the id is URL-encoded (a "." in it is %2E), so split first
            parts = sid[len(_PREFIX):].split(".", 2)
            if len(parts) == 3:
                slices[unquote(parts[2])] = path
    roster = _value(slices["roster.last-roster"]) if "roster.last-roster" in slices else None
    names: dict[str, str] = {}
    for row in (roster or {}).get("rows", []) if isinstance(roster, dict) else []:
        if isinstance(row, dict) and str(row.get("name") or "").strip() and not row.get("isGroup"):
            names[str(row.get("id"))] = str(row["name"]).strip()
    cache.bots = list(names.values())
    for kind, path in slices.items():
        if not kind.startswith("transcript.replicas.") or kind.rsplit(".", 1)[-1] not in names:
            continue
        seen: dict[str, str] = {}
        value = _value(path)
        for entry in (value or {}).get("entries", []) if isinstance(value, dict) else []:
            event = entry.get("event") if isinstance(entry, dict) else None
            if isinstance(event, dict) and event.get("type") == "automation-changed":
                key = str(event.get("automationId") or event.get("automationName") or "")
                if event.get("action") == "deleted":
                    seen.pop(key, None)
                elif key:
                    seen[key] = str(event.get("automationName") or key)
        if seen:
            cache.routines[names[kind.rsplit(".", 1)[-1]]] = list(seen.values())
    return cache
