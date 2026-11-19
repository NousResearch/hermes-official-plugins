"""Fail-closed chain canaries; negatives precede positive cases."""
import copy
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import pytest
from hermes_security.chains import eligible_edges, compose, validate_chain, chain_severity, build_chains_doc
from hermes_security.errors import ValidationError

SNAP = "sha256:" + "a" * 64


def cap(kind, **kw):
    return dict(kind=kind, actor="anonymous", tenant="same", deployment="prod", detail="", **kw)


def finding(name, pre=(), effects=(), **kw):
    row = {"findingId": "hsf_" + name, "scanId": "scan_test", "snapshotDigest": SNAP,
           "severity": {"level": "medium"}, "evidenceState": "source_supported",
           "capabilities": {"preconditions": list(pre), "effects": list(effects)},
           "codeEvidence": [{"id": "ev-1"}], "counterEvidence": [],
           "attackPath": {"controls": [], "assumptions": []}, "taxonomy": {"cwe": ["CWE-918"]}}
    row.update(kw)
    return row


def pair():
    return [finding("a", effects=[cap("internal-network-reach")]),
            finding("b", pre=[cap("internal-network")], effects=[cap("code-exec")])]


def doc(rows):
    return build_chains_doc("scan_test", rows, snapshot_digest=SNAP)


def test_same_cwe_is_not_an_edge():
    rows = pair()
    rows[0]["capabilities"]["effects"] = [cap("data-write")]
    assert eligible_edges(rows, snapshot_digest=SNAP) == []


@pytest.mark.parametrize("field,value", [("tenant", "cross"), ("deployment", "other"), ("actor", "admin")])
def test_incompatible_scope(field, value):
    rows = pair()
    rows[1]["capabilities"]["preconditions"][0][field] = value
    assert doc(rows)["chains"] == []


def test_missing_auth_scope():
    rows = pair()
    del rows[0]["capabilities"]["effects"][0]["actor"]
    assert doc(rows)["chains"] == []


def test_speculative_credential_not_invented():
    rows = [finding("a", effects=[cap("data-read")]), finding("b", pre=[cap("credential")])]
    assert doc(rows)["chains"] == []


def test_broken_control_recorded_not_promoted():
    rows = pair()
    rows[1]["capabilities"]["preconditions"][0]["defeatedBy"] = "egress deny"
    rows[1]["attackPath"]["controls"] = ["egress deny"]
    result = doc(rows)
    assert not result["chains"]
    assert result["broken"][0]["defeatedBy"] == "egress deny"
    assert result["broken"][0]["edge"]["state"] == "broken"


@pytest.mark.parametrize("state", ["rejected", "inconclusive"])
def test_excluded_findings(state):
    rows = pair()
    rows[0]["evidenceState"] = state
    assert doc(rows)["chains"] == []


@pytest.mark.parametrize("field", ["scanId", "snapshotDigest"])
def test_mixed_or_missing_snapshot_rejected(field):
    rows = pair()
    rows[1][field] = "other"
    with pytest.raises(ValidationError):
        doc(rows)
    del rows[1][field]
    with pytest.raises(ValidationError):
        doc(rows)


def test_no_cycles_depth_and_count_caps():
    rows = [finding(str(i), [cap("file-write")], [cap("file-write")]) for i in range(7)]
    edges = eligible_edges(rows, snapshot_digest=SNAP)
    chains = compose(rows, edges)
    assert len(chains) == 50
    assert max(map(lambda c: len(c["findingIds"]), chains)) <= 4
    assert all(len(c["findingIds"]) == len(set(c["findingIds"])) for c in chains)
    assert compose(rows, edges, max_chains=0) == []
    assert compose(rows, edges, max_depth=2, max_chains=1)[0]["findingIds"] == ["hsf_0", "hsf_1"]


def test_llm_cannot_invent_edges():
    rows = pair()
    original = doc(rows)
    cid = original["chains"][0]["chainId"]
    result = build_chains_doc("scan_test", rows, snapshot_digest=SNAP, explanations={
        "made-up": {"title": "invented"}, cid: {"title": "Reviewed title", "edges": [], "severity": "critical"}})
    assert result["chains"][0]["edges"] == original["chains"][0]["edges"]
    assert result["chains"][0]["severity"] == original["chains"][0]["severity"]
    assert result["chains"][0]["title"] == "Reviewed title"
    assert len(result["ignoredExplanations"]) == 3


def test_validator_rejects_invented_reference_and_edge():
    rows = pair()
    chain = doc(rows)["chains"][0]
    by_id = {r["findingId"]: r for r in rows}
    fake = copy.deepcopy(chain)
    fake["edges"][0]["evidenceRefs"].append("hsf_a#nonexistent")
    with pytest.raises(ValidationError):
        validate_chain(fake, by_id)
    fake = copy.deepcopy(chain)
    fake["edges"][0]["effect"]["kind"] = "privilege-gain"
    with pytest.raises(ValidationError):
        validate_chain(fake, by_id)


def test_ssrf_to_admin_exec_and_determinism():
    rows = pair()
    before = copy.deepcopy(rows)
    result = doc(rows)
    assert result == doc(list(reversed(rows)))
    assert rows == before
    chain = result["chains"][0]
    assert chain["findingIds"] == ["hsf_a", "hsf_b"]
    assert chain["disposition"] == "source_supported_chain"
    assert chain["severity"]["level"] == "high"
    assert chain_severity(chain, {r["findingId"]: r for r in rows}) == chain["severity"]


def test_credential_leak_to_authenticated_idor():
    effect = cap("credential-acquire")
    effect["yieldsActor"] = "authenticated"
    pre = cap("authenticated-session")
    pre["actor"] = "authenticated"
    rows = [finding("a", effects=[effect]), finding("b", pre=[pre], effects=[cap("data-read")])]
    assert len(doc(rows)["chains"]) == 1
    del effect["yieldsActor"]
    assert not doc(rows)["chains"]


@pytest.mark.parametrize("change", ["candidate", "no-evidence", "assumption"])
def test_conditional_never_escalates(change):
    rows = pair()
    if change == "candidate":
        rows[0]["evidenceState"] = "candidate"
    elif change == "no-evidence":
        rows[0]["codeEvidence"] = []
    else:
        rows[1]["attackPath"]["assumptions"] = ["admin component enabled"]
    chain = doc(rows)["chains"][0]
    assert chain["conditional"]
    assert chain["disposition"] == "candidate_chain"
    assert chain["severity"]["level"] == "medium"


def test_other_defeated_required_precondition_cannot_be_bypassed():
    rows = pair()
    blocked = cap("authenticated-session")
    blocked["brokenBy"] = ["token revoked"]
    rows[1]["capabilities"]["preconditions"].append(blocked)
    rows[1]["counterEvidence"] = ["token revoked"]
    result = doc(rows)
    assert not result["chains"]
    assert result["broken"][0]["defeatedBy"] == "token revoked"


def test_invalid_final_effect_does_not_escalate():
    rows = pair()
    del rows[1]["capabilities"]["effects"][0]["actor"]
    assert doc(rows)["chains"][0]["severity"]["level"] == "medium"


def test_canned_evaluation():
    proc = subprocess.run([sys.executable, "scripts/eval_chains.py"], cwd=Path(__file__).resolve().parents[2],
                          capture_output=True, text=True, check=True)
    result = json.loads(proc.stdout)
    assert result["cases"] >= 8
    assert result["precision"] == result["recall"] == 1.0
    assert result["inventedEdgeCount"] == 0
