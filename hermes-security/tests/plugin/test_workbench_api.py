"""Real profile-local workbench HTTP acceptance; no live Hermes required."""
import importlib.util
import json
from pathlib import Path
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
PREFIX = '/api/plugins/hermes-security'


@pytest.fixture
def api(tmp_path, monkeypatch):
    import types
    # Never import/start the real gateway transport during offline tests, even
    # when earlier host-parser tests have added the Hermes source to sys.path.
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_events', types.SimpleNamespace(broadcast_plugin_event=lambda *args: None))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profile'))
    monkeypatch.chdir('/')
    spec = importlib.util.spec_from_file_location('test_security_dashboard', ROOT / 'dashboard/plugin_api.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    with TestClient(app) as client:
        yield module, client


def test_empty_workbench_reads_succeed(api):
    module, client = api
    summary = client.get(PREFIX + '/summary').json()
    assert summary['profile'] == 'default'
    assert summary['scans'] == summary['findings'] == 0
    assert summary['open'] == {}
    for path in ['/scans', '/findings', '/repositories']:
        response = client.get(PREFIX + path)
        assert response.status_code == 200, (path, response.text)
        assert response.json()['items'] == []


def test_browse_lists_folders_on_the_gateway_machine(api, tmp_path):
    module, client = api
    base = tmp_path / 'browse'
    (base / 'b-repo' / '.git').mkdir(parents=True)
    (base / 'A-dir').mkdir(); (base / '.hidden').mkdir(); (base / 'file.txt').write_text('x')
    body = client.get(PREFIX + '/browse', params={'path': str(base)}).json()
    assert body['path'] == str(base.resolve())
    assert body['parent'] == str(base.resolve().parent)
    assert body['repo'] is False and body['roots']
    assert [(i['name'], i['repo']) for i in body['items']] == [('A-dir', False), ('b-repo', True)]
    assert body['items'][1]['path'] == str(base.resolve() / 'b-repo')
    assert client.get(PREFIX + '/browse').json()['path'] == str(Path.home().resolve())
    for missing in [base / 'file.txt', base / 'nope']:
        assert client.get(PREFIX + '/browse', params={'path': str(missing)}).status_code == 400


def settled(module, scan_id, timeout=10):
    """Wait out the background inventory and dependency check a Workbench start launches, so a test
    acting on the scan doesn't race it (the thread flips status and options under the test's reads)."""
    thread = module.get_service()._dependency_threads.get(scan_id)
    if thread:
        thread.join(timeout)
    return scan_id


def test_standalone_health_and_real_scan(api, tmp_path):
    module, client = api
    assert client.get(PREFIX + '/health').json() == {'ok': True, 'plugin': 'hermes-security'}
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('print("not executed")\n')
    response = client.post(PREFIX + '/scans', json={'path': str(root)})
    assert response.status_code == 200, response.text
    scan = response.json()
    # The route answers once the background inventory has started; on a fast runner that thread can
    # finish a one-file repo before the response is built, so either status is correct here.
    assert scan['status'] in {'created', 'awaiting_analysis'}
    thread = module.get_service()._dependency_threads.get(scan['scanId'])
    if thread:
        thread.join(10)
    scan = client.get(PREFIX + '/scans/' + scan['scanId']).json()
    assert scan['status'] == 'awaiting_analysis' and scan['plan']['inventory']['files'] == 1
    assert client.get(PREFIX + '/summary').json()['scans'] == 1


@pytest.fixture
def populated(api, tmp_path):
    module, client = api
    root = tmp_path / 'repo'; root.mkdir()
    secret = 'AKIA' + 'Q7X2M9P4R8S6T1V3'
    (root / 'app.py').write_text('def get_invoice(invoice_id):\n    return invoices.get(invoice_id)\nAWS_ACCESS_KEY_ID = "' + secret + '"\n')
    # Import a real acceptance helper by file path (tests need not be packages).
    spec = importlib.util.spec_from_file_location('security_acceptance_helpers', ROOT / 'tests/integration/test_end_to_end_static.py')
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec); spec.loader.exec_module(helpers)
    service = module.get_service()
    plan = service.start_scan(path=str(root))
    candidate = helpers.candidate_for(root)
    candidate['evidenceState'] = 'source_supported'
    service.submit_worker_result(helpers.worker(plan, [candidate]))
    service.finalize(plan['scanId'])
    fid = service.list_findings()['items'][0]['finding_id']
    return module, client, root, plan['scanId'], fid, secret


def test_scan_rows_carry_counts_only_after_seal(populated, tmp_path):
    module, client, root, sid, fid, secret = populated
    rows = {r['scan_id']: r for r in client.get(PREFIX + '/scans').json()['items']}
    indexed = [f for f in client.get(PREFIX + '/findings').json()['items'] if f['scan_id'] == sid]
    assert indexed
    expected = {}
    for f in indexed:
        expected[f['severity']] = expected.get(f['severity'], 0) + 1
    assert rows[sid]['counts'] == expected
    open_sid = client.post(PREFIX + '/scans', json={'path': str(root)}).json()['scanId']
    rows = {r['scan_id']: r for r in client.get(PREFIX + '/scans').json()['items']}
    assert rows[open_sid]['counts'] is None


