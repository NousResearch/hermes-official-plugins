"""A possible duplicate pair needs a same-bug-or-different verdict before the scan can be sealed."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from hermes_security import recovery, tools
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


UNDECIDED_GAP = '1 possible duplicate pair(s) were sealed without a same-bug-or-different verdict.'


def test_an_undecided_pair_is_not_asked_again_and_seals_the_scan_as_partial(tmp_path):
    service, plan, first, second, kept, other = checked_pair(tmp_path)
    sid = plan['scanId']
    step = open_pair(service, sid, kept, other)
    second['provenance']['maybeDuplicateOf'] = [kept]
    second['proofGaps'] = [*second.get('proofGaps', []), 'Both reports reach the sink; the code read cannot tell whether one fix closes both.']
    result = service.submit_worker_result(worker(plan, [second], attempt=step['attemptId'], role='validator'))
    assert result['rejected'] == []
    assert service.next_step(sid)['step'] == 'chains'
    listed = [{'candidateIds': sorted([kept, other]), 'path': 'app.py', 'lines': [2, 2], 'verdict': 'undecided'}]
    assert service.get_scan(sid, 'summary')['possibleDuplicates'] == listed
    service.record_chains(sid)
    assert service.next_step(sid)['step'] == 'finalize'
    final = service.finalize(sid)
    assert final['status'] == 'partial' and final['possibleDuplicates'] == listed
    artifacts = Path(final['artifactDir'])
    assert verify_bundle(artifacts) == {'ok': True, 'problems': []}
    coverage = json.loads((artifacts / 'coverage.json').read_text(encoding='utf-8'))
    assert coverage['completeness'] == 'partial' and coverage['gaps'].count(UNDECIDED_GAP) == 1
    doc = json.loads((artifacts / 'findings.json').read_text(encoding='utf-8'))
    assert sorted(f['identity']['anchor'] for f in doc['findings']) == ['app.py/get_invoice', 'app.py/route']
    for sealed_row in doc['findings'] + doc['retained']:
        assert not (VERDICT_FIELDS | {'maybeDuplicateOf'}) & set(sealed_row['provenance'])
    assert service.next_step(sid)['step'] == 'done'


def test_a_rejected_candidate_is_retained_without_its_undecided_verdict(tmp_path):
    service, plan, first, second, kept, other = checked_pair(tmp_path)
    sid = plan['scanId']
    step = open_pair(service, sid, kept, other)
    second['provenance']['maybeDuplicateOf'] = [kept]
    second['evidenceState'] = 'rejected'
    assert service.submit_worker_result(worker(plan, [second], attempt=step['attemptId'], role='validator'))['rejected'] == []
    doc = sealed(service, sid)
    assert [r['candidateId'] for r in doc['retained']] == [other] and len(doc['findings']) == 1
    assert not (VERDICT_FIELDS | {'maybeDuplicateOf'}) & set(doc['retained'][0]['provenance'])


def test_a_later_different_bug_verdict_wins_over_undecided(tmp_path):
    service, plan, first, second, kept, other = checked_pair(tmp_path)
    sid = plan['scanId']
    step = open_pair(service, sid, kept, other)
    second['provenance']['maybeDuplicateOf'] = [kept]
    service.submit_worker_result(worker(plan, [second], attempt=step['attemptId'], role='validator'))
    assert service.get_scan(sid, 'summary')['possibleDuplicates'][0]['verdict'] == 'undecided'
    del second['provenance']['maybeDuplicateOf']
    second['provenance']['distinctFrom'] = [kept]
    result = service.submit_worker_result(worker(plan, [second], attempt='att_second_opinion', role='validator'))
    assert result['rejected'] == []
    doc = sealed(service, sid)
    assert len(doc['findings']) == 2
    coverage = json.loads((Path(service.data_dir) / 'scans' / sid / 'coverage.json').read_text(encoding='utf-8'))
    assert not any('possible duplicate pair' in gap for gap in coverage['gaps'])


def test_validators_that_never_give_a_verdict_trip_the_automatic_continue_limit(tmp_path):
    service, plan, first, second, kept, other = checked_pair(tmp_path)
    sid = plan['scanId']
    open_pair(service, sid, kept, other)
    get = tools.make_handler('security_scan_get', lambda: service)

    def turn_end():
        return recovery.continue_scan(service, service.store.get_scan(sid), interrupted=False, inject=lambda text: True)

    def validator_turn(candidate):
        """The injected turn reads `next` through the tool (clearing the queued continue) and a validator answers."""
        get({'scan_id': sid, 'section': 'next'})
        step = service.next_step(sid)
        assert step['step'] == 'validate' and step['items'][0]['kind'] == 'duplicate'
        result = service.submit_worker_result(worker(plan, [candidate], attempt=step['attemptId'], role='validator'))
        assert result['rejected'] == []
        assert next(a for a in service._attempts(sid) if a['attempt_id'] == step['attemptId'])['status'] == 'accepted'

    actions = []
    for _ in range(recovery.CAP):
        actions.append(turn_end())
        validator_turn(second)   # an accepted result that says nothing about the pair
    assert actions == ['scheduled'] * recovery.CAP
    assert turn_end() == 'capped'
    assert service.store.get_scan(sid)['reason'] == recovery.PAUSED_STUCK
    assert turn_end() == 'capped'
    # A real verdict is progress: the count starts again and the pause is lifted.
    second['provenance']['distinctFrom'] = [kept]
    validator_turn(second)
    assert service.next_step(sid)['step'] == 'chains'
    assert turn_end() == 'scheduled'
    scan = service.store.get_scan(sid)
    assert scan['reason'] is None and scan['options']['autoResume']['count'] == 1


@pytest.mark.parametrize('cancel_first', [True, False], ids=['cancel-then-resume', 'resume'])
@pytest.mark.parametrize('held', ['capped', 'stopped-by-user'])
def test_an_explicit_resume_lets_automatic_continue_run_again_with_the_pair_still_open(tmp_path, held, cancel_first):
    service, plan, first, second, kept, other = checked_pair(tmp_path)
    sid = plan['scanId']
    open_pair(service, sid, kept, other)
    get = tools.make_handler('security_scan_get', lambda: service)

    def turn_end(interrupted=False):
        return recovery.continue_scan(service, service.store.get_scan(sid), interrupted=interrupted, inject=lambda text: True)

    if held == 'capped':
        for _ in range(recovery.CAP):
            assert turn_end() == 'scheduled'
            get({'scan_id': sid, 'section': 'next'})   # the injected turn looked at the scan and settled nothing
    else:
        assert turn_end(interrupted=True) == 'stopped-by-user'
    assert turn_end() == held
    # No pair was settled, so the pair-based key is unchanged: the resume itself must lift the hold.
    if cancel_first:
        service.cancel(sid)
    service.resume(sid)
    step = service.next_step(sid)
    assert step['step'] == 'validate' and step['items'][0]['kind'] == 'duplicate'
    assert turn_end() == 'scheduled'
    scan = service.store.get_scan(sid)
    assert scan['reason'] is None and scan['options']['autoResume']['count'] == 1
