"""A stray file written into the target must not void a scan's work.

Incident: workers dumped notes into the target's working folder (the terminal starts
there), the whole-tree digest changed, and every result was rejected with
'snapshot changed'. Reviewed files were untouched, so their evidence was still valid.
"""
from pathlib import Path

import pytest

from hermes_security.errors import Conflict
from hermes_security.service import SecurityService


def _start(tmp_path):
    root = tmp_path / 'repo'; root.mkdir(); (root / 'app.py').write_text('print(1)\n')
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=str(root))
    payload = dict(scanId=plan['scanId'], attemptId='att_first', workerRole='baseline',
                   packetId=plan['packets'][0]['packetId'], snapshotDigest=plan['snapshotDigest'],
                   methodologyVersion=plan['methodologyVersion'], candidates=[], coverage=[], negativeResults=[], notes='')
    return root, service, plan, payload


def test_new_file_in_the_target_does_not_reject_work_on_unchanged_files(tmp_path):
    root, service, plan, payload = _start(tmp_path)
    (root / 'notes_dump.txt').write_text('scratch\n')
    assert service.submit_worker_result(payload)['inserted'] == 0
    result = service.finalize(plan['scanId'])
    assert result['verified']['ok']
    gaps = ' '.join(service.coverage(plan['scanId'])['gaps'])
    assert 'added after this scan started' in gaps


def test_changed_or_deleted_reviewed_file_still_rejects(tmp_path):
    root, service, plan, payload = _start(tmp_path)
    (root / 'app.py').write_text('print(2)\n')
    with pytest.raises(Conflict, match='snapshot changed'):
        service.submit_worker_result(payload)
    (root / 'app.py').unlink()
    with pytest.raises(Conflict, match='snapshot changed'):
        service.submit_worker_result(payload)


def test_plan_names_a_private_work_dir_outside_the_target(tmp_path):
    root, service, plan, _ = _start(tmp_path)
    for header in (plan, service.get_scan(plan['scanId'], section='summary')):
        work = Path(header['workDir'])
        assert work.is_dir()
        assert root not in work.parents and work != root