def test_all_reads_triage_and_exports(populated):
    module, client, root, sid, fid, secret = populated
    paths = ['/health', '/summary', '/scans', '/scans/' + sid,
             '/scans/' + sid + '/activity', '/scans/' + sid + '/coverage',
             '/findings', '/findings/' + fid, '/findings/' + fid + '/patch', '/repositories']
    paths += ['/scans/' + sid + '?section=' + s for s in ['inventory', 'workers', 'validations', 'candidates', 'findings', 'chains', 'coverage', 'activity', 'manifest', 'report']]
    for path in paths:
        response = client.get(PREFIX + path)
        assert response.status_code == 200, (path, response.text)
        assert secret not in response.text
    assert client.get(PREFIX + '/findings/' + fid + '/patch').json()['readOnly'] is True
    for state in ['closed', 'accepted_risk', 'false_positive', 'open']:
        response = client.post(PREFIX + '/findings/' + fid + '/triage', json={'state': state, 'note': 'Reviewed locally.'})
        assert response.status_code == 200
        assert response.json()['triage']['state'] == state
        assert response.json() == client.get(PREFIX + '/findings/' + fid).json()
        assert secret not in response.text
    for fmt in ['md', 'sarif', 'json', 'csv']:
        expected = module.get_service().export(sid, fmt)
        response = client.get(PREFIX + '/exports/' + sid + '/' + fmt)
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('application/json')
        assert response.json() == expected
        response = client.get(PREFIX + '/exports/' + sid + '/' + fmt + '?download=1')
        assert response.status_code == 200
        assert response.text == expected['content']
        assert expected['filename'] in response.headers['content-disposition']
        assert response.headers['content-type'].startswith(expected['contentType'])
        assert response.headers['content-disposition'] == 'attachment; filename="' + expected['filename'] + '"'
        assert secret not in response.text


def test_scan_actions_policy_and_events(api, tmp_path, monkeypatch):
    import types
    module, client = api
    events = []
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_events', types.SimpleNamespace(broadcast_plugin_event=lambda *args: events.append(args)))
    root = tmp_path / 'repo'; root.mkdir(); (root / 'a.py').write_text('x = 1\n')
    # The "Run tests to confirm findings" checkbox is the only consent needed.
    response = client.post(PREFIX + '/scans', json={'path': str(root), 'allow_tests': True})
    assert response.status_code == 200
    sid = settled(module, response.json()['scanId'])
    assert response.json()['safety_level'] == 'local-safe'
    for action, status in [('cancel', 'canceled'), ('resume', 'awaiting_analysis')]:
        response = client.post(PREFIX + '/scans/' + sid + '/' + action)
        assert response.status_code == 200
        assert response.json()['status'] == status
        # A running scan's timing is live (durationSeconds can tick between the two reads).
        live = {k: v for k, v in client.get(PREFIX + '/scans/' + sid).json().items() if k != 'timing'}
        assert {k: v for k, v in response.json().items() if k != 'timing'} == live
    assert events == [('hermes-security', 'scan.updated', {'scanId': sid})] * 3
    rows = client.get(PREFIX + '/scans/' + sid + '/activity', params={'limit': 1}).json()['items']
    assert len(rows) == 1
    after = rows[0]['id']
    assert all(r['id'] > after for r in client.get(PREFIX + '/scans/' + sid + '/activity', params={'after_id': after}).json()['items'])
    def broken(*args):
        raise RuntimeError('no transport')
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_events', types.SimpleNamespace(broadcast_plugin_event=broken))
    assert client.post(PREFIX + '/scans/' + sid + '/cancel').status_code == 200


def test_sql_filters_and_pagination(populated):
    module, client, root, sid, fid, secret = populated
    service = module.get_service()
    for mode in ['standard', 'deep', 'deep']:
        service.start_scan(path=str(root), mode=mode)
    response = client.get(PREFIX + '/scans', params={'q': str(root), 'mode': 'deep', 'status': 'awaiting_analysis', 'limit': 1, 'offset': 1}).json()
    assert response['total'] == 2 and len(response['items']) == 1
    assert response['items'][0]['mode'] == 'deep'
    assert client.get(PREFIX + '/scans', params={'mode': 'deep', 'offset': 2}).json() == {'items': [], 'total': 2}
    row = service.list_findings()['items'][0]
    filters = {'q': row['title'], 'severity': row['severity'], 'evidenceState': row['evidence_state'],
               'validationLevel': 'static', 'repo': row['repo_key'], 'owasp': row['owasp'][0],
               'chained': 'false', 'status': 'open'}
    for key, value in filters.items():
        result = client.get(PREFIX + '/findings', params={key: value, 'limit': 1}).json()
        assert result['total'] == 1 and len(result['items']) == 1, (key, result)
        assert 'data' not in result['items'][0] and 'codeEvidence' not in result['items'][0]
        missing = 'true' if key == 'chained' else 'does-not-match'
        assert client.get(PREFIX + '/findings', params={key: missing}).json()['total'] == 0
    assert client.get(PREFIX + '/findings', params={**filters, 'offset': 1}).json() == {'items': [], 'total': 1}
    assert client.get(PREFIX + '/findings', params={**filters, 'limit': 0}).json() == {'items': [], 'total': 1}
    assert client.get(PREFIX + '/repositories', params={'offset': 1}).json() == {'items': [], 'total': 1}
    assert client.get(PREFIX + '/scans/' + sid, params={'section': 'findings', 'offset': 1}).json() == {'items': [], 'total': 1}


