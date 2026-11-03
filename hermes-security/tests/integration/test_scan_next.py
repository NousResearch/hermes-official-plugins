"""The `next` section walks a real scan from dispatch to done using only stored state."""
import json

from hermes_security import tools
from hermes_security.service import SecurityService
from test_end_to_end_static import candidate_for, worker


def start(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('def get_invoice(invoice_id):\n    return invoices.get(invoice_id)\n')
    service = SecurityService(tmp_path / 'data')
    return root, service, service.start_scan(path=str(root))


def submit_all_packets(service, plan, root, candidates=True):
    for packet in plan['packets']:
        payload = worker(plan, [candidate_for(root)] if candidates and packet['role'] == 'baseline' else [],
                         role=packet['role'], attempt='att_' + packet['packetId'][4:16],
                         coverage=[{'unit': 'file:app.py', 'state': 'reviewed', 'note': 'Read fully.'}])
        payload['packetId'] = packet['packetId']
        service.submit_worker_result(payload)


def test_next_walks_dispatch_validate_chains_finalize_done(tmp_path):
    root, service, plan = start(tmp_path)
    sid = plan['scanId']
    first = service.get_scan(sid, 'next')
    assert first['step'] == 'dispatch' and first['total'] == len(plan['packets'])
    assert service.get_scan(sid, 'summary')['next']['step'] == 'dispatch'
    assert 'next' in service.get_scan(sid, 'summary')['sections']
    submit_all_packets(service, plan, root)
    after = service.next_step(sid)
    assert after['step'] == 'validate' and after['total'] == 1
    assert after['progressKey'] > first['progressKey']
    candidate = candidate_for(root); candidate['evidenceState'] = 'source_supported'
    service.submit_worker_result(worker(plan, [candidate], attempt=after['attemptId'], role='validator'))
    assert service.next_step(sid)['step'] == 'chains'
    service.record_chains(sid)
    assert service.next_step(sid)['step'] == 'finalize'
    service.finalize(sid)
    assert service.next_step(sid)['step'] == 'done'


def test_canceled_scan_is_blocked_and_resume_reopens_it(tmp_path):
    _, service, plan = start(tmp_path)
    sid = plan['scanId']
    service.cancel(sid)
    assert service.next_step(sid)['step'] == 'blocked'
    service.resume(sid)
    assert service.next_step(sid)['step'] == 'dispatch'


def test_next_is_reachable_through_the_get_tool_under_the_cap(tmp_path):
    _, service, plan = start(tmp_path)
    handler = tools.make_handler('security_scan_get', lambda: service)
    raw = handler({'scan_id': plan['scanId'], 'section': 'next'})
    assert len(raw) < tools.MAX_RESULT_CHARS
    body = json.loads(raw)
    assert body['ok'] and body['result']['step'] == 'dispatch'
    assert 'next' in tools._SCHEMAS['security_scan_get']['parameters']['properties']['section']['description']
