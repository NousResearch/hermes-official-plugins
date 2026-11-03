"""Boundary checks for independent validation and severity policy."""
import copy
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from hermes_security.chains import build_chains_doc, compose, eligible_edges, validate_chain, chain_severity
from hermes_security.errors import ValidationError
from hermes_security.chains.vocabulary import EFFECT_SATISFIES
from test_chain_graph import SNAP, cap, doc, finding, pair


@pytest.mark.parametrize("kind,pre", [(kind, pre) for kind, pres in sorted(EFFECT_SATISFIES.items()) for pre in sorted(pres)])
def test_every_mapped_effect_requires_exact_compatible_precondition(kind, pre):
    effect = cap(kind)
    if kind == "data-read":
        effect["yieldsCredential"] = True
    rows = [finding("a", effects=[effect]), finding("b", pre=[cap(pre)])]
    assert len(eligible_edges(rows, snapshot_digest=SNAP)) == 1


@pytest.mark.parametrize("level,expected", [("critical", "critical"), ("high", "critical"), ("medium", "high"), ("low", "medium"), ("informational", "low")])
def test_at_most_one_band_uplift(level, expected):
    rows = pair()
    for row in rows:
        row["severity"]["level"] = level
    assert doc(rows)["chains"][0]["severity"]["level"] == expected
    rows[1]["evidenceState"] = "candidate"
    assert doc(rows)["chains"][0]["severity"]["level"] == level


@pytest.mark.parametrize("kind,tenant,expected", [("privilege-gain", "same", "high"), ("credential-acquire", "cross", "high"), ("credential-acquire", "same", "medium"), ("data-read", "cross", "medium")])
def test_final_impact_not_intermediate_effect_controls_uplift(kind, tenant, expected):
    rows = pair()
    effect = cap(kind)
    effect["tenant"] = tenant
    rows[1]["capabilities"]["effects"] = [effect]
    assert doc(rows)["chains"][0]["severity"]["level"] == expected


def test_revalidate_broken_and_stripped_evidence():
    rows = pair()
    chain = doc(rows)["chains"][0]
    by_id = {row["findingId"]: row for row in rows}
    chain["edges"][0]["evidenceRefs"] = []
    checked = validate_chain(chain, by_id)
    assert checked["conditional"] and checked["severity"]["level"] == "medium"
    rows[1]["capabilities"]["preconditions"][0]["brokenBy"] = ["firewall"]
    rows[1]["attackPath"]["controls"] = ["firewall"]
    # The capability was changed, so reconstruct the edge from current facts.
    edge = eligible_edges(rows, snapshot_digest=SNAP)[0]
    edge["state"] = "supported"  # cannot upgrade a defeated transition
    chain["edges"] = [edge]
    checked = validate_chain(chain, by_id)
    assert checked["disposition"] == "broken_chain"
    assert checked["severity"]["level"] == "medium"
    assert compose(rows, [edge]) == []


def test_references_must_belong_to_edge_endpoints():
    rows = pair() + [finding("c")]
    chain = doc(rows)["chains"][0]
    chain["edges"][0]["evidenceRefs"].append("hsf_c#ev-1")
    with pytest.raises(ValidationError):
        validate_chain(chain, {r["findingId"]: r for r in rows})


def test_unknown_duplicate_and_disallowed_steps_rejected():
    rows = pair()
    original = doc(rows)["chains"][0]
    by_id = {r["findingId"]: r for r in rows}
    for ids in (["hsf_a", "missing"], ["hsf_a", "hsf_a"]):
        altered = copy.deepcopy(original)
        altered["findingIds"] = ids
        with pytest.raises(ValidationError):
            validate_chain(altered, by_id)
    rows[1]["evidenceState"] = "rejected"
    with pytest.raises(ValidationError):
        validate_chain(original, by_id)


def test_context_and_empty_documents():
    assert doc([])["chains"] == []
    rows = pair()
    with pytest.raises(ValidationError):
        build_chains_doc("other", rows, snapshot_digest=SNAP)
    with pytest.raises(ValidationError):
        eligible_edges(rows + [rows[0]], snapshot_digest=SNAP)
    for row in rows:
        row["provenance"] = {"scanId": row.pop("scanId"), "snapshotDigest": row.pop("snapshotDigest")}
    assert len(doc(rows)["chains"]) == 1


def test_auth_scope_is_not_inferred():
    effect = cap("credential-acquire")
    pre = cap("authenticated-session")
    pre["authScope"] = "billing"
    rows = [finding("a", effects=[effect]), finding("b", pre=[pre])]
    assert not doc(rows)["chains"]
    effect["authScope"] = "billing"
    assert len(doc(rows)["chains"]) == 1


def test_capability_order_and_duplicate_proofs_do_not_affect_output():
    rows = pair()
    rows[0]["capabilities"]["effects"] += [cap("code-exec"), cap("internal-network-reach")]
    first = doc(rows)
    rows[0]["capabilities"]["effects"].reverse()
    assert first == doc(rows)
    assert len(first["chains"]) == 1


def test_remaining_preconditions_are_conditional():
    rows = pair()
    rows[1]["capabilities"]["preconditions"].append(cap("user-interaction"))
    chain = doc(rows)["chains"][0]
    assert chain["conditional"]
    assert "Unestablished precondition" in chain["edges"][0]["assumptions"][0]


def test_cross_and_wildcard_scope_rules():
    rows = pair()
    rows[0]["capabilities"]["effects"][0]["tenant"] = "cross"
    rows[1]["capabilities"]["preconditions"][0].update(tenant="any", actor="any", deployment="any")
    assert len(doc(rows)["chains"]) == 1


@pytest.mark.parametrize("kwargs", [{"max_depth": -1}, {"max_chains": -1}, {"max_depth": True}])
def test_invalid_budgets(kwargs):
    with pytest.raises(ValidationError):
        compose(pair(), [], **kwargs)


def test_budgets_cannot_raise_hard_caps():
    rows = [finding(str(i), [cap("file-write")], [cap("file-write")]) for i in range(7)]
    chains = compose(rows, eligible_edges(rows, snapshot_digest=SNAP), max_depth=100, max_chains=100)
    assert len(chains) == 50
    assert max(len(c["findingIds"]) for c in chains) == 4
