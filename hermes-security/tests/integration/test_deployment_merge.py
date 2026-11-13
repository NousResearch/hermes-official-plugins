"""Same-boundary reviews conserve evidence, using only temporary scan records."""
from copy import deepcopy

import pytest

from hermes_security.errors import ValidationError
from test_deployment_evidence import evidence, prepared
from test_end_to_end_static import worker


def observe(service, plan, name, fact):
    row = evidence()
    row['note'] = 'Observation ' + name
    row['deployment']['missingEvidence'] = [fact]
    service.submit_worker_result(worker(plan, [], attempt='att_' + name, coverage=[row]))
    return row


def boundary(service, sid):
    return next(u for u in service.coverage(sid)['units'] if u['kind'] == 'deployment')


@pytest.mark.parametrize('order', [('image', 'network'), ('network', 'image')])
def test_same_boundary_observations_survive_submission_and_real_seal(tmp_path, order):
    service, plan = prepared(tmp_path)
    sid = plan['scanId']
    row = evidence()
    for name in order:
        row = observe(service, plan, name, name + ' evidence')
    merged = boundary(service, sid)
    assert set(merged['deployment']['missingEvidence']) == {'image evidence', 'network evidence'}
    assert set(merged['evidence']) == {'att_image', 'att_network'}
    assert all('Observation ' + name in merged['reason'] for name in order)
    with pytest.raises(ValidationError, match='deployment evidence'):
        service.finalize(sid)
    # A parent deferring only the last observation cannot erase the other gap.
    row['state'] = 'deferred'
    row['deployment'].update(disposition='deferred', reviewNote='Evidence unavailable in this source review.')
    service.checkpoint(sid, coverage=[row])
    final = service.finalize(sid)
    assert final['status'] == 'partial' and final['verified']['ok']
    report = service.get_scan(sid, 'report')['content']
    assert 'image evidence' in report and 'network evidence' in report
    assert set(boundary(service, sid)['evidence']) >= {'att_image', 'att_network'}


@pytest.mark.parametrize('disposition', ['resolved', 'deferred'])
def test_reopening_keeps_prior_facts_and_references(tmp_path, disposition):
    service, plan = prepared(tmp_path)
    sid = plan['scanId']
    row = observe(service, plan, 'image', 'image evidence')
    row['state'] = 'reviewed' if disposition == 'resolved' else 'deferred'
    row['deployment'].update(disposition=disposition, reviewNote='Parent reviewed image requirement.')
    if disposition == 'resolved':
        row['deployment']['missingEvidence'] = []
    service.checkpoint(sid, coverage=[row])
    observe(service, plan, 'network', 'network evidence')
    merged = boundary(service, sid)
    assert merged['state'] == 'unknown'
    assert set(merged['deployment']['missingEvidence']) == {'image evidence', 'network evidence'}
    assert set(merged['evidence']) >= {'att_image', 'att_network'}
    assert 'Parent reviewed image requirement.' in merged['deployment']['reviewNote']


@pytest.mark.parametrize('field', ['component', 'ingress', 'actor', 'asset'])
def test_same_key_incompatible_identity_rejected_without_coverage_changes(tmp_path, field):
    service, plan = prepared(tmp_path)
    sid = plan['scanId']
    observe(service, plan, 'image', 'image evidence')
    before = deepcopy(service.store.coverage_units(sid))
    row = evidence()
    row['deployment'][field] = 'public' if field == 'ingress' else 'different identity'
    with pytest.raises(ValidationError, match='identity'):
        service.checkpoint(sid, coverage=[{'unit': 'file:deploy/service.yml', 'state': 'deferred'}, row])
    assert service.store.coverage_units(sid) == before


@pytest.mark.parametrize('gap', ['worker', 'lane', 'deployment_only'])
def test_all_files_read_is_not_repository_completion_in_report(tmp_path, gap):
    service, plan = prepared(tmp_path, extra=[evidence()], skip_baseline=(gap == 'worker'))
    sid = plan['scanId']
    row = evidence()
    row['state'] = 'deferred'
    row['deployment'].update(disposition='deferred', reviewNote='Deployment facts unavailable.')
    service.checkpoint(sid, coverage=[row])
    if gap == 'worker':
        assert any('has no accepted result' in g for g in service.coverage(sid)['gaps'])
    elif gap == 'lane':
        service.checkpoint(sid, coverage=[{'unit': 'lane:A01:2025', 'state': 'deferred', 'note': 'Lane unfinished.'}])
    final = service.finalize(sid)
    assert final['status'] == 'partial'
    report = service.get_scan(sid, 'report')['content']
    assert 'All in-scope files read; deployment security unresolved.' in report
    assert 'Repository review complete' not in report


def test_store_concurrent_observations_merge_under_write_transaction(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from hermes_security.store.db import SecurityStore

    service, plan = prepared(tmp_path)
    sid = plan['scanId']
    gate = Barrier(2)

    def submit(name):
        # Independent Store connections bypass the service lease deliberately, proving
        # the durable read/merge/write seam itself serializes reviewers.
        store = SecurityStore(service.store.data_dir)
        row = evidence()
        row['note'] = 'Concurrent observation ' + name
        row['deployment']['missingEvidence'] = [name + ' evidence']
        unit = service._coverage_input(service._scan(sid), [row], 'att_' + name, worker=True)
        gate.wait(timeout=10)
        store.record_coverage(sid, unit, source='att_' + name, monotonic=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(submit, ['image', 'network']))
    merged = boundary(service, sid)
    assert set(merged['deployment']['missingEvidence']) == {'image evidence', 'network evidence'}
    assert set(merged['evidence']) == {'att_image', 'att_network'}


def test_merged_evidence_bounds_fail_without_overwriting_prior_facts(tmp_path):
    service, plan = prepared(tmp_path)
    sid = plan['scanId']
    row = observe(service, plan, 'image', 'image evidence')
    before = deepcopy(service.store.coverage_units(sid))
    row['deployment']['missingEvidence'] = ['fact ' + str(i) for i in range(16)]
    with pytest.raises(ValidationError, match='bounds'):
        service.checkpoint(sid, coverage=[row])
    assert service.store.coverage_units(sid) == before
