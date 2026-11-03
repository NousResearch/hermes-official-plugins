import copy
import json
from pathlib import Path
import pytest
from hermes_security.domain.validate import validate_document, validate_worker_result, validate_schema, check_finding_sections, check_cross_refs
from hermes_security.domain.models import promote_to_finding

SID = 'scan_' + 'a' * 24

def raw():
    return json.loads((Path(__file__).parents[1] / 'fixtures/domain/candidate.json').read_text())

def worker():
    return {'scanId':SID,'attemptId':'att_a','workerRole':'baseline','snapshotDigest':'sha256:'+'a'*64,'methodologyVersion':'hermes-security/method-1','packetId':None,'candidates':[raw()],'coverage':[],'negativeResults':[],'notes':''}

def test_valid_worker_and_finding():
    assert validate_worker_result(worker()) == []
    f=promote_to_finding(raw(),scan_id=SID,repo_key='repo')
    assert validate_document('hermes-security.findings',{'documentType':'hermes-security.findings','schemaVersion':'1.0','scanId':SID,'findings':[f]}) == []
    assert check_finding_sections(f) == []

@pytest.mark.parametrize('key,value', [('extra',1),('scanId','scan_no'),('attemptId','att_UPPER'),('notes','x'*4001),('candidates',[raw()]*201),('notes',float('nan')),('notes',float('inf'))])
def test_invalid_worker(key,value):
    w=worker(); w[key]=value
    assert validate_worker_result(w)
    assert validate_document('worker-result',w)

def test_nested_limits_and_unknowns():
    for key,value in [('title','x'*20001),('extra',True),('candidateId','cand_bad'),('codeEvidence',[{'code':'x'*4001}])]:
        w=worker(); w['candidates'][0][key]=value
        assert validate_worker_result(w)
    w=worker(); w['candidates']=[{**raw(),'summary':'x'*19000} for _ in range(150)]
    assert any('2 MiB' in p for p in validate_worker_result(w))

@pytest.mark.parametrize('value', ['TODO','N/A','TBD','same as above','  '])
def test_section_placeholders(value):
    f=raw(); f['rootCause']=value
    assert check_finding_sections(f)

def test_duplicate_and_prefix_sections():
    f=raw(); f['rootCause']='  '+f['summary'].upper()+'  '
    assert any('duplicated' in p for p in check_finding_sections(f))
    f['rootCause']=f['summary']+' yes'
    assert any('duplicated' in p for p in check_finding_sections(f))

def test_cross_scan_and_reference_checks():
    f=promote_to_finding(raw(),scan_id=SID,repo_key='repo')
    fd={'scanId':SID,'findings':[f]}; cd={'scanId':SID,'chains':[],'broken':[]}
    assert check_cross_refs(SID,fd,cd)==[]
    cd['scanId']='scan_'+'b'*24
    assert check_cross_refs(SID,fd,cd)
    cd['scanId']=SID; cd['chains']=[{'findingIds':[f['findingId'],'hsf_'+'b'*24],'edges':[{'from':f['findingId'],'to':'hsf_'+'b'*24,'evidenceRefs':[f['findingId']+'#missing']}]}]
    assert len(check_cross_refs(SID,fd,cd)) >= 3

def test_schema_subset():
    schema={'type':'object','required':['x'],'properties':{'x':{'$ref':'#/$defs/number'}},'additionalProperties':False,'$defs':{'number':{'type':'integer','minimum':1,'maximum':3}}}
    assert validate_schema({'x':2},schema)==[]
    for value in [{},{'x':True},{'x':0},{'x':4},{'x':2,'z':0}]: assert validate_schema(value,schema)
    assert validate_schema('abc',{'type':'string','minLength':3,'maxLength':3,'pattern':'^a'})==[]
    assert validate_schema('ab',{'minLength':3})
    assert validate_schema(1,{'oneOf':[{'type':'number'},{'type':'integer'}]})
    assert validate_schema(None,{'anyOf':[{'type':'null'},{'type':'string'}]})==[]
    assert validate_schema([1],{'minItems':2})
    assert validate_schema([1,2],{'maxItems':1})
    assert validate_schema(True,{'const':1})
    assert validate_schema([True],{'const':[1]})
    w=worker(); w['scanId'] += '\n'
    assert validate_worker_result(w)
    assert validate_document('unknown',{})


def test_validation_receipt_schema():
    receipt={'receiptId':'vrc_'+'a'*24,'candidateId':'cand_'+'b'*24,'level':'static','kind':'static','status':'NOT_RUN','positive':{},'negative':{},'commandsDigest':'sha256:'+'c'*64,'startedAt':'2026-10-05T00:00:00Z','finishedAt':'2026-10-05T00:00:00Z','cleanup':{'done':True,'detail':'No runtime work'},'outputExcerpt':'','notes':[]}
    assert validate_document('validation-receipt',receipt)==[]
    receipt['checks']={name:{'checked':True,'notes':''} for name in ('source','control','sink','boundary','counterevidence')}
    assert validate_document('validation-receipt',receipt)==[]
    receipt['unknown']=True
    assert validate_document('validation-receipt',receipt)

def test_schema_documents_use_2020_12_and_closed_objects():
    path=Path(__file__).parents[2]/'hermes_security/schemas'
    schemas=list(path.glob('*.schema.json'))
    assert len(schemas)==6
    for p in schemas:
        s=json.loads(p.read_text())
        assert s['$schema'].endswith('/2020-12/schema')
        assert s['additionalProperties'] is False
        assert (s['$comment'].startswith('Modified from Apache-2.0 source')
                or s['$comment'].startswith('Original to hermes-security'))
