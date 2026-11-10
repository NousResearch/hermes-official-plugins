"""A possible duplicate pair needs a same-bug-or-different verdict before the scan can be sealed."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from hermes_security.errors import Conflict, ValidationError
from hermes_security.reports import verify_bundle
from hermes_security.service import SecurityService
from test_end_to_end_static import candidate_for, worker

VERDICT_FIELDS = {'duplicateOf', 'distinctFrom', 'subsumes'}


def checked_pair(tmp_path):
    """Two source-supported candidates, led by different lines, that name the same sink."""
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('def get_invoice(invoice_id):\n    return invoices.get(invoice_id)\n\n'
                                 'def route(request):\n    return get_invoice(request.args["id"])\n')
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=str(root))
    sid = plan['scanId']
    first = candidate_for(root)
    second = deepcopy(first)
    second['ruleId'] = 'authz.route-reads-any-invoice'
    second['identity']['anchor'] = 'app.py/route'
    second['locations'] = [{'path': 'app.py', 'startLine': 4, 'endLine': 5, 'role': 'entry_point'},
                           {'path': 'app.py', 'startLine': 2, 'endLine': 2, 'role': 'sink'}]
    for packet in plan['packets']:
        payload = worker(plan, [first, second] if packet['role'] == 'baseline' else [], role=packet['role'],
                         attempt='att_' + packet['packetId'][4:16],
                         coverage=[{'unit': 'file:app.py', 'state': 'reviewed', 'note': 'Read fully.'}])
        payload['packetId'] = packet['packetId']
        assert service.submit_worker_result(payload)['rejected'] == []
    step = service.next_step(sid)
    assert step['step'] == 'validate' and step['total'] == 2 and 'kind' not in step['items'][0]
    for candidate in (first, second):
        candidate['evidenceState'] = 'source_supported'
    service.submit_worker_result(worker(plan, [first, second], attempt=step['attemptId'], role='validator'))
    ids = {row['identity']['anchor']: row['candidateId'] for row in service.get_scan(sid, 'candidates')['items']}
    return service, plan, first, second, ids['app.py/get_invoice'], ids['app.py/route']


def open_pair(service, sid, kept, other):
    """The scan asks for a verdict on the pair and refuses to seal without one."""
    step = service.next_step(sid)
    assert step['step'] == 'validate' and step['total'] == step['actionable'] == 1
    item = step['items'][0]
    assert item['kind'] == 'duplicate' and item['candidateIds'] == sorted([kept, other]) and item['state'] == 'pending'
    assert (item['path'], item['lines']) == ('app.py', [2, 2])
    assert service.get_scan(sid, 'summary')['possibleDuplicates'] == [
        {'candidateIds': sorted([kept, other]), 'path': 'app.py', 'lines': [2, 2]}]
    events = len(service.activity(sid)['items'])
    with pytest.raises(ValidationError, match='1 possible duplicate pair'):
        service.finalize(sid)
    scan = service.get_scan(sid)
    assert not scan['sealed_at'] and len(service.activity(sid)['items']) == events
    assert not (Path(service.data_dir) / 'scans' / sid / 'findings.json').exists()
    return step


def sealed(service, sid):
    assert service.next_step(sid)['step'] == 'chains'
    assert service.get_scan(sid, 'summary')['possibleDuplicates'] == []
    service.record_chains(sid)
    assert service.next_step(sid)['step'] == 'finalize'
    final = service.finalize(sid)
    assert final['possibleDuplicates'] == []
    assert verify_bundle(Path(final['artifactDir'])) == {'ok': True, 'problems': []}
    assert service.next_step(sid)['step'] == 'done'
    return json.loads((Path(final['artifactDir']) / 'findings.json').read_text(encoding='utf-8'))


def test_a_same_bug_verdict_seals_one_finding_that_names_both_sources(tmp_path):
    service, plan, first, second, kept, other = checked_pair(tmp_path)
    sid = plan['scanId']
    step = open_pair(service, sid, kept, other)
    second['provenance']['duplicateOf'] = kept
    result = service.submit_worker_result(worker(plan, [second], attempt=step['attemptId'], role='validator'))
    assert result['inserted'] == 1 and result['rejected'] == []
    doc = sealed(service, sid)
    assert len(doc['findings']) == 1 and doc['retained'] == []
    finding = doc['findings'][0]
    assert finding['identity']['anchor'] == 'app.py/get_invoice' and finding['locations'] == first['locations']
    assert finding['provenance']['sourceCandidateIds'] == sorted([kept, other])
    assert finding['provenance']['supersedes'] == [other]
    assert not VERDICT_FIELDS & set(finding['provenance'])


def test_a_different_bug_verdict_seals_both_findings_without_the_verdict_fields(tmp_path):
    service, plan, first, second, kept, other = checked_pair(tmp_path)
    sid = plan['scanId']
    step = open_pair(service, sid, kept, other)
    second['provenance']['distinctFrom'] = [kept]
    service.submit_worker_result(worker(plan, [second], attempt=step['attemptId'], role='validator'))
    doc = sealed(service, sid)
    assert sorted(f['identity']['anchor'] for f in doc['findings']) == ['app.py/get_invoice', 'app.py/route']
    for finding in doc['findings']:
        assert not VERDICT_FIELDS & set(finding['provenance'])
        assert finding['provenance']['supersedes'] is None


def test_a_changed_target_seals_the_open_pair_as_partial_instead_of_stranding_the_scan(tmp_path):
    service, plan, first, second, kept, other = checked_pair(tmp_path)
    sid = plan['scanId']
    step = open_pair(service, sid, kept, other)
    source = tmp_path / 'repo' / 'app.py'
    source.write_text(source.read_text() + '\n# edited after the scan started\n')
    # The changed target accepts no more results, so the verdict the scan asked for cannot arrive.
    second['provenance']['distinctFrom'] = [kept]
    with pytest.raises(Conflict, match='snapshot changed'):
        service.submit_worker_result(worker(plan, [second], attempt=step['attemptId'], role='validator'))
    final = service.finalize(sid)
    assert final['status'] == 'partial'
    assert final['possibleDuplicates'] == [{'candidateIds': sorted([kept, other]), 'path': 'app.py', 'lines': [2, 2]}]
    assert verify_bundle(Path(final['artifactDir'])) == {'ok': True, 'problems': []}
    artifacts = Path(final['artifactDir'])
    gaps = json.loads((artifacts / 'coverage.json').read_text(encoding='utf-8'))['gaps']
    assert '1 possible duplicate pair(s) were sealed without a same-bug-or-different verdict.' in gaps
    assert len(json.loads((artifacts / 'findings.json').read_text(encoding='utf-8'))['findings']) == 2
