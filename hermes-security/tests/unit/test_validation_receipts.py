import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import pytest
from hermes_security.validation.receipts import static_receipt, next_evidence_state, build_receipt, validate_receipt, redact_output
from hermes_security.validation.plans import execution_limits, control_spec
from hermes_security.errors import ValidationError, PolicyDenied

def test_static_and_transitions():
    r=static_receipt({'candidateId':'c'},checks={k:{'checked':True,'notes':'reviewed'} for k in ['source','control','sink','boundary','counterevidence']})
    assert r['status']=='NOT_RUN' and next_evidence_state('candidate',r)=='source_supported'
    for state in ['candidate','source_supported','runtime_confirmed','rejected','inconclusive']:
        assert next_evidence_state(state,r)!='runtime_confirmed'
    with pytest.raises(ValidationError): static_receipt({},checks={})
    r['status']='passed'
    with pytest.raises(ValidationError): validate_receipt(r)

def test_runtime_receipt():
    r=build_receipt({'candidateId':'c','level':'local-safe','kind':'local-command'},status='passed',positive={'ran':True,'complete':True,'matched':True},negative={'ran':True,'complete':True,'matched':False},output='token=top-secret')
    assert 'top-secret' not in str(r) and next_evidence_state('candidate',r)=='runtime_confirmed'
    r['negative']['ran']=False
    with pytest.raises(ValidationError): validate_receipt(r)
    assert 'value' not in redact_output('password=value')

def test_limits_and_controls():
    assert execution_limits({})==(10,65536)
    for p in [{'timeoutS':0},{'timeoutS':float('nan')},{'maxOutputBytes':0}]:
        with pytest.raises(PolicyDenied): execution_limits(p)
    with pytest.raises(PolicyDenied): control_spec({'positiveControl':{},'negativeControl':{}},1)