def test_findings_sort_worst_severity_first_before_pagination(populated):
    module, client, root, sid, fid, secret = populated
    store = module.get_service().store
    # Insertion order is oldest -> newest, deliberately not severity order.
    rows = [('crit-old', 'critical', 'candidate'), ('info', 'informational', 'source_supported'),
            ('low', 'low', 'candidate'), ('unknown', 'unknown', 'source_supported'),
            ('medium', 'medium', 'candidate'), ('high', 'high', 'source_supported'),
            ('crit-new', 'critical', 'source_supported')]
    with store.transaction() as conn:
        columns = [r[1] for r in conn.execute('PRAGMA table_info(findings)')]
        template = dict(conn.execute('SELECT * FROM findings WHERE finding_id=?', (fid,)).fetchone())
        conn.execute('DELETE FROM findings')
        for name, severity, evidence in rows:
            values = {**template, 'finding_id': 'f-' + name, 'occurrence_id': 'o-' + name,
                      'severity': severity, 'evidence_state': evidence}
            conn.execute('INSERT INTO findings(' + ','.join(columns) + ') VALUES(' + ','.join('?' * len(columns)) + ')',
                         [values[c] for c in columns])
    order = ['f-crit-new', 'f-crit-old', 'f-high', 'f-medium', 'f-low', 'f-info', 'f-unknown']
    seen = []
    for offset in (0, 3, 6):
        page = client.get(PREFIX + '/findings', params={'limit': 3, 'offset': offset}).json()
        assert page['total'] == 7
        assert all('data' not in item and 'codeEvidence' not in item for item in page['items'])
        seen += [item['finding_id'] for item in page['items']]
    assert seen == order
    # evidenceState stays a server-side filter applied before ordering and LIMIT.
    page = client.get(PREFIX + '/findings', params={'evidenceState': 'candidate', 'limit': 2}).json()
    assert page['total'] == 3
    assert [item['finding_id'] for item in page['items']] == ['f-crit-old', 'f-medium']
    page = client.get(PREFIX + '/findings', params={'evidenceState': 'source_supported', 'limit': 2, 'offset': 2}).json()
    assert page['total'] == 4
    assert [item['finding_id'] for item in page['items']] == ['f-info', 'f-unknown']


@pytest.mark.parametrize('path', ['/scans?limit=101', '/findings?offset=-1', '/repositories?limit=101', '/scans/x?limit=101', '/scans/x/activity?limit=501', '/scans/x/activity?after_id=-1', '/findings?chained=nonsense'])
def test_bad_query_is_sanitized_400(api, path):
    _, client = api
    response = client.get(PREFIX + path)
    assert response.status_code == 400
    assert response.json() == {'error': {'code': 'invalid_input', 'message': 'Invalid request.'}}


def test_a_500_logs_its_cause_without_secrets(populated, monkeypatch, caplog):
    module, client, root, sid, fid, secret = populated
    def fail():
        raise RuntimeError('summary broke near ' + secret)
    monkeypatch.setattr(module.get_service(), 'summary', fail)
    with caplog.at_level('ERROR', logger='hermes_security.dashboard'):
        assert client.get(PREFIX + '/summary').status_code == 500
    text = caplog.text
    assert 'GET' in text and '/summary' in text and 'RuntimeError: summary broke near' in text
    assert secret not in text


def test_an_oversized_response_is_logged(populated, monkeypatch, caplog):
    module, client, root, sid, fid, secret = populated
    monkeypatch.setattr(module, 'MAX_JSON_BYTES', 10)
    with caplog.at_level('ERROR', logger='hermes_security.dashboard'):
        assert client.get(PREFIX + '/summary').status_code == 500
    assert 'exceeds the 10 byte cap' in caplog.text


def test_error_mapping_and_unexposed_internal_routes(populated, monkeypatch):
    module, client, root, sid, fid, secret = populated
    for path in ['/scans/missing', '/findings/missing', '/scans/missing/activity', '/scans/missing/coverage']:
        assert client.get(PREFIX + path).status_code == 404
    assert client.post(PREFIX + '/scans/' + sid + '/cancel').status_code == 409
    assert client.post(PREFIX + '/scans/' + sid + '/resume').status_code == 409
    assert client.get(PREFIX + '/exports/' + sid + '/bad').status_code == 400
    assert client.get(PREFIX + '/scans/' + sid + '?section=bad').status_code == 400
    assert client.post(PREFIX + '/findings/' + fid + '/triage', json={'state': 'invented'}).status_code == 400
    assert client.post(PREFIX + '/findings/' + fid + '/triage', json={'state': 'open', 'extra': 1}).status_code == 400
    for body in [{}, [], {'path': str(root), 'unexpected': True}]:
        assert client.post(PREFIX + '/scans', json=body).status_code == 400
    assert client.post(PREFIX + '/scans', content='{broken', headers={'Content-Type': 'application/json'}).status_code == 400
    assert client.post(PREFIX + '/scans', content='x' * (module.MAX_JSON_BYTES + 1)).status_code == 400
    for suffix in ['mint_grant', 'record_validations', 'validations']:
        assert client.post(PREFIX + '/scans/' + sid + '/' + suffix, json={}).status_code == 404
    def fail():
        raise RuntimeError('/private/file ' + secret)
    monkeypatch.setattr(module.get_service(), 'summary', fail)
    response = client.get(PREFIX + '/summary')
    assert response.status_code == 500 and secret not in response.text and '/private' not in response.text
    for code, status in [('invalid_input', 400), ('not_found', 404), ('conflict', 409), ('sealed', 409), ('policy_denied', 403), ('target_error', 500)]:
        def fail(code=code):
            raise module.SecurityError('/private/file ' + secret, code=code)
        monkeypatch.setattr(module.get_service(), 'summary', fail)
        response = client.get(PREFIX + '/summary')
        assert response.status_code == status and secret not in response.text and '/private' not in response.text


