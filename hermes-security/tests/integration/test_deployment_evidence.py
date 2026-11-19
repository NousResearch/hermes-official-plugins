"""Deterministic offline contracts, NOT a model discovery benchmark."""
from pathlib import Path
import shutil

import pytest

from hermes_security.errors import ValidationError
from hermes_security.reports import verify_bundle
from hermes_security.service import SecurityService
from test_end_to_end_static import worker


FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'deployment'


def evidence(ingress='private'):
    return {'unit': 'deployment:deploy/service.yml#collector-' + ingress,
            'state': 'unknown', 'note': 'Image-owned management API needs source evidence.',
            'deployment': {'component': 'collector', 'ingress': ingress,
                           'actor': 'customer-controlled workload', 'asset': 'organization-wide telemetry',
                           'missingEvidence': ['deployed image digest', 'effective listener configuration',
                                               'raw-event API capability', 'private-network policy'],
                           'disposition': 'pending', 'reviewNote': ''}}


def deployment_candidate(root, sibling):
    # Predetermined reviewer observations, not model-discovered findings.
    return {
        'ruleId': 'deployment.management-interface', 'identity': {'anchor': 'collector/management'},
        'title': 'A shared workload may read organization-wide telemetry',
        'summary': 'A customer-controlled workload may stream raw events from the private management API.',
        'rootCause': 'The management listener can cross the workload-to-telemetry trust boundary without authentication.',
        'attackPath': {'source': 'customer-controlled workload', 'sink': 'raw-event API',
                       'steps': ['Reach the private listener and request the event stream.'],
                       'controls': ['No public ingress.'], 'assumptions': ['Manifest settings describe the deployed component.']},
        'severity': {'level': 'high', 'rationale': 'Organization-wide telemetry could cross tenant boundaries.',
                     'reachability': 'Private shared-workload network', 'impact': 'Cross-tenant telemetry disclosure',
                     'likelihood': 'Depends on effective listener and network configuration',
                     'prerequisites': ['A workload able to reach the management listener.']},
        'taxonomy': {'category': 'security-misconfiguration', 'cwe': ['CWE-306'], 'owasp': ['A02:2025'], 'asvs': []},
        'confidence': {'level': 'medium', 'rationale': 'Only inert deployment configuration was reviewed.'},
        'evidenceState': {'vulnerable': 'source_supported', 'fixed': 'rejected', 'unknown': 'inconclusive'}[sibling],
        'locations': [{'path': 'deploy/service.yml', 'startLine': 1, 'endLine': len((root / 'deploy/service.yml').read_text(encoding='utf-8').splitlines())}],
        'codeEvidence': [{'id': 'ev-config', 'label': 'Synthetic deployment', 'path': 'deploy/service.yml',
                          'startLine': 1, 'endLine': len((root / 'deploy/service.yml').read_text(encoding='utf-8').splitlines()),
                          'code': ''.join((root / 'deploy/service.yml').read_text(encoding='utf-8').splitlines(keepends=True)),
                          'explanation': 'Repository configuration is the available source evidence.'}],
        'counterEvidence': ['Public ingress is absent; that alone does not defeat a private path.'] if sibling != 'fixed'
                           else ['Loopback binding and authentication defeat direct workload ingress.'],
        'proofGaps': ['Need the effective image listener and private-network isolation evidence.'] if sibling == 'unknown' else [],
        'remediation': {'summary': 'Restrict the management listener to loopback and require authentication.',
                        'regressionTests': ['A separate workload cannot read raw events.']}}


