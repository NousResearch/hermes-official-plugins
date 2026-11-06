from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
import pytest
from hermes_security.service import SecurityService
from hermes_security.errors import Conflict, PolicyDenied, ValidationError
from hermes_security.config import resolve_data_dir
from hermes_security.standards import ledger
from test_service_modes import setup
from test_end_to_end_static import worker, candidate_for


def test_complete_requires_every_worker_file_and_standard(tmp_path):
    root, service, plan = setup(tmp_path)
    sid = plan['scanId']
    units = [{'unit': 'file:app.py', 'state': 'reviewed', 'note': 'File reviewed.'}]
    units += [{'unit': 'lane:' + r['id'], 'state': 'reviewed', 'note': 'Lane reviewed.'} for r in ledger.load_top10()['categories']]
    units += [{'unit': 'asvs:' + r['id'], 'state': 'reviewed', 'note': 'Control reviewed.'} for r in ledger.applicable_controls(plan['surfaces'])]
    service.checkpoint(sid, coverage=units)
    assert service.coverage(sid)['completeness'] == 'partial'
    for i, packet in enumerate(plan['packets']):
        payload = worker(plan, [], role=packet['role'], attempt='att_packet_' + str(i)); payload['packetId'] = packet['packetId']
        service.submit_worker_result(payload)
    assert service.coverage(sid)['completeness'] == 'complete'
    assert service.finalize(sid)['status'] == 'completed'


def test_real_http_grant_budget_and_concurrent_owner_isolation(tmp_path):
    root, service, plan = setup(tmp_path, safety_level='active-authorized')
    sid = plan['scanId']
    service.submit_worker_result(worker(plan, [candidate_for(root)]))
    cid = service.get_scan(sid, 'candidates')['items'][0]['candidateId']
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'MARKER' if self.path == '/positive' else b'negative'
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        origin = 'http://127.0.0.1:' + str(server.server_port)
        grant = service.mint_grant(sid, origins=[origin], actions=['http-probe'], expires_in_s=60, max_requests=2)
        other = SecurityService(tmp_path / 'data')
        with service._mutation(sid, owner='grant:' + grant['grantId']):
            with pytest.raises(Conflict):
                with other._mutation(sid, owner='grant:' + grant['grantId']):
                    pytest.fail('same owner prefix must not bypass the lease')
        request = {'candidateId': cid, 'grantId': grant['grantId'], 'level': 'active-authorized', 'kind': 'http-probe',
                   'requests': [{'method': 'GET', 'url': origin + '/positive'}, {'method': 'GET', 'url': origin + '/negative'}],
                   'positiveControl': {'requestIndex': 0, 'expectedMarker': 'MARKER'}, 'negativeControl': {'requestIndex': 1},
                   'cleanup': {'responses': 'close'}, 'timeoutS': 2}
        result = service.record_validations(sid, plans=[request])
        assert result['receipts'][0]['status'] == 'passed'
        assert service.store.get_grant(grant['grantId'])['used'] == 2
        with pytest.raises(PolicyDenied): service.record_validations(sid, plans=[request])
        assert service.store.get_grant(grant['grantId'])['used'] == 2
        spent = service.store.get_grant(grant['grantId'])
        for altered in ({**spent, 'used': 1}, {**spent, 'used': 3}, {**spent, 'maxRequests': 4}, {**spent, 'origins': ['https://other.invalid:443']}):
            with pytest.raises(Conflict): service.store.grant_validation(sid, altered)
        service.revoke_grant(grant['grantId'])
        with pytest.raises(PolicyDenied): service.record_validations(sid, plans=[request])
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_config_context_and_importable_hermes_state(tmp_path, monkeypatch):
    import os
    import sys
    import types
    assert resolve_data_dir(SimpleNamespace(state=SimpleNamespace(data_dir=tmp_path / 'context'))) == tmp_path / 'context'
    module = types.ModuleType('hermes_cli.plugins_state')
    class PluginState:
        def __init__(self, plugin_id):
            assert plugin_id == 'hermes-security'
        @property
        def data_dir(self):
            from pathlib import Path
            return Path(os.environ['HERMES_HOME']) / 'plugin-data/hermes-security'
    module.PluginState = PluginState
    monkeypatch.setitem(sys.modules, 'hermes_cli', types.ModuleType('hermes_cli'))
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugins_state', module)
    for label in ('A', 'B'):
        monkeypatch.setenv('HERMES_HOME', str(tmp_path / label))
        assert resolve_data_dir() == tmp_path / label / 'plugin-data/hermes-security'


def test_standalone_data_dir_matches_hermes_plugin_state(tmp_path, monkeypatch):
    # The standalone CLI must write where agent tools (ctx.state) and the dashboard read.
    import os
    import sys
    from pathlib import Path
    from hermes_security.config import data_namespace, resolve_data_dir
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugins_state', None)
    fallback = resolve_data_dir()
    assert fallback == tmp_path / 'plugin-data' / data_namespace()
    agent = Path(os.environ.get('HERMES_AGENT_SRC', Path.home() / '.hermes' / 'hermes-agent'))
    if not (agent / 'hermes_cli' / 'plugins_manifest.py').is_file():
        pytest.skip('Hermes source not available')
    monkeypatch.syspath_prepend(str(agent))
    try:
        from hermes_cli.plugins_manifest import _portable_skill_namespace
    except ImportError:
        pytest.skip('Hermes manifest module not importable here')
    assert data_namespace() == _portable_skill_namespace('hermes-security')


def test_resume_reissues_canceled_deep_attempt(tmp_path):
    root, service, plan = setup(tmp_path, mode='deep')
    packet = plan['packets'][0]
    sid = plan['scanId']
    service.store.record_attempt(sid, packet['attemptId'], 'deep-pass', packet_id=packet['packetId'])
    service.cancel(sid)
    resumed = service.resume(sid)
    replacement = resumed['packets'][0]
    assert replacement['attemptId'] != packet['attemptId']
    payload = worker(plan, [], role='deep-pass', attempt=replacement['attemptId']); payload['packetId'] = packet['packetId']
    assert service.submit_worker_result(payload)['inserted'] == 0
