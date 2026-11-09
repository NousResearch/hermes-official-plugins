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
    candidate = json.loads((Path(__file__).parents[1] / 'fixtures/reports/invoice.json').read_text(encoding='utf-8'))
    for key in ('findingId', 'occurrenceId', 'fingerprints'):
        candidate.pop(key)
    candidate['evidenceState'] = 'candidate'
    candidate['identity'] = {'anchor': 'app.py/get_invoice'}
    candidate['locations'] = [{'path': 'app.py', 'startLine': 1, 'endLine': 2, 'role': 'sink'}]
    candidate['codeEvidence'] = [{'id': 'ev-1', 'label': 'Unscoped query', 'path': 'app.py', 'startLine': 1, 'endLine': 2,
                                 'code': ''.join((root / 'app.py').read_text(encoding='utf-8').splitlines(keepends=True)[:2]),
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


def _reviewed_scan(tmp_path, name, extra):
    root = tmp_path / name; root.mkdir()
    (root / 'app.py').write_text('def f():\n    return 1\n')
    service = SecurityService(tmp_path / (name + '-data'))
    sid = service.start_scan(path=str(root))['scanId']
    lanes = [{'unit': f'lane:A{i:02}:2025', 'state': 'reviewed', 'note': 'Checked.'} for i in range(1, 11)]
    service.checkpoint(sid, coverage=[{'unit': 'file:app.py', 'state': 'reviewed', 'note': 'Read in full.'}] + lanes + extra,
                       note='Done.')
    service.record_chains(sid)
    final = service.finalize(sid)
    return final, json.loads((Path(final['artifactDir']) / 'coverage.json').read_text(encoding='utf-8'))


def test_open_asvs_units_never_make_a_reviewed_scan_partial(tmp_path):
    asvs = [{'unit': 'asvs:V8.2.2', 'state': 'deferred', 'note': 'Needs an owner.'},
            {'unit': 'asvs:V16.5.3', 'state': 'failed', 'note': 'Could not check.'},
            {'unit': 'asvs:V1.3.6', 'state': 'unknown', 'note': 'Not looked at.'}]
    _, baseline = _reviewed_scan(tmp_path, 'plain', [])
    final, coverage = _reviewed_scan(tmp_path, 'asvs', asvs)
    # The ledger keeps each ASVS state, but open ASVS units add no gap: the coverage is the same as
    # an identical scan that recorded none (its other gaps are this harness's unreviewed packets).
    states = {c['id']: c['state'] for c in coverage['standards']['asvs']['controls']}
    assert (states['V8.2.2'], states['V16.5.3'], states['V1.3.6']) == ('deferred', 'failed', 'unknown')
    assert not any('asvs' in gap.lower() for gap in coverage['gaps'])
    assert [g for g in coverage['gaps'] if 'Worker packet' not in g] == []
    assert len(coverage['gaps']) == len(baseline['gaps']) and coverage['completeness'] == baseline['completeness']


def test_finalize_ignores_open_asvs_units():
    from hermes_security.reports.finalize import _coverage_gaps
    coverage = {'completeness': 'complete', 'files': {'total': 1, 'reviewed': 1, 'deferred': 0, 'unsupported': 0},
                'standards': {'owaspTop10': {'categories': [{'id': f'A{i:02}:2025', 'state': 'reviewed'} for i in range(1, 11)]}},
                'units': [{'unitId': 'file:app.py', 'state': 'reviewed'}, {'unitId': 'asvs:V8.2.2', 'state': 'deferred'},
                          {'unitId': 'asvs:V16.5.3', 'state': 'failed'}], 'workers': []}
    assert _coverage_gaps(coverage) == []
    coverage['units'].append({'unitId': 'file:b.py', 'state': 'deferred'})
    assert _coverage_gaps(coverage) == ['Review unit file:b.py is incomplete.']


def test_key_line_is_checked_on_submit_and_named_in_the_report(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('def get_invoice(invoice_id):\n    return invoices.get(invoice_id)\n')
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=str(root))
    sid = plan['scanId']
    outside = candidate_for(root)
    outside['identity']['anchor'] = 'app.py/outside'
    outside['codeEvidence'][0]['keyLine'] = 3
    good = candidate_for(root)
    good['codeEvidence'][0]['keyLine'] = 2
    result = service.submit_worker_result(worker(plan, [good, outside]))
    assert result['inserted'] == 1 and len(result['rejected']) == 1
    assert 'keyLine must be a line inside its excerpt' in json.dumps(result['rejected'])
    assert service.get_scan(sid, 'candidates')['items'][0]['codeEvidence'][0]['keyLine'] == 2
    good['evidenceState'] = 'source_supported'
    service.submit_worker_result(worker(plan, [good], attempt='att_validator', role='validator'))
    service.record_chains(sid)
    service.finalize(sid)
    report = service.export(sid, 'md')['content']
    assert 'Key line 2: `return invoices.get(invoice_id)`.' in report
