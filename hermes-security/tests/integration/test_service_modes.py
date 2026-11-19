from copy import deepcopy
import subprocess
import sys
import pytest

from hermes_security.service import SecurityService
from hermes_security.errors import Conflict, PolicyDenied, ValidationError
from hermes_security.orchestration.reducer import reduce_candidates, conservation_check
from hermes_security.orchestration.deep import stop_reason
from hermes_security.orchestration.diff import build_packets
from hermes_security.canonical import stable_id
from test_end_to_end_static import candidate_for, worker


def setup(tmp_path, **opts):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('def get_invoice(invoice_id):\n    return invoices.get(invoice_id)\n')
    service = SecurityService(tmp_path / 'data')
    return root, service, service.start_scan(path=str(root), **opts)


def test_deep_conservation_and_saturation(tmp_path):
    root, service, plan = setup(tmp_path, mode='deep', deep_passes=4)
    assert len(plan['packets']) == 4
    assert len({p['attemptId'] for p in plan['packets']}) == 4
    for packet in plan['packets'][:2]:
        p = worker(plan, [], role='deep-pass', attempt=packet['attemptId'])
        p['packetId'] = packet['packetId']
        service.submit_worker_result(p)
    resumed = service.resume(plan['scanId'])
    assert resumed['packets'] == [] and 'consecutive' in resumed['stopReason']
    a = candidate_for(root); a['candidateId'] = 'cand_' + '1' * 24
    b = deepcopy(a); b['candidateId'] = 'cand_' + '2' * 24
    c = deepcopy(a); c['candidateId'] = 'cand_' + '3' * 24; c['identity']['anchor'] = 'app.py/distinct'
    reduced = reduce_candidates([a, b, c])
    assert len(reduced['retained']) == 2 and len(reduced['absorbed']) == 1
    assert conservation_check([a, b, c], reduced) == []
    reduced['retained'].append(a)
    assert conservation_check([a, b, c], reduced)
    a['provenance']['subsumes'] = [c['candidateId']]
    reduced = reduce_candidates([a, b, c])
    assert len(reduced['retained']) == 1 and len(reduced['absorbed']) == 2 and not reduced['problems']
    assert stop_reason([1, 0], 2) == 'budget passes reached'
    assert stop_reason([], 3, canceled=True) == 'canceled'
    assert stop_reason([1], 3) is None


def test_validator_row_wins_over_discovery_row(tmp_path):
    # Store order (by attempt ID) puts the discovery row first; the validator must still win.
    root, service, plan = setup(tmp_path)
    found = candidate_for(root)
    service.submit_worker_result(worker(plan, [found], attempt='att_a_found'))
    checked = deepcopy(found)
    checked['evidenceState'] = 'source_supported'
    checked['severity']['level'] = 'medium' if found['severity']['level'] == 'low' else 'low'
    service.submit_worker_result(worker(plan, [checked], attempt='att_z_check', role='validator'))
    retained = service._reduced(plan['scanId'])['retained']
    assert len(retained) == 1
    assert retained[0]['severity']['level'] == checked['severity']['level']
    assert retained[0]['provenance']['workerAttemptIds'] == ['att_a_found', 'att_z_check']


def test_diff_all_paths_attribution_and_full_shas(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-c', 'user.name=Local test', '-c', 'user.email=test@example.invalid', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init')
    (root / 'app.py').write_text('def get_invoice(invoice_id):\n    return invoices.get(invoice_id)\n')
    (root / 'unchanged.py').write_text('print(1)\n')
    (root / 'deleted.py').write_text('print(2)\n')
    git('add', '.'); git('commit', '-m', 'base'); base = git('rev-parse', 'HEAD')
    (root / 'app.py').write_text('def get_invoice(invoice_id):\n    return invoices.get(invoice_id)\n# changed\n')
    (root / 'added.py').write_text('print(3)\n'); (root / 'deleted.py').unlink()
    git('add', '.'); git('commit', '-m', 'head'); head = git('rev-parse', 'HEAD')
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=str(root), mode='diff', base=base[:10], head=head[:10])
    assert {p for packet in plan['packets'] for p in packet['files']} == {'app.py', 'added.py', 'deleted.py'}
    assert plan['diff']['base'] == base and plan['diff']['head'] == head
    candidate = candidate_for(root)
    inherited = deepcopy(candidate); inherited['identity']['anchor'] = 'unchanged.py/f'; inherited['locations'] = [{'path': 'unchanged.py', 'startLine': 1}]; inherited['codeEvidence'] = []
    payload = worker(plan, [candidate, inherited], role='diff'); payload['packetId'] = plan['packets'][0]['packetId']
    service.submit_worker_result(payload)
    assert {c['diffAttribution'] for c in service.get_scan(plan['scanId'], 'candidates')['items']} == {'introduced', 'inherited'}
    assert any(u['unitId'] == 'file:deleted.py' for u in service.coverage(plan['scanId'])['units'])
    packets, manifest = build_packets(plan['scanId'], service.store.get_scan(plan['scanId'])['target'], [])
    assert manifest == plan['diff'] and packets
    final = service.finalize(plan['scanId'])
    assert final['manifest']['target']['base'] == base
    assert final['manifest']['target']['head'] == head


