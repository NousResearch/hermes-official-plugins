"""Pure, fail-closed capability matching. No prose/CWE inference."""
from copy import deepcopy

from ..canonical import canonical_json
from ..errors import ValidationError
from .vocabulary import ACTORS, ACTOR_YIELDS, EFFECTS, EFFECT_SATISFIES, PARTICIPATING_STATES, PRECONDITIONS, TENANTS


def _context(findings, snapshot_digest=None):
    scans, snapshots, ids = set(), set(), set()
    for finding in findings:
        fid = finding.get("findingId")
        if not isinstance(fid, str) or not fid or fid in ids:
            raise ValidationError("Chain findings require unique nonempty findingIds")
        ids.add(fid)
        provenance = finding.get("provenance", {})
        values = []
        for key in ("scanId", "snapshotDigest"):
            direct, inherited = finding.get(key), provenance.get(key)
            if direct and inherited and direct != inherited:
                raise ValidationError("Conflicting chain finding context")
            value = direct or inherited
            if not isinstance(value, str) or not value:
                raise ValidationError("Chain findings must carry scanId and snapshotDigest")
            values.append(value)
        scans.add(values[0])
        snapshots.add(values[1])
    if len(scans) > 1 or len(snapshots) > 1 or (snapshots and snapshot_digest is not None and snapshots != {snapshot_digest}):
        raise ValidationError("Chain findings must belong to the same scan and snapshot")
    return next(iter(scans), None), next(iter(snapshots), None)


def _flag(capability, key):
    detail = capability.get("detail")
    return capability.get(key, detail.get(key) if isinstance(detail, dict) else None)


def _valid(capability, kinds):
    return (isinstance(capability, dict) and capability.get("kind") in kinds
            and capability.get("actor") in ACTORS and capability.get("tenant") in TENANTS
            and isinstance(capability.get("deployment"), str) and bool(capability["deployment"]))


def _satisfies(effect, precondition):
    if not _valid(effect, EFFECTS) or not _valid(precondition, PRECONDITIONS):
        return False
    kind = effect["kind"]
    if precondition["kind"] not in EFFECT_SATISFIES.get(kind, ()):
        return False
    if kind == "data-read" and _flag(effect, "yieldsCredential") is not True:
        return False
    actor = precondition["actor"]
    if actor != "any" and effect["actor"] != actor:
        if actor not in ACTOR_YIELDS.get(kind, ()) or _flag(effect, "yieldsActor") != actor:
            return False
    # 'any' on an effect is unknown scope, not proof of crossing a tenant boundary.
    if precondition["tenant"] != "any" and effect["tenant"] != precondition["tenant"]:
        return False
    if effect["deployment"] != precondition["deployment"] and "any" not in (effect["deployment"], precondition["deployment"]):
        return False
    # Optional auth/role/component scopes, when required, must be established exactly.
    for key in ("authScope", "role", "credentialType", "component"):
        required = _flag(precondition, key)
        if required is not None and _flag(effect, key) != required:
            return False
    return True


def _texts(value):
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _defeats(target, precondition):
    controls = _texts(target.get("counterEvidence", [])) + _texts(target.get("attackPath", {}).get("controls", []))
    breakers = _texts(precondition.get("defeatedBy", [])) + _texts(precondition.get("brokenBy", []))
    normalize = lambda text: " ".join(text.casefold().split())
    # Exact normalized control labels only: natural-language substring inference is unsafe.
    return sorted({control for control in controls if normalize(control) in {normalize(b) for b in breakers}})


def _target_defeats(target):
    return sorted({control for pre in target.get("capabilities", {}).get("preconditions", [])
                   for control in _defeats(target, pre)})


def _evidence(finding):
    return sorted({finding["findingId"] + "#" + ev["id"] for ev in finding.get("codeEvidence", [])
                   if isinstance(ev, dict) and isinstance(ev.get("id"), str) and ev["id"] and "#" not in ev["id"]})


def eligible_edges(findings, *, snapshot_digest):
    """Return sorted eligible edges, including defeated edges marked 'broken'.

    Findings must carry scanId/snapshotDigest (top-level or provenance); a supplied
    snapshot alone cannot establish their provenance. Missing capability scopes fail
    closed. Citations on both endpoints are needed for supported status.
    """
    _context(findings, snapshot_digest)
    rows = sorted((f for f in findings if f.get("evidenceState") in PARTICIPATING_STATES), key=lambda f: f["findingId"])
    edges = {}
    for source in rows:
        for target in rows:
            if source["findingId"] == target["findingId"]:
                continue
            for effect in source.get("capabilities", {}).get("effects", []):
                for pre in target.get("capabilities", {}).get("preconditions", []):
                    if not _satisfies(effect, pre):
                        continue
                    refs_from, refs_to = _evidence(source), _evidence(target)
                    assumptions = sorted(set(_texts(source.get("attackPath", {}).get("assumptions", []))
                                             + _texts(target.get("attackPath", {}).get("assumptions", []))))
                    defeats = _target_defeats(target)
                    speculative = (not refs_from or not refs_to or assumptions or
                                   source["evidenceState"] == "candidate" or target["evidenceState"] == "candidate")
                    edge = {"from": source["findingId"], "to": target["findingId"], "effect": deepcopy(effect),
                            "precondition": deepcopy(pre), "evidenceRefs": sorted(refs_from + refs_to),
                            "counterEvidence": sorted(set(_texts(source.get("counterEvidence", [])) + _texts(target.get("counterEvidence", [])))),
                            "assumptions": assumptions, "state": "broken" if defeats else "speculative" if speculative else "supported"}
                    edges[canonical_json(edge)] = edge
    return sorted(edges.values(), key=lambda e: (e["from"], e["to"], canonical_json(e)))
