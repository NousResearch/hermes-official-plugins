from hermes_security.domain.identities import finding_id, occurrence_id, candidate_id, primary_fingerprint, scan_id, chain_id
from hermes_security.canonical import stable_id


def test_identity_contract():
    assert finding_id('repo', 'rule', 'anchor') == stable_id('hsf', 'repo', 'rule', 'anchor')
    assert occurrence_id('scan', 'finding', {'path': 'x'}) == stable_id('occ', 'scan', 'finding', {'path': 'x'})
    assert candidate_id('s', 'r', 'a', 'p', 1) == stable_id('cand', 's', 'r', 'a', 'p', 1)
    assert primary_fingerprint('r', 'rule', 'a').startswith('hermes-security/v1:sha256:')
    assert scan_id('r', 'snapshot') == scan_id('r', 'snapshot')
    assert chain_id(['a','b']) != chain_id(['b','a'])
