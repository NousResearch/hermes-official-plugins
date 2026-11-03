"""The dependency check becomes one finding rated at the worst severity it contains."""
import json

from hermes_security.dependencies import finding
from hermes_security.domain.validate import validate_document
from hermes_security.reports import validate_finding_sections
from hermes_security.service import SecurityService

SCAN, REPO = 'scan_' + '1' * 24, '2' * 24


def lead(name, severity, path='pnpm-lock.yaml'):
    return {'path': path, 'ecosystem': 'npm', 'name': name, 'version': '1.0.0', 'severity': severity,
            'advisories': [{'id': 'GHSA-' + name, 'aliases': [], 'summary': 's', 'severity': severity, 'fixed': ['1.0.1']}]}


def receipt(leads, runtimes=(), status='ok'):
    return {'status': status, 'tool': {'name': 'osv-scanner', 'version': '2.7.0'}, 'packages': 500,
            'vulnerablePackages': len(leads), 'advisories': len(leads), 'sources': sorted({l['path'] for l in leads}),
            'leads': list(leads), 'runtimes': list(runtimes), 'truncated': False}


def build(r):
    return finding.build(r, scan_id=SCAN, repo_key=REPO)


def test_two_hundred_packages_rate_at_their_worst_severity():
    leads = [lead(f'low{i}', 'low') for i in range(150)] + [lead(f'mid{i}', 'medium') for i in range(50)]
    result = build(receipt(leads))
    assert result['severity']['level'] == 'medium'
    assert '50 medium, 150 low' in result['severity']['rationale']
    assert result['title'] == 'Dependencies with known vulnerabilities (worst: medium)'
    # Most severe first, the rest pointed at the report.
    assert result['rootCause'].split('\n- ')[1].startswith('mid')
    assert '175 more packages are listed' in result['rootCause']


def test_one_critical_package_makes_the_finding_critical():
    assert build(receipt([lead('a', 'low'), lead('b', 'critical')]))['severity']['level'] == 'critical'


def test_unrated_advisories_and_end_of_life_runtimes_count_as_low():
    assert build(receipt([lead('a', 'unknown')]))['severity']['level'] == 'low'
    eol = {'runtime': 'nodejs', 'declared': '18', 'path': '.nvmrc', 'cycle': '18', 'status': 'end_of_life',
           'eolDate': '2025-04-30', 'latest': '24.1.0'}
    only_runtime = build(receipt([], [eol]))
    assert only_runtime['severity']['level'] == 'low' and '.nvmrc' in only_runtime['rootCause']
    assert only_runtime['locations'] == [{'path': '.nvmrc', 'startLine': 1, 'role': 'manifest'}]


def test_nothing_found_or_not_run_makes_no_finding():
    assert build(receipt([])) is None
    assert build(receipt([lead('a', 'high')], status='not_run')) is None
    assert build(None) is None


def test_the_finding_is_a_valid_reportable_finding_with_a_stable_identity():
    result = build(receipt([lead('a', 'high'), lead('b', 'low', path='web/package-lock.json')]))
    doc = {'documentType': 'hermes-security.findings', 'schemaVersion': '1.0', 'scanId': SCAN, 'findings': [result]}
    assert validate_document('hermes-security.findings', doc) == []
    assert validate_finding_sections(result) == []
    assert result['evidenceState'] == 'source_supported' and result['confidence']['level'] == 'low'
    assert result['proofGaps'] and {l['path'] for l in result['locations']} == {'pnpm-lock.yaml', 'web/package-lock.json'}
    assert build(receipt([lead('z', 'low')]))['findingId'] == result['findingId']


def test_finalize_adds_the_finding_to_the_index_and_the_report(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'pnpm-lock.yaml').write_text('lockfileVersion: 9\n')
    (root / 'app.js').write_text('module.exports = 1\n')
    r = receipt([lead('lodash', 'medium'), lead('ms', 'low')])
    service = SecurityService(tmp_path / 'data', dependency_checker=lambda **kw: json.loads(json.dumps(r)))
    sid = service.start_scan(path=str(root))['scanId']
    result = service.finalize(sid)
    assert result['manifest']['counts']['medium'] == 1
    items = service.list_findings()['items']
    assert len(items) == 1 and items[0]['title'].startswith('Dependencies with known vulnerabilities')
    report = service.get_scan(sid, 'report')['content']
    assert 'Dependencies with known vulnerabilities (worst: medium)' in report



EOL = {'runtime': 'nodejs', 'declared': '18', 'path': '.nvmrc', 'cycle': '18', 'status': 'end_of_life',
       'eolDate': '2025-04-30', 'latest': '24.1.0'}


def valid(result):
    doc = {'documentType': 'hermes-security.findings', 'schemaVersion': '1.0', 'scanId': SCAN, 'findings': [result]}
    return validate_document('hermes-security.findings', doc) == [] and validate_finding_sections(result) == []


def test_end_of_life_runtimes_count_when_no_package_was_checked():
    # The runtime lookup runs on its own: no lockfile in scope, or OSV unavailable, still reports them.
    for status in ('no_sources', 'not_run'):
        result = build(receipt([], [EOL], status=status))
        assert result['severity']['level'] == 'low' and '.nvmrc' in result['rootCause'] and valid(result)
    # Package leads count only from a completed advisory check.
    assert 'lodash' not in build(receipt([lead('lodash', 'critical')], [EOL], status='not_run'))['rootCause']
    assert build(receipt([], [dict(EOL, status='supported')], status='no_sources')) is None


def test_a_monorepo_of_dependency_results_stays_within_the_finding_schema():
    runtimes = [dict(EOL, runtime='python', declared='3.8', cycle='3.8', path=f'apps/service{i}/Dockerfile')
                for i in range(250)]
    leads = [lead(f'pkg{i}', 'medium', path=f'apps/service{i}/package-lock.json') for i in range(100)]
    leads.append(lead('x' * 30_000, 'high'))   # package names come from the repository
    result = build(receipt(leads, runtimes))
    assert valid(result) and len(result['rootCause']) <= finding.ROOT_CAUSE_CHARS
    assert result['severity']['level'] == 'high'
    assert '76 more packages and 225 more runtimes are listed' in result['rootCause']


def test_finalize_reports_end_of_life_runtimes_when_no_lockfile_is_in_scope(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    (root / '.nvmrc').write_text('18\n')
    (root / 'app.js').write_text('module.exports = 1\n')
    r = dict(receipt([], [EOL], status='no_sources'), reason='no manifest or lockfile in the scanned scope',
             runtimeStatus='ok')
    service = SecurityService(tmp_path / 'data', dependency_checker=lambda **kw: json.loads(json.dumps(r)))
    sid = service.start_scan(path=str(root))['scanId']
    assert service.finalize(sid)['manifest']['counts']['low'] == 1
    report = service.get_scan(sid, 'report')['content']
    assert 'Dependencies with known vulnerabilities (worst: low)' in report
    assert 'Runtime past end of life: nodejs 18 in .nvmrc' in report
