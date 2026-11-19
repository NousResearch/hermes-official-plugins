"""Receipt construction and conservative evidence transitions."""
import re
from ..canonical import canonical_json, sha256_hex, stable_id, utcnow
from ..errors import ValidationError

_SECRET=re.compile(r'(?im)(?:\b(?:password|passwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*[\"\']?[^\s\"\',;]+|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bsk-[A-Za-z0-9_-]{16,}\b|Bearer\s+\S+|-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----)')

def redact_output(text):
    """Never persist recognized credential values from harness output."""
    return _SECRET.sub('[REDACTED]',str(text))

def validate_receipt(receipt):
    """Reject malformed receipts and unsupported runtime-success claims."""
    if not isinstance(receipt,dict): raise ValidationError('receipt must be an object')
    required={'receiptId','candidateId','level','kind','status','positive','negative','commandsDigest','startedAt','finishedAt','cleanup','outputExcerpt','notes'}
    if not required<=receipt.keys(): raise ValidationError('missing receipt fields')
    if not re.fullmatch(r'vrc_[0-9a-f]{24}',receipt['receiptId']): raise ValidationError('invalid receiptId')
    if receipt['level'] not in {'static','local-safe','active-authorized'} or receipt['status'] not in {'NOT_RUN','passed','failed','inconclusive'}:
        raise ValidationError('invalid receipt level/status')
    if len(receipt['outputExcerpt'])>4000: raise ValidationError('output excerpt too long')
    if receipt['level']=='static' and receipt['status']!='NOT_RUN': raise ValidationError('static receipt must be NOT_RUN')
    if receipt['status']=='passed' and not _confirmed(receipt): raise ValidationError('passed requires completed paired controls and cleanup')
    canonical_json(receipt)
    return receipt

def _confirmed(r):
    p,n=r.get('positive',{}),r.get('negative',{})
    return (r.get('level') in {'local-safe','active-authorized'} and r.get('status')=='passed'
            and p.get('ran') is True and n.get('ran') is True and p.get('matched') is True
            and n.get('matched') is False and p.get('complete') is True and n.get('complete') is True
            and r.get('cleanup',{}).get('done') is True)

def build_receipt(plan, *, status, positive=None, negative=None, started_at=None, cleanup=None, output='', notes=None):
    """Build a secret-redacted, bounded receipt from executed observations."""
    r=dict(candidateId=plan.get('candidateId',''),level=plan.get('level','static'),kind=plan.get('kind','static'),
           status=status,positive=positive or {},negative=negative or {},
           commandsDigest='sha256:'+sha256_hex(canonical_json({'commands':plan.get('commands',[]),'requests':plan.get('requests',[])})),
           startedAt=started_at or utcnow(),finishedAt=utcnow(),cleanup=cleanup or {'done':True,'detail':'no runtime work'},
           outputExcerpt=redact_output(output)[:4000],notes=[redact_output(n) for n in (notes or [])])
    r['receiptId']=stable_id('vrc',r)
    return validate_receipt(r)

def static_receipt(candidate, *, checks):
    """Record a five-part source review, explicitly NOT_RUN at runtime."""
    keys={'source','control','sink','boundary','counterevidence'}
    if not isinstance(checks,dict) or set(checks)!=keys: raise ValidationError('static checklist requires source/control/sink/boundary/counterevidence')
    normalized={}
    for k,v in checks.items():
        if isinstance(v,bool): v={'checked':v,'notes':''}
        if not isinstance(v,dict) or type(v.get('checked')) is not bool or not isinstance(v.get('notes'),str):
            raise ValidationError('check requires checked boolean and notes')
        normalized[k]={'checked':v['checked'],'notes':redact_output(v['notes'])}
    r=build_receipt({'candidateId':candidate.get('candidateId',candidate.get('findingId','')),'level':'static','kind':'static'},status='NOT_RUN',notes=['Runtime NOT RUN; source review only'])
    r['checks']=normalized
    r['receiptId']=stable_id('vrc',{k:v for k,v in r.items() if k!='receiptId'})
    return r

def next_evidence_state(current, receipt):
    """Only successful executed paired controls can establish runtime evidence."""
    if current not in {'candidate','source_supported','runtime_confirmed','rejected','inconclusive'}:
        raise ValidationError('invalid evidence state')
    validate_receipt(receipt)
    if _confirmed(receipt): return 'runtime_confirmed'
    if receipt['level']=='static' or receipt['status']=='NOT_RUN':
        if current=='runtime_confirmed': return 'source_supported'
        checks=receipt.get('checks',{})
        if current=='candidate' and checks and all(v.get('checked') is True for v in checks.values()): return 'source_supported'
        return current
    return 'inconclusive'