def test_profile_isolation(api, tmp_path, monkeypatch):
    module, client = api
    first = module.get_service()
    root = tmp_path / 'repo'; root.mkdir(); (root / 'a.py').write_text('x=1\n')
    sid = client.post(PREFIX + '/scans', json={'path': str(root)}).json()['scanId']
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profiles' / 'second'))
    assert client.get(PREFIX + '/summary').json()['scans'] == 0
    assert client.get(PREFIX + '/summary').json()['profile'] == 'second'
    assert client.get(PREFIX + '/scans/' + sid).status_code == 404
    assert module.get_service() is not first
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profile'))
    assert module.get_service() is first
    assert client.get(PREFIX + '/scans/' + sid).status_code == 200


def test_triage_event_and_output_bound(populated, monkeypatch):
    import types
    module, client, root, sid, fid, secret = populated
    events = []
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_events', types.SimpleNamespace(broadcast_plugin_event=lambda *args: events.append(args)))
    assert client.post(PREFIX + '/findings/' + fid + '/triage', json={'state': 'closed'}).status_code == 200
    assert events == [('hermes-security', 'finding.updated', {'findingId': fid})]
    monkeypatch.setattr(module.get_service(), 'summary', lambda: {'tooLarge': 'a' * module.MAX_JSON_BYTES})
    assert client.get(PREFIX + '/summary').status_code == 500


def test_sql_trace_filters_precede_limit(populated, monkeypatch):
    from contextlib import contextmanager
    module, client, root, sid, fid, secret = populated
    service = module.get_service()
    queries = []
    original = service.store._connection
    @contextmanager
    def traced():
        with original() as conn:
            conn.set_trace_callback(queries.append)
            yield conn
    monkeypatch.setattr(service.store, '_connection', traced)
    response = client.get(PREFIX + '/findings', params={'validationLevel': 'static', 'limit': 1, 'offset': 1})
    assert response.json() == {'items': [], 'total': 1}
    query = next(q for q in queries if 'END, f.rowid DESC LIMIT 1 OFFSET 1' in q)
    assert "json_extract(f.data,'$.validation.level')='static'" in query.split(' ORDER BY ')[0]
    assert ',f.data,' not in query.split(' FROM findings')[0]
    assert not query.startswith('SELECT f.data')
    queries.clear()
    client.get(PREFIX + '/scans', params={'mode': 'deep', 'status': 'awaiting_analysis', 'limit': 1, 'offset': 1})
    query = next(q for q in queries if 'ORDER BY rowid DESC LIMIT 1 OFFSET 1' in q)
    assert "mode='deep'" in query and "status='awaiting_analysis'" in query


