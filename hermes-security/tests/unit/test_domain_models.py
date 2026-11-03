import json
from pathlib import Path
import pytest
from hermes_security.domain.models import normalize_candidate, promote_to_finding
from hermes_security.errors import ValidationError
from hermes_security.domain.identities import finding_id

SID = 'scan_' + 'a' * 24

def raw():
    return json.loads((Path(__file__).parents[1] / 'fixtures/domain/candidate.json').read_text())

def test_normalize_and_promote():
    original = raw()
    c = normalize_candidate(original, scan_id=SID)
    assert 'candidateId' not in original
    assert c['proofGaps'] == [] and c['counterEvidence'] == []
    assert c['validation'] == {'status':'NOT_RUN','level':'static','receiptIds':[],'summary':''}
    assert normalize_candidate({**original,'validation':{'status':'NOT_RUN'}},scan_id=SID)['validation']['level']=='static'
    f = promote_to_finding(c, scan_id=SID, repo_key='repo')
    assert f['findingId'] == finding_id('repo', c['ruleId'], c['identity']['anchor'])
    assert 'candidateId' not in f and 'source' not in f
    assert f['provenance']['sourceCandidateIds'] == [c['candidateId']]

@pytest.mark.parametrize('change', [{'summary':'TODO'}, {'rootCause':'An authenticated user can fetch records owned by another account.'}, {'evidenceState':'candidate'}, {'evidenceState':'runtime_confirmed'}])
def test_promotion_rejects_incomplete_or_unsupported(change):
    c=raw(); c.update(change)
    with pytest.raises(ValidationError):
        promote_to_finding(c, scan_id=SID, repo_key='repo')

def test_normalize_missing_identity():
    with pytest.raises(ValidationError): normalize_candidate({}, scan_id=SID)