def test_validation_authority_and_real_local_controls(tmp_path):
    root, service, plan = setup(tmp_path)
    service.submit_worker_result(worker(plan, [candidate_for(root)]))
    cid = service.get_scan(plan['scanId'], 'candidates')['items'][0]['candidateId']
    local = {'candidateId': cid, 'kind': 'local-command',
             'commands': [[sys.executable, '-c', 'print("MARKER")'], [sys.executable, '-c', 'print("negative")']],
             'positiveControl': {'commandIndex': 0, 'expectedMarker': 'MARKER'}, 'negativeControl': {'commandIndex': 1, 'expectedMarker': 'MARKER'},
             'timeoutS': 30}
    # A scan started without tests never runs code, even if the agent asks.
    with pytest.raises(PolicyDenied): service.record_validations(plan['scanId'], plans=[local])
    plan = service.start_scan(path=str(root), allow_tests=True)
    service.submit_worker_result(worker(plan, [candidate_for(root)], attempt='att_tests_allowed'))
    cid = service.get_scan(plan['scanId'], 'candidates')['items'][0]['candidateId']
    local['candidateId'] = cid
    active = {**local, 'level': 'active-authorized'}
    with pytest.raises(PolicyDenied): service.record_validations(plan['scanId'], plans=[active])
    # Static checklists are allowed without runtime authorization.
    static = {'candidateId': cid, 'level': 'static', 'kind': 'static', 'checks': {k: True for k in ('source', 'control', 'sink', 'boundary', 'counterevidence')}}
    service.record_validations(plan['scanId'], plans=[static])
    assert service.get_scan(plan['scanId'], 'candidates')['items'][0]['evidenceState'] == 'source_supported'
    # The scan's allow_tests switch is the consent: the agent runs tests itself.
    recorded = service.record_validations(plan['scanId'], plans=[local])
    assert recorded['receipts'][0]['status'] == 'passed'
    assert service.get_scan(plan['scanId'], 'candidates')['items'][0]['evidenceState'] == 'runtime_confirmed'
    assert service.finalize(plan['scanId'])['manifest']['counts']['high'] == 1


def test_user_grants_and_revocation(tmp_path):
    root, service, plan = setup(tmp_path)
    grant = service.mint_grant(plan['scanId'], origins=['https://example.invalid'], actions=['http-probe'], expires_in_s=60, max_requests=2)
    assert grant['used'] == 0
    assert service.revoke_grant(grant['grantId'])['revoked']


def test_missing_sections_retained(tmp_path):
    root, service, plan = setup(tmp_path)
    candidate = candidate_for(root); candidate['summary'] = candidate['rootCause']; candidate['evidenceState'] = 'source_supported'
    # Refused at submit, while the worker can still fix it, instead of being demoted at finalize.
    response = service.submit_worker_result(worker(plan, [candidate]))
    assert response['inserted'] == 0 and 'incomplete sections' in response['rejected'][0]['reason']
    result = service.finalize(plan['scanId'])
    assert result['manifest']['counts']['high'] == 0 and result['manifest']['counts']['retained'] == 0


def test_lease_excludes_other_service_and_resume_expired(tmp_path):
    root, service, plan = setup(tmp_path)
    sid = plan['scanId']
    other = SecurityService(tmp_path / 'data')
    assert service.store.lease(sid, 'another-worker', 300)
    with pytest.raises(Conflict): other.finalize(sid)
    with pytest.raises(Conflict): other.resume(sid)
    with service.store.transaction() as conn:
        conn.execute('UPDATE leases SET expires_at=0 WHERE scan_id=?', (sid,))
    service.store.set_scan_status(sid, 'running')
    assert other.resume(sid)['packets']
