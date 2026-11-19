# Modified from Apache-2.0 source scripts/finalize_scan_contract.py (see NOTICE).
"""SARIF 2.1.0 projection, without network or target filesystem access."""
from __future__ import annotations

from urllib.parse import quote
from .copy import _reportable

_LEVELS = {'critical': 'error', 'high': 'error', 'medium': 'warning', 'low': 'note', 'informational': 'note'}
_SCORES = {'critical': 9.5, 'high': 8.0, 'medium': 5.0, 'low': 2.0, 'informational': 0.0}


def render_sarif(manifest: dict, findings_doc: dict) -> dict:
    """Return deterministic rules and reportable results with stable fingerprints."""
    findings = _reportable(findings_doc)
    grouped = {}
    for finding in findings:
        grouped.setdefault(finding['ruleId'], []).append(finding)
    rule_ids = sorted(grouped)
    rules = []
    for rule_id in rule_ids:
        group = grouped[rule_id]
        tags = sorted({'security', 'security-severity'} | {cwe for f in group for cwe in f.get('taxonomy', {}).get('cwe', [])})
        rules.append({'id': rule_id, 'shortDescription': {'text': group[0]['title']},
                      'properties': {'tags': tags, 'security-severity': str(max(_SCORES[f['severity']['level']] for f in group))}})
    results = []
    for finding in findings:
        locations = []
        for loc in finding.get('locations', []):
            locations.append({'physicalLocation': {'artifactLocation': {'uri': quote(loc['path'], safe='/')},
                              'region': {'startLine': loc['startLine'], 'endLine': loc.get('endLine', loc['startLine'])}}})
        results.append({'ruleId': finding['ruleId'], 'ruleIndex': rule_ids.index(finding['ruleId']),
                        'level': _LEVELS[finding['severity']['level']], 'message': {'text': finding['summary']},
                        'locations': locations, 'partialFingerprints': {'hermesSecurity/v1': finding['fingerprints']['primary']},
                        'properties': {'evidenceState': finding['evidenceState'], 'findingId': finding['findingId'],
                                       'severity': finding['severity']['level']}})
    return {'$schema': 'https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/schemas/sarif-schema-2.1.0.json',
            'version': '2.1.0', 'runs': [{'tool': {'driver': {'name': 'hermes-security', 'version': manifest.get('producer', {}).get('version', '0.1.0'), 'rules': rules}},
                                      'results': results, 'properties': {'scanId': manifest.get('scanId'), 'status': manifest.get('status')}}]}
