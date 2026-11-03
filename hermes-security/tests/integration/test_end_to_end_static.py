"""Offline real-library orchestration acceptance (no external scanner dependency)."""
from copy import deepcopy
import json
from pathlib import Path
import pytest

from hermes_security.service import SecurityService
from hermes_security.errors import SealedError
from hermes_security.reports import verify_bundle
from hermes_security.canonical import sha256_hex


def candidate_for(root):
    candidate = json.loads((Path(__file__).parents[1] / 'fixtures/reports/invoice.json').read_text())
    for key in ('findingId', 'occurrenceId', 'fingerprints'):
        candidate.pop(key)
    candidate['evidenceState'] = 'candidate'
    candidate['identity'] = {'anchor': 'app.py/get_invoice'}
    candidate['locations'] = [{'path': 'app.py', 'startLine': 1, 'endLine': 2, 'role': 'sink'}]
    candidate['codeEvidence'] = [{'id': 'ev-1', 'label': 'Unscoped query', 'path': 'app.py', 'startLine': 1, 'endLine': 2,
                                 'code': ''.join((root / 'app.py').read_text().splitlines(keepends=True)[:2]),
                                 'sha256': '0' * 64, 'explanation': 'Invoice selection omits a tenant constraint.'}]
    candidate['validation'] = {'status': 'NOT_RUN', 'level': 'static', 'receiptIds': [], 'summary': 'Source review; runtime NOT RUN.'}
    return candidate


def worker(plan, candidates, attempt='att_baseline', role='baseline', coverage=None):
    return dict(scanId=plan['scanId'], attemptId=attempt, workerRole=role,
                snapshotDigest=plan['snapshotDigest'], methodologyVersion=plan['methodologyVersion'],
                packetId=plan['packets'][0]['packetId'] if role == 'baseline' else None,
                candidates=candidates, coverage=coverage or [], negativeResults=[], notes='')


def test_end_to_end_static(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    secret = 'AKIA' + 'Q7X2M9P4R8S6T1V3'
    (root / 'app.py').write_text('def get_invoice(invoice_id):\n    return invoices.get(invoice_id)\n' + 'AWS_ACCESS_KEY_ID = "' + secret + '"\n')
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=str(root))
    sid = plan['scanId']
    candidate = candidate_for(root)
    forged = deepcopy(candidate); forged['identity']['anchor'] = 'app.py/forged'; forged['codeEvidence'][0]['code'] = 'invented query'
    payload = worker(plan, [candidate, forged], coverage=[{'unit': 'file:app.py', 'state': 'reviewed', 'note': 'Source reviewed.'}])
    result = service.submit_worker_result(payload)
    assert result['inserted'] == 1 and len(result['rejected']) == 1
    assert service.submit_worker_result(payload)['duplicates'] == 1
    stored = service.get_scan(sid, 'candidates')['items'][0]
    assert stored['codeEvidence'][0]['sha256'] == sha256_hex(candidate['codeEvidence'][0]['code'])
    candidate['evidenceState'] = 'source_supported'
    service.submit_worker_result(worker(plan, [candidate], attempt='att_validator', role='validator'))
    service.checkpoint(sid, coverage=[{'unit': 'lane:A01:2025', 'state': 'reviewed', 'note': 'Ownership checks reviewed.'}], note='Authorization pass done.')
    assert service.propose_chains(sid)['scanId'] == sid
    assert service.record_chains(sid)['scanId'] == sid
    final = service.finalize(sid)
    assert final['status'] == 'partial'
    assert final['manifest']['counts']['high'] == 1
    assert verify_bundle(Path(final['artifactDir'])) == {'ok': True, 'problems': []}
    report = service.export(sid, 'md')['content']
    assert 'This scan is incomplete' in report and 'NOT RUN' in report
    sarif = json.loads(service.export(sid, 'sarif')['content'])
    assert sarif['version'] == '2.1.0'
    assert len(sarif['runs'][0]['results']) == 1
    for path in (tmp_path / 'data').rglob('*'):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
    with pytest.raises(SealedError): service.submit_worker_result(payload)
    fid = service.list_findings()['items'][0]['finding_id']
    assert service.get_finding(fid)
    assert service.patch_preview(fid)['readOnly']
    assert 'app.py:1-2' in service.patch_preview(fid)['content']
    service.triage(fid, 'accepted_risk', 'Tracked locally.')
    assert service.list_findings(triage='accepted_risk')['total'] == 1
    assert service.list_repositories()['total'] == 1
    assert service.list_scans()['total'] == 1
    assert service.summary()['findings'] == 1
    assert service.activity(sid)['items']
    assert service.get_scan(sid, 'manifest')['seal']
    assert service.get_scan(sid, 'report')['content'] == report
    assert service.export(sid, 'csv')['content']
    assert service.export(sid, 'json')['content']
    assert verify_bundle(Path(final['artifactDir']))['ok']
