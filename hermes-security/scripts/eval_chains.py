#!/usr/bin/env python3
"""Offline hand-labeled chain canaries; exits nonzero on any mismatch/invention."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hermes_security.chains import build_chains_doc


def main():
    true_positive = false_positive = false_negative = invented = 0
    failures = []
    files = sorted((ROOT / "evals" / "chains").glob("*.json"))
    for path in files:
        case = json.loads(path.read_text())
        actual = build_chains_doc(case["scanId"], case["findings"], snapshot_digest=case["snapshotDigest"])
        expected = case["expected"]
        want = {(kind, tuple(ids)) for kind in ("chains", "broken") for ids in expected[kind]}
        got = {(kind, tuple(row["findingIds"])) for kind in ("chains", "broken") for row in actual[kind]}
        true_positive += len(want & got)
        false_positive += len(got - want)
        false_negative += len(want - got)
        allowed = {tuple(edge) for edge in expected["allowedEdges"]}
        observed_edges = [edge for chain in actual["chains"] for edge in chain["edges"]]
        observed_edges += [row["edge"] for row in actual["broken"]]
        illegal = {(e["from"], e["to"], e["effect"]["kind"], e["precondition"]["kind"])
                   for e in observed_edges} - allowed
        invented += len(illegal)
        if want != got or illegal:
            failures.append(path.name)
        for chain in actual["chains"]:
            wanted_state = expected.get("disposition")
            if wanted_state and chain["disposition"] != wanted_state:
                failures.append(path.name + ":disposition")
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    result = {"cases": len(files), "precision": precision, "recall": recall, "inventedEdgeCount": invented,
              "truePositives": true_positive, "falsePositives": false_positive, "falseNegatives": false_negative,
              "failures": failures}
    print(json.dumps(result, sort_keys=True))
    return 0 if files and not failures and precision == recall == 1.0 and invented == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
