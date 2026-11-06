"""Bounded deterministic traversal; finding-order identity, no cyclic paths."""
from ..canonical import canonical_json
from ..errors import ValidationError
from .eligibility import _context
from .validate import validate_chain


def compose(findings, edges, *, max_depth=4, max_chains=50):
    """Return simple paths (including prefixes), capped at four findings/fifty paths.

    Multiple proofs for an ordered finding path are deduplicated; supported proofs
    precede speculative ones. Broken edges are handled by build_chains_doc, never
    traversed here. Every emitted path is independently revalidated.
    """
    _context(findings)
    if type(max_depth) is not int or type(max_chains) is not int or max_depth < 0 or max_chains < 0:
        raise ValidationError("Chain budgets must be nonnegative integers")
    depth, budget = min(max_depth, 4), min(max_chains, 50)
    if depth < 2 or not budget:
        return []
    by_id = {f["findingId"]: f for f in findings}
    adjacency = {}
    for edge in edges:
        if edge.get("state") == "broken":
            continue
        # Validate even non-emitted edges so cap/order cannot hide invalid inputs.
        checked = validate_chain({"findingIds": [edge.get("from"), edge.get("to")], "edges": [edge]}, by_id)
        if checked["disposition"] == "broken_chain":
            continue
        adjacency.setdefault(edge["from"], []).append(edge)
    for outgoing in adjacency.values():
        outgoing.sort(key=lambda e: (e["to"], e.get("state") != "supported", canonical_json(e)))
    results, seen = [], set()

    def walk(ids, path):
        if len(results) >= budget or len(ids) >= depth:
            return
        for edge in adjacency.get(ids[-1], []):
            if edge["to"] in ids:
                continue
            next_ids, next_path = ids + [edge["to"]], path + [edge]
            key = tuple(next_ids)
            if key not in seen:
                seen.add(key)
                results.append(validate_chain({"findingIds": next_ids, "edges": next_path}, by_id))
                if len(results) >= budget:
                    return
                walk(next_ids, next_path)
                if len(results) >= budget:
                    return

    for fid in sorted(by_id):
        walk([fid], [])
        if len(results) >= budget:
            break
    return results
