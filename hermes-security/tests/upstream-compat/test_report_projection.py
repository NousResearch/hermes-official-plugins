"""Projection compatibility uses imported semantics, not the domain importer."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hermes_security.reports import render_markdown


def test_imported_upstream_sections_map_without_report_parsing():
    f = json.loads((Path(__file__).parents[1] / 'fixtures/reports/imported-finding.json').read_text())
    manifest = {'target': {'root': '/example/app', 'revision': 'abcdef'}, 'status': 'partial'}
    coverage = {'completeness': 'partial', 'gaps': ['Proxy policy is unavailable.']}
    findings = {'findings': [f]}
    text = render_markdown(manifest, findings, coverage, {'chains': [], 'broken': []})
    assert text == render_markdown(manifest, findings, coverage, {'chains': [], 'broken': []})
    for value in [f['summary'], f['rootCause'], f['attackPath']['steps'][0], f['severity']['rationale'], f['validation']['summary'], f['remediation']['summary'], f['codeEvidence'][0]['code']]:
        assert value in text
    assert 'NOT RUN' in text
    assert '### Root cause' in text and '### What could make this wrong' in text
    assert 'CWE-' not in text.split('\n\n')[1]
