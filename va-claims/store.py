"""Local benefits store shared by the va-claims tools and the dashboard API.

One JSON file per veteran under ~/.hermes/plugins-data/va-claims/.
Tracks: disability claims, education (Post-9/11 GI Bill), health coverage
(VHA enrollment, CHAMPVA), plus deadlines and notes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INTENT_NOTICE = (
    "Local planning record only: NOT submitted to VA. Saving this record does not "
    "establish an effective date or start a 12-month filing period. Submit separately "
    "through VA's official channels and confirm VA receipt and the applicable deadline."
)

PROGRAMS = ("disability", "education", "health", "champva", "life_insurance", "pension", "voc_rehab")


def _root() -> Path:
    try:
        from hermes_constants import get_hermes_home

        base = get_hermes_home()
    except Exception:
        base = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    d = Path(base) / "plugins-data" / "va-claims"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_icn(icn: str | None) -> str:
    safe = "".join(c for c in (icn or "default") if c.isalnum())
    return safe or "default"


def claim_file(icn: str | None) -> Path:
    return _root() / f"claim-{safe_icn(icn)}.json"


def load(icn: str | None) -> dict[str, Any]:
    p = claim_file(icn)
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("icn", icn or "default")
    data.setdefault("intent_to_file", [])
    data.setdefault("snapshots", [])
    data.setdefault("notes", [])
    data.setdefault("benefits", {})
    data.setdefault("deadlines", [])
    return data


def save(icn: str | None, data: dict[str, Any]) -> str:
    p = claim_file(icn)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    return str(p)


def intent_tracking_view(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project local plans safely without rewriting historical records on disk.

    Older versions manufactured filed_on/expires_on and a matching deadline.
    Those dates are not VA receipt evidence; keep the raw history but do not
    present its generated deadline as a confirmed filing period.
    """
    intents = []
    legacy_deadlines = set()
    for entry in data.get("intent_to_file", []):
        if "filed_on" in entry:
            legacy_deadlines.add((
                f"Complete claim for intent filed {entry['filed_on']}",
                entry.get("expires_on"),
            ))
        intents.append({
            "recorded_on": entry.get("recorded_on") or entry.get("filed_on"),
            "conditions": entry.get("conditions", []),
            "note": entry.get("note", ""),
            "status": "local_plan_only",
            "submitted_to_va": False,
            "legacy_record": "filed_on" in entry,
        })
    deadlines = [
        d for d in data.get("deadlines", [])
        if not (
            (d.get("label"), d.get("due")) in legacy_deadlines
            and d.get("program") == "disability"
            and d.get("note") == "38 CFR 3.155(b): miss this and the effective date is lost"
        )
    ]
    return intents, deadlines


def list_people() -> list[dict[str, Any]]:
    out = []
    for p in sorted(_root().glob("claim-*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        benefits = d.get("benefits", {})
        out.append(
            {
                "icn": d.get("icn", p.stem),
                "programs": sorted(benefits.keys()),
                "open_claims": sum(1 for s in d.get("snapshots", []) if (s.get("status") or "").upper() not in ("COMPLETE", "CANCELED", "ERRORED")),
                "deadlines": len(intent_tracking_view(d)[1]),
                "updated": max([s.get("taken_at", "") for s in d.get("snapshots", [])] or [""]),
            }
        )
    return out


def set_benefit(icn: str | None, program: str, fields: dict[str, Any]) -> dict[str, Any]:
    if program not in PROGRAMS:
        raise ValueError(f"Unknown program '{program}'. Valid: {', '.join(PROGRAMS)}")
    data = load(icn)
    entry = dict(data["benefits"].get(program, {}))
    data["benefits"][program] = entry
    entry.update({k: v for k, v in fields.items() if v is not None})
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["benefits"][program] = entry
    return data


def add_deadline(icn: str | None, label: str, due: str, program: str = "disability", note: str = "") -> dict[str, Any]:
    data = load(icn)
    data["deadlines"] = [d for d in data["deadlines"] if d.get("label") != label]
    data["deadlines"].append({"label": label, "due": due, "program": program, "note": note, "added": datetime.now(timezone.utc).isoformat()})
    data["deadlines"].sort(key=lambda d: d.get("due") or "9999")
    return data


def summary(icn: str | None) -> dict[str, Any]:
    data = load(icn)
    intents, deadlines = intent_tracking_view(data)
    snaps = data.get("snapshots", [])
    latest: dict[str, Any] = {}
    for s in snaps:
        latest[s["claim_id"]] = s
    open_claims = [s for s in latest.values() if (s.get("status") or "").upper() not in ("COMPLETE", "CANCELED", "ERRORED")]
    today = datetime.now(timezone.utc).date()
    upcoming = []
    for d in deadlines:
        try:
            due = datetime.strptime(str(d.get("due"))[:10], "%Y-%m-%d").date()
            d = {**d, "days_left": (due - today).days}
        except Exception:
            continue
        upcoming.append(d)
    return {
        "icn": data.get("icn"),
        "programs": sorted(data.get("benefits", {}).keys()),
        "benefits": data.get("benefits", {}),
        "open_claims": open_claims,
        "closed_claims": [s for s in latest.values() if s not in open_claims],
        "intents": intents,
        "intent_notice": INTENT_NOTICE if intents else None,
        "deadlines": upcoming,
        "file": str(claim_file(icn)),
    }