def test_isolated_import_without_repo_on_sys_path(tmp_path):
    import os
    import subprocess
    code = '''import importlib.util, sys
spec = importlib.util.spec_from_file_location('standalone_dashboard', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.SecurityService.__module__.startswith('_hermes_security_dashboard_')
assert module.get_service().summary()['scans'] == 0
print(module.health())
'''
    result = subprocess.run([sys.executable, '-I', '-c', code, str(ROOT / 'dashboard/plugin_api.py')],
                            cwd='/', env={**os.environ, 'HERMES_HOME': str(tmp_path / 'isolated')},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "'plugin': 'hermes-security'" in result.stdout


@pytest.mark.parametrize('confirm', [None, 'wrong', True])
def test_dashboard_confirmation_required(api, tmp_path, confirm):
    module, client = api
    root = tmp_path / 'repo'; root.mkdir(); (root / 'app.py').write_text('x=1\n')
    sid = module.get_service().start_scan(path=str(root), allow_tests=True)['scanId']
    for suffix in ['authorize-validation', 'run-validation', 'grants/missing/revoke']:
        response = client.post(PREFIX + '/scans/' + sid + '/' + suffix, json={'confirm': confirm})
        assert response.status_code == 400


def test_dashboard_grant_lifecycle_and_level_boundaries(api, tmp_path):
    module, client = api
    root = tmp_path / 'repo'; root.mkdir(); (root / 'app.py').write_text('x=1\n')
    ids = {}
    for level in ['static', 'local-safe', 'active-authorized']:
        body: dict = {'path': str(root), 'safetyLevel': level}
        response = client.post(PREFIX + '/scans', json=body)
        assert response.status_code == 200, response.text
        ids[level] = response.json()['scanId']
    for level, sid in ids.items():
        url = PREFIX + '/scans/' + sid
        # Any open scan can be pointed at the user's running app; defaults are fixed.
        response = client.post(url + '/authorize-validation', json={'confirm': sid, 'origin': 'http://127.0.0.1:8765'})
        assert response.status_code == 200, response.text
        grant = response.json()
        assert grant['createdBy'] == 'dashboard'
        assert grant['actions'] == ['http-probe'] and grant['maxRequests'] == 200
        assert client.get(url + '/grants').json()['items'] == [grant]
        other = next(s for l, s in ids.items() if l != level)
        wrong = PREFIX + '/scans/' + other + '/grants/' + grant['grantId'] + '/revoke'
        assert client.post(wrong, json={'confirm': other}).status_code == 400
        revoke = url + '/grants/' + grant['grantId'] + '/revoke'
        assert client.post(revoke, json={'confirm': sid}).json()['revoked'] is True
        assert client.get(url + '/grants').json()['items'][0]['revoked'] is True
        response = client.post(url + '/run-validation', json={'confirm': sid, 'plans': []})
        assert response.status_code == (403 if level == 'static' else 400)


def test_tests_allowed_scan_runs_local_controls_from_dashboard_and_agent(api, tmp_path):
    module, client = api
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('def get_invoice(invoice_id):\n    return invoices.get(invoice_id)\n')
    sid = settled(module, client.post(PREFIX + '/scans', json={'path': str(root), 'allow_tests': True}).json()['scanId'])
    service = module.get_service()
    assert service.get_scan(sid)['options']['authorizationSource'] == 'dashboard'
    spec = importlib.util.spec_from_file_location('local_helpers', ROOT / 'tests/integration/test_end_to_end_static.py')
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec); spec.loader.exec_module(helpers)
    service.submit_worker_result(helpers.worker(service.get_scan(sid)['plan'], [helpers.candidate_for(root)]))
    cid = service.get_scan(sid, 'candidates')['items'][0]['candidateId']
    plan = {'candidateId': cid, 'kind': 'local-command', 'level': 'local-safe',
            'commands': [[sys.executable, '-c', 'print("MARKER")'], [sys.executable, '-c', 'print("negative")']],
            'positiveControl': {'commandIndex': 0, 'expectedMarker': 'MARKER'},
            'negativeControl': {'commandIndex': 1, 'expectedMarker': 'MARKER'},
            'cleanup': {'disposable': True}, 'timeoutS': 2}
    tools = importlib.import_module('.tools', module._PACKAGE_NAME)
    handler = tools.make_handler('security_scan_record_validations', lambda: service)
    # Agents cannot smuggle authority fields through the tool schema.
    for extra in [{'user_authorized': True}, {'authorization_source': 'dashboard'}]:
        result = json.loads(handler({'scan_id': sid, 'plans': [plan], **extra}))
        assert result['ok'] is False
    assert service.store.validations(sid) == []
    url = PREFIX + '/scans/' + sid + '/run-validation'
    for body in [{}, {'confirm': 'wrong', 'plans': [plan]}]:
        assert client.post(url, json=body).status_code == 400
    response = client.post(url, json={'confirm': sid, 'plans': [plan]})
    assert response.status_code == 200, response.text
    receipt = response.json()['receipts'][0]
    assert receipt['status'] == 'passed'
    assert receipt['cleanup']['done'] is True
    assert 'User authorization source: dashboard' in receipt['notes']
    assert client.get(PREFIX + '/scans/' + sid, params={'section': 'validations'}).json()['items'] == [receipt]
    assert service.get_scan(sid, 'candidates')['items'][0]['evidenceState'] == 'runtime_confirmed'
    # The scan allows tests, so the agent may run its own reproductions too.
    agent = json.loads(handler({'scan_id': sid, 'plans': [plan]}))
    assert agent['ok'] is True, agent
    service.finalize(sid)
    assert client.post(url, json={'confirm': sid, 'plans': [plan]}).status_code == 409


@pytest.mark.parametrize('extra', [
    {'minutes': 0}, {'minutes': 241}, {'minutes': True}, {'maxRequests': 201},
    {'maxRequests': False}, {'actions': ['local-command']}, {'created_by': 'dashboard'},
    {'origin': 'http://localhost:8000/private'}, {'origin': 'http://user:pass@localhost:8000'},
    {'origin': None},
])
def test_dashboard_grant_invalid_scope_is_safe(api, tmp_path, extra):
    module, client = api
    root = tmp_path / 'repo'; root.mkdir(); (root / 'app.py').write_text('x=1\n')
    sid = module.get_service().start_scan(path=str(root), safety_level='active-authorized')['scanId']
    response = client.post(PREFIX + '/scans/' + sid + '/authorize-validation', json={
        'confirm': sid, 'origin': 'http://localhost:8000', **extra})
    assert response.status_code == 400
    assert '/private' not in response.text and 'user:pass' not in response.text
    assert module.get_service().list_grants(sid) == {'items': []}


