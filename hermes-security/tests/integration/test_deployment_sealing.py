"""Offline finalizer and schema checks for optional deployment coverage."""
import json
from pathlib import Path

import pytest

from hermes_security.reports import finalize_bundle
from hermes_security.reports.finalize import ReportError
from test_deployment_evidence import evidence, prepared


def test_worker_and_saved_deployment_detail_schemas_are_identical():
    schemas = Path(__file__).parents[2] / 'hermes_security' / 'schemas'
    worker = json.loads((schemas / 'worker-result.schema.json').read_text(encoding='utf-8'))
    coverage = json.loads((schemas / 'coverage.schema.json').read_text(encoding='utf-8'))
    assert worker['$defs']['deploymentEvidence'] == coverage['$defs']['deploymentEvidence']


def test_offline_finalizer_refuses_pending_and_downgrades_false_complete_deployment(tmp_path):
    service, plan = prepared(tmp_path)
    sid = plan['scanId']
    final = service.finalize(sid)
    directory = Path(final['artifactDir'])
    load = lambda name: json.loads((directory / name).read_text(encoding='utf-8'))
    docs = dict(manifest=load('scan-manifest.json'), findings_doc=load('findings.json'),
                coverage=load('coverage.json'), chains_doc=load('chains.json'))
    row = evidence()
    unit = {'unitId': row['unit'], 'kind': 'deployment', 'state': row['state'],
            'reason': row['note'], 'evidence': [], 'deployment': row['deployment']}
    docs['coverage']['units'].append(unit)
    with pytest.raises(ReportError, match='deployment evidence.*disposition'):
        finalize_bundle(**docs)
    # Even a caller that falsely claims all coverage is complete cannot hide deferral.
    unit['state'] = 'deferred'
    unit['deployment'].update(disposition='deferred', reviewNote='Image configuration and isolation evidence unavailable.')
    result = finalize_bundle(**docs)
    assert json.loads(result['coverage.json'])['completeness'] == 'partial'
    assert json.loads(result['scan-manifest.json'])['status'] == 'partial'
    assert result == finalize_bundle(**docs)  # exact deterministic bytes, no target I/O