def prepared(tmp_path, sibling='unknown', extra=None, candidate=False, skip_baseline=False):
    root = tmp_path / 'repo'
    shutil.copytree(FIXTURES / sibling, root)
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=str(root), dependency_check=False)
    sid = plan['scanId']
    ids = [p['packetId'] for p in plan['packets']]
    # This single deployment area still has only the pre-existing baseline and area packet.
    assert len(ids) == 2
    for i, packet in enumerate(plan['packets']):
        if skip_baseline and i == 0:
            continue
        units = [{'unit': 'file:' + p, 'state': 'reviewed', 'note': 'Read all inert configuration.'}
                 for p in packet.get('files', [])]
        units += [{'unit': 'lane:' + lane, 'state': 'reviewed', 'note': 'Reviewed this lane.'}
                  for lane in packet['lanes']]
        if i == 1 and extra:
            units += extra
        candidates = [deployment_candidate(root, sibling)] if i == 1 and candidate else []
        payload = worker(plan, candidates, attempt='att_deploy_' + str(i), role=packet['role'], coverage=units)
        payload['packetId'] = packet['packetId']
        result = service.submit_worker_result(payload)
        assert result['rejected'] == []
        assert result['inserted'] == len(candidates)
    assert [p['packetId'] for p in service._packets(service._scan(sid))] == ids
    return service, plan


def test_unknown_deployment_requires_parent_disposition_without_more_workers(tmp_path):
    service, plan = prepared(tmp_path, extra=[evidence(), evidence('public')], candidate=True)
    sid = plan['scanId']
    coverage = service.coverage_view(sid)
    assert coverage['files']['reviewed'] == coverage['files']['total'] == 1
    assert coverage['completeness'] == 'partial'
    assert len([u for u in coverage['units'] if u['kind'] == 'deployment']) == 2
    cid = service.get_scan(sid, 'candidates')['items'][0]['candidateId']
    service.record_validations(sid, plans=[{'candidateId': cid, 'level': 'static', 'kind': 'static',
                                        'checks': {'source': True, 'control': True, 'sink': True, 'boundary': False, 'counterevidence': True}}])
    step = service.next_step(sid)
    assert step['step'] == 'finalize' and 'security_scan_checkpoint' in step['how']
    assert len(step['deploymentEvidence']) == 2
    with pytest.raises(ValidationError, match='deployment evidence.*disposition'):
        service.finalize(sid)
    # Public ingress has a defeating control; it does NOT settle direct private ingress.
    public = evidence('public')
    public.update(state='reviewed', note='No public ingress is configured in the reviewed manifest.')
    public['deployment'].update(disposition='resolved', reviewNote='Public path defeated by no ingress or port mapping.', missingEvidence=[])
    private = evidence()
    private.update(state='deferred')
    private['deployment'].update(disposition='deferred', reviewNote='Scope lacks image/config/network evidence; private path remains unresolved.')
    service.checkpoint(sid, coverage=[public, private])
    assert service.next_step(sid)['step'] == 'finalize'
    final = service.finalize(sid)
    assert final['status'] == 'partial' and final['verified']['ok']
    assert verify_bundle(Path(final['artifactDir']))['ok']
    report = service.get_scan(sid, 'report')['content']
    assert 'All in-scope files read; deployment security unresolved.' in report
    assert '## Important unresolved candidates' in report
    assert 'Need the effective image listener' in report
    assert 'customer-controlled workload' in report and 'organization-wide telemetry' in report
    assert 'raw-event API capability' in report and 'private-network policy' in report
    assert 'Public path defeated' in report and 'private path remains unresolved' in report
    assert report.index('## Deployment evidence') < report.index('## Coverage')
    assert len(service._attempts(sid)) == 2


@pytest.mark.parametrize('sibling', ['vulnerable', 'fixed'])
def test_source_resolved_deployment_siblings_finish_without_extra_round(tmp_path, sibling):
    service, plan = prepared(tmp_path, sibling, [evidence()], candidate=True)
    sid = plan['scanId']
    row = evidence()
    row.update(state='reviewed', note='Reviewed effective configuration as inert data.')
    row['deployment'].update(disposition='resolved', missingEvidence=[],
                             reviewNote=('Private listener exposes raw events without authentication.' if sibling == 'vulnerable'
                                         else 'Management listener is loopback-only and authenticated.'))
    service.checkpoint(sid, coverage=[row])
    assert service.coverage(sid)['completeness'] == 'complete'
    if sibling == 'vulnerable':
        cid = service.get_scan(sid, 'candidates')['items'][0]['candidateId']
        service.record_validations(sid, plans=[{'candidateId': cid, 'level': 'static', 'kind': 'static',
                                             'checks': {k: True for k in ('source', 'control', 'sink', 'boundary', 'counterevidence')}}])
        service.record_chains(sid)
    assert service.next_step(sid)['step'] == 'finalize'
    final = service.finalize(sid)
    assert final['manifest']['counts']['high'] == (1 if sibling == 'vulnerable' else 0)
    assert final['status'] == 'completed'
    report = service.get_scan(sid, 'report')['content']
    assert row['deployment']['reviewNote'] in report
    assert 'runtime exploitability' in report and 'NOT RUN' in report
    assert len(service._attempts(sid)) == len(plan['packets']) == 2


