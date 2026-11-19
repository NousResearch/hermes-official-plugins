"""Canonical chains document builder; explanation text cannot change the graph."""
from ..errors import ValidationError
from .compose import compose
from .eligibility import _context, _target_defeats, eligible_edges
from .vocabulary import VOCABULARY_VERSION


def build_chains_doc(scan_id, findings, *, snapshot_digest, explanations=None):
    """Build a deterministic chains.json document without I/O or model calls.

    ignoredExplanations contains identifiers/reasons, never untrusted rejected
    payloads. Broken entries are minimal defeated transitions, not inflated chains.
    """
    actual_scan, _ = _context(findings, snapshot_digest)
    if actual_scan is not None and actual_scan != scan_id:
        raise ValidationError("Requested scanId does not match chain findings")
    edges = eligible_edges(findings, snapshot_digest=snapshot_digest)
    chains = compose(findings, edges)
    by_id = {f["findingId"]: f for f in findings}
    broken = [{"findingIds": [e["from"], e["to"]], "edge": e,
               "defeatedBy": "; ".join(_target_defeats(by_id[e["to"]]))}
              for e in edges if e["state"] == "broken"]
    result = {"documentType": "hermes-security.chains", "schemaVersion": "1.0", "scanId": scan_id,
              "vocabularyVersion": VOCABULARY_VERSION, "chains": chains, "broken": broken}
    if explanations is not None:
        ignored = []
        if not isinstance(explanations, dict):
            ignored.append({"reason": "explanations must be an object"})
        else:
            by_chain = {c["chainId"]: c for c in chains}
            for cid in sorted(explanations, key=str):
                fields = explanations[cid]
                if cid not in by_chain:
                    ignored.append({"chainId": cid, "reason": "unknown chainId"})
                    continue
                if not isinstance(fields, dict):
                    ignored.append({"chainId": cid, "reason": "explanation must be an object"})
                    continue
                for key in sorted(fields, key=str):
                    value = fields[key]
                    if key in {"title", "summary"} and isinstance(value, str) and 0 < len(value.strip()) <= 4000:
                        by_chain[cid][key] = value
                    else:
                        ignored.append({"chainId": cid, "key": key, "reason": "only nonempty title/summary text is allowed"})
        result["ignoredExplanations"] = ignored
    return result