def test_dashboard_start_cannot_accept_authority_fields(api, tmp_path):
    _, client = api
    root = tmp_path / 'repo'; root.mkdir(); (root / 'app.py').write_text('x=1\n')
    for field in ['user_authorized', 'authorization_source']:
        assert client.post(PREFIX + '/scans', json={'path': str(root), field: True}).status_code == 400
    for body in [{'safetyLevel': 'static', 'safety_level': 'local-safe'}, {'allow_tests': 'yes'}]:
        assert client.post(PREFIX + '/scans', json={'path': str(root), **body}).status_code == 400


def test_dashboard_http_grant_runs_bounded_agent_plan(api, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    module, client = api
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('def get_invoice(invoice_id):\n    return invoices.get(invoice_id)\n')
    service = module.get_service()
    plan = service.start_scan(path=str(root), safety_level='active-authorized')
    sid = plan['scanId']
    spec = importlib.util.spec_from_file_location('http_helpers', ROOT / 'tests/integration/test_end_to_end_static.py')
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec); spec.loader.exec_module(helpers)
    service.submit_worker_result(helpers.worker(plan, [helpers.candidate_for(root)]))
    cid = service.get_scan(sid, 'candidates')['items'][0]['candidateId']
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200); self.end_headers()
            self.wfile.write(b'MARKER' if self.path == '/positive' else b'negative')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        origin = 'http://127.0.0.1:' + str(server.server_port)
        url = PREFIX + '/scans/' + sid
        grant = client.post(url + '/authorize-validation', json={
            'confirm': sid, 'origin': origin, 'maxRequests': 2, 'minutes': 1}).json()
        http_plan = {'candidateId': cid, 'grantId': grant['grantId'], 'level': 'active-authorized', 'kind': 'http-probe',
            'requests': [{'method': 'GET', 'url': origin + '/positive'}, {'method': 'GET', 'url': origin + '/negative'}],
            'positiveControl': {'requestIndex': 0, 'expectedMarker': 'MARKER'}, 'negativeControl': {'requestIndex': 1},
            'cleanup': {'responses': 'close'}, 'timeoutS': 2}
        tools = importlib.import_module('.tools', module._PACKAGE_NAME)
        handler = tools.make_handler('security_scan_record_validations', lambda: service)
        result = json.loads(handler({'scan_id': sid, 'plans': [http_plan]}))
        assert result['ok'] is True and result['result']['receipts'][0]['status'] == 'passed'
        assert requests == ['/positive', '/negative']
        assert client.get(url + '/grants').json()['items'][0]['used'] == 2
        assert json.loads(handler({'scan_id': sid, 'plans': [http_plan]}))['ok'] is False
        assert client.post(url + '/grants/' + grant['grantId'] + '/revoke', json={'confirm': sid}).status_code == 200
        assert json.loads(handler({'scan_id': sid, 'plans': [http_plan]}))['ok'] is False
        assert len(requests) == 2
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_manifest_contract():
    manifest = json.loads((ROOT / 'dashboard/manifest.json').read_text(encoding='utf-8'))
    assert manifest['name'] == 'hermes-security' and manifest['api'] == 'plugin_api.py'


def test_new_scan_folders_progress_and_chat_link(api, tmp_path):
    module, client = api
    root = tmp_path / 'app'
    (root / 'src').mkdir(parents=True)
    (root / 'node_modules').mkdir()
    (root / '.git').mkdir()
    (root / 'src' / 'main.py').write_text('print(1)\n')
    assert client.get(PREFIX + '/folders', params={'path': str(root)}).json()['items'] == ['src']
    assert client.get(PREFIX + '/folders', params={'path': str(root / 'missing')}).status_code >= 400
    scan = client.post(PREFIX + '/scans', json={'path': str(root)}).json()
    progress = scan['progress']
    assert [s['label'] for s in progress['steps']][:2] == ['Preparing scan', 'Building threat model']
    assert progress['steps'][0]['state'] == 'active'
    assert all(s['state'] == 'pending' for s in progress['steps'][1:])
    linked = client.post(PREFIX + f"/scans/{scan['scanId']}/session", json={'sessionId': 'rt_1', 'storedSessionId': 'st_1'}).json()
    assert linked['options']['session'] == {'sessionId': 'rt_1', 'storedSessionId': 'st_1'}
    assert client.post(PREFIX + f"/scans/{scan['scanId']}/session", json={'bogus': 1}).status_code >= 400


def test_summary_counts_open_findings_by_severity(populated):
    module, client, root, sid, fid, secret = populated
    finding = client.get(PREFIX + '/findings/' + fid).json()
    finding = finding.get('finding', finding)
    level = finding['severity']['level'] if isinstance(finding.get('severity'), dict) else finding['severity']
    before = client.get(PREFIX + '/summary').json()['open'].get(level, 0)
    assert before >= 1
    assert client.post(PREFIX + '/findings/' + fid + '/triage', json={'state': 'closed', 'note': ''}).status_code == 200
    assert client.get(PREFIX + '/summary').json()['open'].get(level, 0) == before - 1


