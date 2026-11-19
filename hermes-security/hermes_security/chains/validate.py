"""Independent edge revalidation and final-impact severity policy."""
from copy import deepcopy

from ..canonical import canonical_json, stable_id
from ..errors import ValidationError
from .eligibility import _context, _evidence, _satisfies, _valid, eligible_edges
from .vocabulary import EFFECTS, PARTICIPATING_STATES, SEVERITIES


def chain_severity(chain, findings_by_id):
    """Never add severities. Conditional/broken chains retain primitive maximum.

    The last finding's declared effects establish final impact; only code execution,
    privilege gain, or cross-tenant credential acquisition justify a one-band lift.
    """
    rows = [findings_by_id[fid] for fid in chain["findingIds"]]
    try:
        maximum = min(SEVERITIES.index(row["severity"]["level"]) for row in rows)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError("Invalid primitive severity") from exc
    effects = [e for e in rows[-1].get("capabilities", {}).get("effects", []) if _valid(e, EFFECTS)]
    conditional = chain.get("conditional", False) or any(e.get("state") != "supported" or e.get("assumptions") for e in chain.get("edges", []))
    conditional = conditional or any(row.get("evidenceState") == "candidate" for row in rows)
    lifted = not conditional and any(e.get("kind") in {"code-exec", "privilege-gain"} or
        (e.get("kind") == "credential-acquire" and e.get("tenant") == "cross") for e in effects)
    index = max(0, maximum - 1) if lifted else maximum
    impact = ", ".join(sorted({e.get("kind", "unknown") for e in effects})) or "no declared final effect"
    reason = f"Final reachable impact: {impact}. "
    reason += ("Conditional or broken path: retained maximum primitive severity; no uplift." if conditional else
               "One-band uplift capped above maximum primitive severity." if lifted and maximum else
               "Retained maximum primitive severity; severities are not summed.")
    return {"level": SEVERITIES[index], "rationale": reason}


def validate_chain(chain, findings_by_id):
    """Recompute eligibility, citations, state, identity and severity; reject invention."""
    ids = chain.get("findingIds", [])
    if not isinstance(ids, list) or not 2 <= len(ids) <= 4 or len(ids) != len(set(ids)):
        raise ValidationError("Chains require 2..4 distinct findings")
    if any(fid not in findings_by_id for fid in ids):
        raise ValidationError("Chain references an unknown finding")
    rows = [findings_by_id[fid] for fid in ids]
    if any(row.get("evidenceState") not in PARTICIPATING_STATES for row in rows):
        raise ValidationError("Rejected or inconclusive findings cannot participate")
    _, snapshot = _context(rows)
    eligible = eligible_edges(rows, snapshot_digest=snapshot)
    supplied = chain.get("edges", [])
    if len(supplied) != len(ids) - 1:
        raise ValidationError("Every adjacent step requires exactly one edge")
    validated = []
    for index, edge in enumerate(supplied):
        if edge.get("from") != ids[index] or edge.get("to") != ids[index + 1]:
            raise ValidationError("Edge endpoints do not match chain order")
        matches = [e for e in eligible if all(e[k] == edge.get(k) for k in ("from", "to", "effect", "precondition"))]
        if not matches:
            raise ValidationError("Chain contains an ineligible or invented edge")
        refs = edge.get("evidenceRefs", [])
        allowed = set(_evidence(rows[index]) + _evidence(rows[index + 1]))
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
            raise ValidationError("Edge evidenceRefs must resolve on its endpoints")
        actual = deepcopy(matches[0])
        actual["evidenceRefs"] = sorted(set(refs))
        # A caller may weaken evidence, never strengthen it.
        if actual["state"] != "broken" and (edge.get("state") == "speculative" or
            not all(any(ref.startswith(fid + "#") for ref in refs) for fid in (ids[index], ids[index + 1]))):
            actual["state"] = "speculative"
        # Other required preconditions remain explicit gaps unless earlier effects establish them.
        prior_effects = [eff for row in rows[:index + 1] for eff in row.get("capabilities", {}).get("effects", [])]
        unresolved = [p for p in rows[index + 1].get("capabilities", {}).get("preconditions", [])
                      if not any(_satisfies(eff, p) for eff in prior_effects)]
        if unresolved:
            actual["assumptions"] = sorted(set(actual["assumptions"] +
                ["Unestablished precondition: " + canonical_json(p) for p in unresolved]))
            if actual["state"] != "broken":
                actual["state"] = "speculative"
        validated.append(actual)
    result = deepcopy(chain)
    result["edges"] = validated
    result["chainId"] = stable_id("chn", rows[0].get("scanId", rows[0].get("provenance", {}).get("scanId")), snapshot, ids)
    broken = any(e["state"] == "broken" for e in validated)
    conditional = any(e["state"] == "speculative" for e in validated)
    result["conditional"] = conditional or broken
    result["disposition"] = ("broken_chain" if broken else "candidate_chain" if conditional else
                             "runtime_confirmed_chain" if all(r["evidenceState"] == "runtime_confirmed" for r in rows) else
                             "source_supported_chain")
    result["severity"] = chain_severity(result, findings_by_id)
    result.setdefault("title", " → ".join(ids))
    result.setdefault("summary", "Established effects satisfy the next step's declared prerequisites.")
    return result
