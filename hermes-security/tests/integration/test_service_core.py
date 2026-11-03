from pathlib import Path
import pytest
from hermes_security.service import SecurityService
from hermes_security.errors import Conflict, PolicyDenied, SealedError, ValidationError


def test_start_snapshot_cancel_resume(tmp_path):
    root = tmp_path / 'repo'; root.mkdir(); (root / 'app.py').write_text('print(1)\n')
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=str(root))
    assert plan['packets'] and len(plan['lanes']) == 10
    assert Path(plan['workerBriefPath']).is_file()
    assert service.coverage(plan['scanId'])['completeness'] == 'partial'
    service.cancel(plan['scanId'])
    payload = dict(scanId=plan['scanId'], attemptId='att_first', workerRole='baseline',
                   packetId=plan['packets'][0]['packetId'], snapshotDigest=plan['snapshotDigest'],
                   methodologyVersion=plan['methodologyVersion'], candidates=[], coverage=[], negativeResults=[], notes='')
    with pytest.raises(Conflict): service.submit_worker_result(payload)
    assert service.resume(plan['scanId'])['packets']
    result = service.submit_worker_result(payload)
    assert result['inserted'] == 0
    assert service.submit_worker_result(payload)['duplicates'] == 0
    (root / 'app.py').write_text('print(2)\n')
    with pytest.raises(Conflict, match='snapshot changed'): service.submit_worker_result(dict(payload, attemptId='att_second'))
    assert service.finalize(plan['scanId'])['verified']['ok']
    with pytest.raises(SealedError): service.submit_worker_result(payload)
    with pytest.raises(SealedError): service.finalize(plan['scanId'])


def test_local_policy_and_profile_isolation(tmp_path, monkeypatch):
    from hermes_security.config import resolve_data_dir
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'A'))
    store = 'plugin-data/agent-plugin-hermes-security-974429e7'
    assert resolve_data_dir() == tmp_path / 'A' / store
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'B'))
    assert resolve_data_dir() == tmp_path / 'B' / store
    root = tmp_path / 'repo'; root.mkdir()
    service = SecurityService(tmp_path / 'data')
    # One switch: allow_tests (or an explicit level) enables local tests.
    assert service.get_scan(service.start_scan(path=str(root), allow_tests=True)['scanId'])['safety_level'] == 'local-safe'
    assert service.get_scan(service.start_scan(path=str(root))['scanId'])['safety_level'] == 'static'
    with pytest.raises(ValidationError): service.start_scan(path=str(root), allow_tests='yes')