def test_delete_scan_requires_consent_and_a_stopped_scan(populated):
    module, client, root, sid, fid, secret = populated
    service = module.get_service()
    assert client.post(PREFIX + '/findings/' + fid + '/triage', json={'state': 'closed', 'note': 'done'}).status_code == 200
    open_sid = client.post(PREFIX + '/scans', json={'path': str(root)}).json()['scanId']
    assert client.post(PREFIX + f'/scans/{open_sid}/delete', json={}).status_code == 400
    assert client.post(PREFIX + f'/scans/{open_sid}/delete', json={'confirm': sid}).status_code == 400
    assert client.post(PREFIX + f'/scans/{open_sid}/delete', json={'confirm': open_sid}).status_code == 409
    assert client.post(PREFIX + f'/scans/{open_sid}/cancel').status_code == 200
    assert client.post(PREFIX + f'/scans/{open_sid}/delete', json={'confirm': open_sid}).json()['deleted'] is True
    assert client.get(PREFIX + '/scans/' + open_sid).status_code == 404
    directory = module.importlib.import_module('.store.artifacts', module._PACKAGE_NAME).scan_dir(service.data_dir, sid)
    assert directory.exists()
    result = client.post(PREFIX + f'/scans/{sid}/delete', json={'confirm': sid}).json()
    assert result['deleted'] is True and result['findings'] >= 1 and 'filesLeft' not in result
    assert not directory.exists()
    assert client.get(PREFIX + '/findings').json()['total'] == 0
    assert client.get(PREFIX + '/summary').json()['scans'] == 0
    assert client.get(PREFIX + '/repositories').json()['total'] == 0
    with service.store._connection() as conn:
        assert conn.execute('SELECT count(*) FROM triage').fetchone()[0] == 0
        for table in ('events', 'inventory_files', 'worker_attempts', 'candidates', 'seals', 'snapshots'):
            assert conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0, table


def test_delete_keeps_triage_a_remaining_scan_still_reports(populated, tmp_path):
    module, client, root, sid, fid, secret = populated
    service = module.get_service()
    spec = importlib.util.spec_from_file_location('security_acceptance_helpers', ROOT / 'tests/integration/test_end_to_end_static.py')
    helpers = importlib.util.module_from_spec(spec); spec.loader.exec_module(helpers)
    plan = service.start_scan(path=str(root))
    candidate = helpers.candidate_for(root); candidate['evidenceState'] = 'source_supported'
    service.submit_worker_result(helpers.worker(plan, [candidate], attempt='att_second'))
    service.finalize(plan['scanId'])
    assert client.post(PREFIX + '/findings/' + fid + '/triage', json={'state': 'accepted_risk', 'note': ''}).status_code == 200
    assert client.post(PREFIX + f'/scans/{sid}/delete', json={'confirm': sid}).status_code == 200
    assert client.get(PREFIX + '/findings/' + fid).json()['triage']['state'] == 'accepted_risk'


def test_repository_and_folder_filters(populated, tmp_path):
    module, client, root, sid, fid, secret = populated
    other = tmp_path / 'other'; (other / 'lib').mkdir(parents=True); (other / 'lib' / 'x.py').write_text('x = 1\n')
    client.post(PREFIX + '/scans', json={'path': str(other)})
    repos = {r['root']: r['repo_key'] for r in client.get(PREFIX + '/repositories').json()['items']}
    key = repos[str(root.resolve())]
    rows = client.get(PREFIX + '/scans', params={'repo': key}).json()
    assert rows['total'] == 1 and rows['items'][0]['scan_id'] == sid
    assert client.get(PREFIX + '/scans', params={'repo': repos[str(other.resolve())]}).json()['total'] == 1
    service = module.get_service()
    with service.store._connection() as conn:
        conn.execute("UPDATE findings SET path='src/api/app.py'")
    folders = client.get(PREFIX + '/finding-folders', params={'repo': key}).json()['items']
    assert [(f['folder'], f['count']) for f in folders] == [('src', 1), ('src/api', 1)]
    assert client.get(PREFIX + '/finding-folders', params={'repo': 'nope'}).json()['items'] == []
    for folder, total in [('src', 1), ('src/api', 1), ('src/', 1), ('sr', 0), ('src/a', 0), ('lib', 0)]:
        assert client.get(PREFIX + '/findings', params={'folder': folder}).json()['total'] == total, folder


def test_clone_rejects_unsafe_addresses_with_a_readable_message(api, tmp_path):
    module, client = api
    clone = module.importlib.import_module('.target.clone', module._PACKAGE_NAME)
    assert clone.parse_url('owner/repo') == ('https://github.com/owner/repo', 'github.com', ['owner', 'repo'])
    assert clone.parse_url('git@gitlab.com:team/app.git')[1:] == ('gitlab.com', ['team', 'app'])
    assert clone.parse_url('https://example.com/a/b/c.git')[2] == ['a', 'b', 'c']
    # One repository however it is written, so asking again updates the earlier clone.
    assert clone._identity('owner/repo') == clone._identity('https://github.com/owner/repo.git') == clone._identity('git@github.com:Owner/Repo.git')
    assert clone._identity('owner/repo') != clone._identity('https://gitlab.com/owner/repo')
    for bad in ['file:///etc', 'C:/repo', '/tmp/repo', '--upload-pack=x', 'ext::sh -c id', 'https://user:pw@github.com/o/r',
                'https://github.com/', 'https://github.com/o/..', 'ftp://example.com/o/r', '']:
        with pytest.raises(clone.CloneError):
            clone.parse_url(bad)
    response = client.post(PREFIX + '/clone', json={'url': 'file:///etc/passwd'})
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'clone_failed' and 'https://' in response.json()['error']['message']
    assert client.post(PREFIX + '/clone', json={'url': 'o/r', 'extra': 1}).status_code == 400
    assert client.get(PREFIX + '/clones').json() == {'items': []}
    service = module.get_service()
    (service.data_dir / 'repos' / 'github.com' / 'o' / 'r' / '.git').mkdir(parents=True)
    (service.data_dir / 'repos' / 'github.com' / 'o' / 'r.partial-1234' / '.git').mkdir(parents=True)
    assert client.get(PREFIX + '/clones').json()['items'] == [{'path': str(service.data_dir / 'repos' / 'github.com' / 'o' / 'r'), 'name': 'o/r'}]


