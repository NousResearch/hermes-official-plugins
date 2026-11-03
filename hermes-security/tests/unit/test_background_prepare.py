"""Start scan answers before the inventory: large repositories take minutes to read."""
import threading

from hermes_security import target
from hermes_security.errors import TargetError
from hermes_security.orchestration import standard
from hermes_security.service import SecurityService


def repo(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('def handler(request):\n    return request.args.get("q")\n')
    return str(root)


def join(service, sid):
    thread = service._dependency_threads.get(sid)
    if thread:
        thread.join(10)


def test_start_answers_before_the_inventory_and_review_waits_for_it(tmp_path, monkeypatch):
    release = threading.Event()
    real = target.build_inventory

    def slow(*args, **kwargs):
        release.wait(10)
        return real(*args, **kwargs)
    monkeypatch.setattr(target, 'build_inventory', slow)
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=repo(tmp_path), background=True)
    sid = plan['scanId']
    assert plan['status'] == 'created' and plan['inventory']['files'] == 0
    step = service.next_step(sid)
    assert step['step'] == 'wait' and 'inventor' in step['reason'].lower()
    assert service.get_scan(sid)['progress']['phase'] == 'preparing'
    release.set()
    join(service, sid)
    scan = service._scan(sid)
    assert scan['status'] == 'awaiting_analysis' and scan['snapshot_digest'].startswith('sha256:')
    assert scan['options']['planner'] == standard.PLANNER_VERSION and not scan['options']['preparing']
    assert [f['path'] for f in service.store.inventory(sid)] == ['app.py']
    assert service.next_step(sid)['step'] == 'dispatch'
    events = [e['kind'] for e in service.activity(sid)['items']]
    assert events.index('scan.preparing') < events.index('scan.inventoried') < events.index('scan.started')


def test_an_inventory_lost_to_a_gateway_restart_runs_again_on_the_next_read(tmp_path, monkeypatch):
    first = SecurityService(tmp_path / 'data')
    monkeypatch.setattr(first, '_start_dependency_check', lambda scan_id: None)  # died before it ran
    sid = first.start_scan(path=repo(tmp_path), background=True)['scanId']
    restarted = SecurityService(tmp_path / 'data')
    assert restarted.next_step(sid)['step'] == 'wait'
    join(restarted, sid)
    assert restarted.next_step(sid)['step'] == 'dispatch'
    assert len(restarted.store.inventory(sid)) == 1


def test_an_unreadable_target_fails_the_scan_instead_of_waiting_forever(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise TargetError('Unable to enumerate target directory')
    monkeypatch.setattr(target, 'build_inventory', broken)
    service = SecurityService(tmp_path / 'data')
    sid = service.start_scan(path=repo(tmp_path), background=True)['scanId']
    join(service, sid)
    scan = service._scan(sid)
    assert scan['status'] == 'failed' and 'enumerate' in scan['reason']
    assert not scan['options']['preparing'] and not scan['options']['dependenciesPending']
    assert 'scan.failed' in [e['kind'] for e in service.activity(sid)['items']]


def test_without_background_the_tool_path_stays_synchronous(tmp_path):
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=repo(tmp_path))
    assert plan['status'] == 'awaiting_analysis' and plan['inventory']['files'] == 1
    assert 'preparing' not in service._scan(plan['scanId'])['options']
