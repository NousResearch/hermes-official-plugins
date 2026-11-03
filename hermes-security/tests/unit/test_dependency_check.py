"""Dependency check: pinned download, advisory leads, runtime end of life, reporting."""
import hashlib
import io
import json
import threading

import pytest

from hermes_security import dependencies
from hermes_security.dependencies import eol, osv
from hermes_security.service import SecurityService


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def pinned(monkeypatch, payload):
    """Pin a fake build whose hash is *payload*'s, and return a counting opener."""
    monkeypatch.setitem(osv.BINARIES, ('testos', 'amd64'), ('osv-scanner_test', hashlib.sha256(payload).hexdigest()))
    calls = []

    def opener(url, timeout):
        calls.append(url)
        return Response(payload)
    return opener, calls


def test_download_once_then_cache_and_refetch_tampered_copy(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_SECURITY_OSV_SCANNER', raising=False)
    opener, calls = pinned(monkeypatch, b'scanner-bytes')
    path, info = osv.ensure_scanner(tmp_path, opener=opener, key=('testos', 'amd64'))
    assert info['source'] == 'downloaded' and path.read_bytes() == b'scanner-bytes'
    assert calls == [osv.RELEASE_URL.format(version=osv.OSV_VERSION, name='osv-scanner_test')]
    assert osv.ensure_scanner(tmp_path, opener=opener, key=('testos', 'amd64'))[1]['source'] == 'cached'
    path.write_bytes(b'tampered')
    assert osv.ensure_scanner(tmp_path, opener=opener, key=('testos', 'amd64'))[1]['source'] == 'downloaded'
    assert path.read_bytes() == b'scanner-bytes' and len(calls) == 2


def test_hash_mismatch_is_never_kept(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_SECURITY_OSV_SCANNER', raising=False)
    opener, _ = pinned(monkeypatch, b'expected')
    monkeypatch.setitem(osv.BINARIES, ('testos', 'amd64'), ('osv-scanner_test', hashlib.sha256(b'other').hexdigest()))
    with pytest.raises(osv.ScannerUnavailable, match='hash check'):
        osv.ensure_scanner(tmp_path, opener=opener, key=('testos', 'amd64'))
    assert not any(p.is_file() for p in (tmp_path / 'tools').rglob('*'))


def test_platforms_are_pinned_and_unknown_ones_refused():
    assert osv.platform_key('Windows', 'ARM64') == ('windows', 'arm64')
    assert osv.platform_key('Linux', 'x86_64') == ('linux', 'amd64')
    assert osv.platform_key('Darwin', 'arm64') == ('darwin', 'arm64')
    assert all(len(sha) == 64 for _, sha in osv.BINARIES.values())
    with pytest.raises(osv.ScannerUnavailable):
        osv.platform_key('Plan9', 'mips')


REPORT = {'results': [
    {'source': {'path': 'REPO/web/package-lock.json'}, 'packages': [
        {'package': {'name': 'lodash', 'version': '4.17.15', 'ecosystem': 'npm'},
         'groups': [{'ids': ['GHSA-p6mc-m468-83gw'], 'max_severity': '7.4'}],
         'vulnerabilities': [{'id': 'GHSA-p6mc-m468-83gw', 'aliases': ['CVE-2020-8203'], 'summary': 'Prototype pollution',
                              'affected': [{'package': {'name': 'lodash'}, 'ranges': [{'events': [{'introduced': '0'}, {'fixed': '4.17.19'}]}]}]}]},
        {'package': {'name': 'left-pad', 'version': '1.3.0', 'ecosystem': 'npm'}, 'vulnerabilities': []}]},
    {'source': {'path': 'REPO/node_modules/x/package-lock.json'}, 'packages': []}]}


def report_for(root):
    return json.loads(json.dumps(REPORT).replace('REPO', str(root).replace('\\', '/')))


def test_summary_keeps_inventory_manifests_and_ranks_by_severity(tmp_path):
    summary = dependencies.summarize(report_for(tmp_path), tmp_path, {'web/package-lock.json'})
    assert summary['packages'] == 2 and summary['vulnerablePackages'] == 1 and summary['outsideInventory'] == 1
    lead = summary['leads'][0]
    assert (lead['name'], lead['severity'], lead['path']) == ('lodash', 'high', 'web/package-lock.json')
    assert lead['advisories'][0]['fixed'] == ['4.17.19'] and lead['advisories'][0]['aliases'] == ['CVE-2020-8203']


def test_runtime_declarations_and_end_of_life(tmp_path):
    files = {'.nvmrc': '18\n', 'api/pyproject.toml': '[project]\nrequires-python = ">=3.9"\n',
             'Dockerfile': 'FROM python:3.12-slim\n', 'go.mod': 'module x\n\ngo 1.22\n',
             'node_modules/pkg/package.json': '{"engines": {"node": ">=4"}}'}
    found = eol.declarations(files, files.get)
    assert ('nodejs', '18', '.nvmrc') in found and ('python', '>=3.9', 'api/pyproject.toml') in found
    assert ('python', '3.12-slim', 'Dockerfile') in found and ('go', '1.22', 'go.mod') in found
    assert not any(path.startswith('node_modules/') for *_, path in found)
    tables = {'nodejs': [{'name': '18', 'isEol': True, 'eolFrom': '2025-04-30', 'latest': {'name': '18.20.8'}}],
              'python': [{'name': '3.9', 'isEol': True, 'eolFrom': '2025-10-31'}, {'name': '3.12', 'isEol': False}],
              'go': [{'name': '1.22', 'isEol': True}]}

    def opener(url, timeout):
        product = url.rsplit('/', 1)[1]
        if product not in tables:
            raise OSError('offline')
        return Response(json.dumps({'result': {'releases': tables[product]}}).encode())
    rows = {(r['runtime'], r['path']): r for r in eol.check_runtimes(files, files.get, opener=opener)}
    assert rows[('nodejs', '.nvmrc')]['status'] == 'end_of_life' and rows[('nodejs', '.nvmrc')]['eolDate'] == '2025-04-30'
    assert rows[('python', 'Dockerfile')]['status'] == 'supported'
    # '>=3.9' also permits supported releases, so it only allows an end-of-life minimum.
    assert rows[('python', 'api/pyproject.toml')]['status'] == 'allows_end_of_life'


def test_turned_off_by_environment_or_option(tmp_path, monkeypatch):
    args = dict(root=tmp_path, inventory=[], data_dir=tmp_path, work_dir=tmp_path / 'w', read=lambda p: None)
    assert dependencies.check_dependencies(**args)['status'] == 'disabled'   # conftest sets the env var
    monkeypatch.delenv('HERMES_SECURITY_DEPENDENCY_CHECK')
    assert dependencies.check_dependencies(enabled=False, **args)['reason'] == 'turned off for this scan'


def test_unavailable_scanner_is_not_run_never_clean(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_SECURITY_DEPENDENCY_CHECK')
    monkeypatch.setenv('HERMES_SECURITY_OSV_SCANNER', str(tmp_path / 'missing.exe'))
    receipt = dependencies.check_dependencies(root=tmp_path, inventory=[], data_dir=tmp_path, work_dir=tmp_path / 'w',
                                              read=lambda p: None, opener=lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    assert receipt['status'] == 'not_run' and 'HERMES_SECURITY_OSV_SCANNER' in receipt['reason']
    assert dependencies.describe(receipt).startswith('Dependency check did not run')


def test_a_scope_without_manifests_is_not_reported_clean(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_SECURITY_DEPENDENCY_CHECK')
    monkeypatch.setattr(osv, 'ensure_scanner', lambda data_dir, opener=None: ('osv', {'source': 'cached'}))
    monkeypatch.setattr(osv, 'run_scanner', lambda exe, root, work: (report_for(tmp_path), {'exitCode': 1}))
    receipt = dependencies.check_dependencies(root=tmp_path, inventory=[{'path': 'tools/a.py'}], data_dir=tmp_path,
                                              work_dir=tmp_path / 'w', read=lambda p: None)
    assert receipt['status'] == 'no_sources' and receipt['outsideInventory'] == 2
    assert '2 outside it not counted' in receipt['reason']
    assert dependencies.describe(receipt).startswith('Dependency check found nothing to check')


def fake_checker(receipt):
    def check(**kwargs):
        return {**receipt, 'leads': [dict(l) for l in receipt.get('leads', [])]}
    return check


def ok_receipt():
    summary = dependencies.summarize(json.loads(json.dumps(REPORT).replace('REPO/web/', '')), '.', {'package-lock.json'})
    return {'status': 'ok', 'reason': None, 'tool': {'name': 'osv-scanner', 'version': osv.OSV_VERSION, 'sha256': 'a' * 64, 'source': 'cached'},
            'network': ['api.osv.dev'], 'runtimes': [{'runtime': 'nodejs', 'declared': '18', 'path': '.nvmrc', 'cycle': '18',
                                                      'status': 'end_of_life', 'eolDate': '2025-04-30', 'latest': None}],
            'runtimeStatus': 'ok', **summary}


def scan_with(tmp_path, receipt):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'package-lock.json').write_text('{}\n')
    (root / 'app.js').write_text('module.exports = 1\n')
    service = SecurityService(tmp_path / 'data', dependency_checker=fake_checker(receipt))
    return service, service.start_scan(path=str(root))


def test_leads_reach_the_area_packet_and_the_sealed_report(tmp_path):
    service, plan = scan_with(tmp_path, ok_receipt())
    assert plan['dependencyCheck']['vulnerablePackages'] == 1 and 'leads' not in plan['dependencyCheck']
    area = next(p for p in plan['packets'] if p.get('review') == 'area')
    assert area['dependencyLeads']['items'][0]['name'] == 'lodash'
    section = service.get_scan(plan['scanId'], 'dependencies')
    assert section['total'] == 1 and section['check']['status'] == 'ok'
    events = [e['kind'] for e in service.activity(plan['scanId'])['items']]
    assert events.index('dependencies.checked') < events.index('scan.started')
    result = service.finalize(plan['scanId'])
    assert result['manifest']['dependencyCheck']['leads'][0]['name'] == 'lodash'
    report = service.get_scan(plan['scanId'], 'report')['content']
    assert '## Dependency check' in report and 'lodash' in report and 'past end of life' in report


def test_a_check_that_could_not_run_makes_the_scan_partial(tmp_path):
    receipt = {'status': 'not_run', 'reason': 'could not download osv-scanner: offline', 'leads': [], 'runtimes': []}
    service, plan = scan_with(tmp_path, receipt)
    result = service.finalize(plan['scanId'])
    assert result['status'] == 'partial'
    report = service.get_scan(plan['scanId'], 'report')['content']
    assert 'Not run:' in report and 'could not download osv-scanner' in report


def repo(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'package-lock.json').write_text('{}\n')
    (root / 'app.js').write_text('module.exports = 1\n')
    return str(root)


def area_leads(service, scan_id):
    area = next(p for p in service._packets(service._scan(scan_id)) if p.get('review') == 'area')
    return area.get('dependencyLeads')


def test_background_check_answers_at_once_and_holds_review_until_leads_are_saved(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_SECURITY_DEPENDENCY_CHECK')
    release = threading.Event()

    def slow(**kwargs):
        release.wait(10)
        return fake_checker(ok_receipt())(**kwargs)
    service = SecurityService(tmp_path / 'data', dependency_checker=slow)
    plan = service.start_scan(path=repo(tmp_path), background_dependencies=True)
    sid = plan['scanId']
    assert plan['status'] == 'created' and plan['dependencyCheck'] is None
    assert service.next_step(sid)['step'] == 'wait' and service.stall(service._scan(sid)) is None
    thread = service._dependency_threads[sid]
    release.set()
    thread.join(10)
    assert service._scan(sid)['status'] == 'awaiting_analysis'
    assert service.next_step(sid)['step'] == 'dispatch'
    assert area_leads(service, sid)['items'][0]['name'] == 'lodash'
    events = [e['kind'] for e in service.activity(sid)['items']]
    assert events.index('dependencies.started') < events.index('dependencies.checked') < events.index('scan.started')


def test_a_check_lost_to_a_gateway_restart_runs_again_on_the_next_read(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_SECURITY_DEPENDENCY_CHECK')
    calls = []

    def check(**kwargs):
        calls.append(1)
        return fake_checker(ok_receipt())(**kwargs)
    first = SecurityService(tmp_path / 'data', dependency_checker=check)
    monkeypatch.setattr(first, '_start_dependency_check', lambda scan_id: None)  # died before the check ran
    sid = first.start_scan(path=repo(tmp_path), background_dependencies=True)['scanId']
    restarted = SecurityService(tmp_path / 'data', dependency_checker=check)
    assert restarted.next_step(sid)['step'] == 'wait'
    thread = restarted._dependency_threads.get(sid)
    if thread:
        thread.join(10)
    assert calls == [1] and restarted.next_step(sid)['step'] == 'dispatch'


def test_a_crashed_background_check_is_recorded_not_run_and_review_goes_on(tmp_path, monkeypatch):
    monkeypatch.delenv('HERMES_SECURITY_DEPENDENCY_CHECK')

    def broken(**kwargs):
        raise RuntimeError('boom')
    service = SecurityService(tmp_path / 'data', dependency_checker=broken)
    sid = service.start_scan(path=repo(tmp_path), background_dependencies=True)['scanId']
    thread = service._dependency_threads.get(sid)
    if thread:
        thread.join(10)
    check = service.get_scan(sid, 'dependencies')['check']
    assert check['status'] == 'not_run' and 'boom' in check['reason']
    assert service.next_step(sid)['step'] == 'dispatch'


def test_a_turned_off_check_stays_synchronous(tmp_path):
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=repo(tmp_path), background_dependencies=True)
    assert plan['status'] == 'awaiting_analysis' and not service._dependency_threads

def test_packet_leads_fit_in_one_tool_page():
    # A monorepo lockfile can carry hundreds of advisories; a packet that embeds them all is too
    # big for security_scan_get, which pushed workers onto the CLI. The rest stay in section dependencies.
    from hermes_security.tools import MAX_RESULT_CHARS
    lead = {'path': 'package-lock.json', 'ecosystem': 'npm', 'summary': 'x' * 1000}
    receipt = {'leads': [dict(lead, name=f'pkg{i}') for i in range(100)]}
    leads, total = dependencies.leads_for(receipt, ['package-lock.json'])
    assert total == 100 and 0 < len(leads) < 100
    assert len(json.dumps(leads)) < MAX_RESULT_CHARS // 4