def next_after_prepare(client, scan_id, timeout=10):
    """The next step once the background inventory is done: a Workbench start answers first."""
    deadline = time.monotonic() + timeout
    while True:
        step = client.get(PREFIX + '/scans/' + scan_id, params={'section': 'next'}).json()
        if step['step'] != 'wait' or time.monotonic() > deadline:
            return step
        time.sleep(0.05)


def test_scan_subagent_route_is_checked_and_reaches_the_agent(api, tmp_path, monkeypatch):
    module, client = api
    routes = {'installed': True, 'backend': 'model_map', 'available': True, 'default': 'fast',
              'choices': [{'name': 'fast', 'description': 'Cheap', 'detail': 'openrouter/x'}]}
    monkeypatch.setattr(module, '_delegation_info', lambda: routes)
    assert client.get(PREFIX + '/delegation').json() == routes
    root = tmp_path / 'repo'; root.mkdir(); (root / 'a.py').write_text('x = 1\n')
    for bad in [{'backend': 'model_map', 'name': 'deep'}, {'backend': 'profile_routes', 'name': 'fast'}, 'fast']:
        assert client.post(PREFIX + '/scans', json={'path': str(root), 'delegate': bad}).status_code == 400, bad
    scan = client.post(PREFIX + '/scans', json={'path': str(root), 'delegate': {'backend': 'model_map', 'name': 'fast'}}).json()
    assert scan['options']['delegate'] == {'backend': 'model_map', 'name': 'fast'}
    step = next_after_prepare(client, scan['scanId'])
    assert step['step'] == 'dispatch' and step['delegate']['name'] == 'fast'
    assert 'Set "option": "fast" on every delegate_task task' in step['how']
    summary = client.get(PREFIX + '/scans/' + scan['scanId'], params={'section': 'summary'}).json()
    assert summary['delegate'] == {'backend': 'model_map', 'name': 'fast'}
    monkeypatch.setattr(module, '_delegation_info', lambda: {**routes, 'installed': False})
    assert client.post(PREFIX + '/scans', json={'path': str(root), 'delegate': {'backend': 'model_map', 'name': 'fast'}}).status_code == 400
    plain = client.post(PREFIX + '/scans', json={'path': str(root)}).json()
    assert 'delegate' not in client.get(PREFIX + '/scans/' + plain['scanId'], params={'section': 'next'}).json()


def _fake_host(monkeypatch, config, registered=()):
    import types
    backends = types.ModuleType('agent.delegation_backends')
    backends.list_backends = lambda: [types.SimpleNamespace(name=n, available=a) for n, a in registered]
    cfg = types.ModuleType('hermes_cli.config')
    cfg.load_config_readonly = lambda: config
    monkeypatch.setitem(sys.modules, 'agent', types.ModuleType('agent'))
    monkeypatch.setitem(sys.modules, 'agent.delegation_backends', backends)
    monkeypatch.setitem(sys.modules, 'hermes_cli', types.ModuleType('hermes_cli'))
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', cfg)


def test_delegation_routes_come_from_config_when_the_dashboard_loaded_no_plugins(api, monkeypatch):
    module, _ = api
    config = {'plugins': {'enabled': ['advanced-delegation']},
              'delegation': {'backend': 'model_map', 'backends': {'model_map': {
                  'default': 'fast', 'options': [{'name': 'fast', 'provider': 'p', 'model': 'm', 'api_key': 'sk-secret'}]}}}}
    _fake_host(monkeypatch, config)
    info = module._delegation_info()
    assert info == {'installed': True, 'backend': 'model_map', 'available': True, 'default': 'fast',
                    'choices': [{'name': 'fast', 'description': '', 'detail': 'p/m'}]}
    assert 'sk-secret' not in json.dumps(info)


def test_delegation_registry_wins_and_disabled_plugin_offers_nothing(api, monkeypatch):
    module, _ = api
    config = {'plugins': {'enabled': ['advanced-delegation'], 'disabled': ['advanced-delegation']},
              'delegation': {'backend': 'model_map', 'backends': {'model_map': {'options': [{'name': 'fast'}]}}}}
    _fake_host(monkeypatch, config)
    assert module._delegation_info()['installed'] is False
    assert module._delegation_info()['available'] is False
    _fake_host(monkeypatch, config, registered=[('default', True), ('model_map', False)])
    info = module._delegation_info()
    assert info['installed'] is True and info['available'] is False