def test_inconclusive_claim_alone_is_not_a_deployment_coverage_gap(tmp_path):
    service, plan = prepared(tmp_path, candidate=True)
    sid = plan['scanId']
    assert service.coverage(sid)['completeness'] == 'complete'
    final = service.finalize(sid)
    assert final['status'] == 'completed'
    assert final['manifest']['counts']['retained'] == 1
    report = service.get_scan(sid, 'report')['content']
    assert 'Important unresolved candidates' in report
    assert 'Need the effective image listener' in report
    assert not any(u['kind'] == 'deployment' for u in service.coverage(sid)['units'])


@pytest.mark.parametrize('planner', [1, 2, 3, 4])
def test_deployment_questions_only_change_existing_area_briefs(tmp_path, monkeypatch, planner):
    from hermes_security.orchestration import standard
    service, plan = prepared(tmp_path)
    files = service.store.inventory(plan['scanId'])
    arguments = (plan['scanId'], files, [])
    with_questions = standard.build_packets(*arguments, planner=planner)
    assert standard._deployment_brief(['src/application.py']) == ''
    monkeypatch.setattr(standard, '_deployment_brief', lambda paths: '')
    without_questions = standard.build_packets(*arguments, planner=planner)
    assert [{k: v for k, v in p.items() if k != 'brief'} for p in with_questions] == [
        {k: v for k, v in p.items() if k != 'brief'} for p in without_questions]
    area = next(p for p in with_questions if p['review'] == 'area')
    for phrase in ('private-network ingress', 'raw-event', 'customer-controlled', 'never pull/run images', 'deployment:'):
        assert phrase in area['brief']


def test_late_deployment_observation_is_not_hidden_by_completed_file_coverage(tmp_path):
    service, plan = prepared(tmp_path, extra=[evidence()])
    sid = plan['scanId']
    resolved = evidence()
    resolved.update(state='reviewed')
    resolved['deployment'].update(disposition='resolved', missingEvidence=[], reviewNote='Reviewed the available configuration.')
    service.checkpoint(sid, coverage=[resolved])
    assert service.coverage(sid)['completeness'] == 'complete'
    payload = worker(plan, [], attempt='att_late_evidence', coverage=[evidence()])
    service.submit_worker_result(payload)
    assert service.coverage(sid)['completeness'] == 'partial'
    with pytest.raises(ValidationError, match='deployment evidence'):
        service.finalize(sid)
    assert len(service._packets(service._scan(sid))) == len(plan['packets'])


@pytest.mark.parametrize('mutation', ['unknown_path', 'missing_detail', 'worker_resolution', 'false_resolution'])
def test_invalid_deployment_evidence_never_closes_coverage(tmp_path, mutation):
    service, plan = prepared(tmp_path)
    row = evidence()
    if mutation == 'unknown_path':
        row['unit'] = 'deployment:outside.yml#collector-private'
    elif mutation == 'missing_detail':
        row.pop('deployment')
    elif mutation == 'worker_resolution':
        row['state'] = 'reviewed'
        row['deployment'].update(disposition='resolved', reviewNote='Claimed resolved.', missingEvidence=[])
    else:
        row['state'] = 'reviewed'
        row['deployment'].update(disposition='resolved', reviewNote='Still missing the image.')
    if mutation == 'worker_resolution':
        payload = worker(plan, [], attempt='att_forged', coverage=[row])
        with pytest.raises(ValidationError):
            service.submit_worker_result(payload)
    else:
        with pytest.raises(ValidationError):
            service.checkpoint(plan['scanId'], coverage=[row])
    assert not any(u['kind'] == 'deployment' for u in service.coverage(plan['scanId'])['units'])
