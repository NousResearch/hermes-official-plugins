"""Planner 4 (the default): planner 3's areas plus cross-file question packets from an import map."""
import time

import pytest

from hermes_security import tools
from hermes_security.errors import ValidationError
from hermes_security.service import SecurityService
from test_end_to_end_static import worker

REPO = {
    'app/__init__.py': '', 'app/web/__init__.py': '', 'app/core/__init__.py': '', 'app/runner/__init__.py': '',
    'app/authz/__init__.py': '',
    'app/web/routes.py': ('from app.core import jobs\nfrom app.authz import guard\n\n\n@app.post("/run")\ndef run():\n'
                          '    guard.require_user(request.json)\n    return jobs.start(request.json["cmd"])\n'),
    'app/web/admin.py': ('from app.authz import guard\n\n\n@app.get("/admin")\ndef admin():\n'
                         '    return guard.require_admin(request.args)\n'),
    'app/core/jobs.py': 'from app.runner import shell\n\n\ndef start(cmd):\n    return shell.run(cmd)\n',
    'app/runner/shell.py': 'import subprocess\n\n\ndef run(cmd):\n    return subprocess.run(cmd, shell=True)\n',
    'app/authz/guard.py': ('def require_user(body):\n    if not authorization_ok(body):\n        raise PermissionError()\n\n\n'
                           'def require_admin(args):\n    return is_admin(args)\n'),
    'tests/test_routes.py': 'def test_run():\n    pass\n',
}


def start(tmp_path, **opts):
    root = tmp_path / 'repo'
    for path, text in REPO.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    service = SecurityService(tmp_path / 'data')
    return service, service.start_scan(path=str(root), **opts)


def test_planner_4_adds_cross_file_questions_to_the_planner_3_areas(tmp_path):
    service, plan = start(tmp_path)
    options = service.get_scan(plan['scanId'])['options']
    assert options['planner'] == 4 and options['flowMap']['stats']['edges'] > 0
    packets = plan['packets']
    assert {'baseline', 'area', 'flow', 'control'} <= {p['review'] for p in packets}
    flow = next(p for p in packets if p['review'] == 'flow' and p['sink'] == 'app/runner/shell.py')
    assert flow['files'][:3] == ['app/web/routes.py', 'app/core/jobs.py', 'app/runner/shell.py']
    assert 'app/authz/guard.py' in flow['files'] and 'app/web/routes.py' in flow['brief']
    control = next(p for p in packets if p['review'] == 'control')
    assert control['files'][0] == 'app/authz/guard.py'
    assert set(control['files'][1:]) == {'app/web/admin.py', 'app/web/routes.py'}


def test_planner_3_stays_available_for_folder_only_plans(tmp_path):
    service, plan = start(tmp_path, planner=3)
    options = service.get_scan(plan['scanId'])['options']
    assert options['planner'] == 3 and 'flowMap' not in options
    assert {p['review'] for p in plan['packets']} <= {'baseline', 'area', 'second'}
    for bad in (5, '4', True):
        with pytest.raises(ValidationError):
            service.start_scan(path=str(tmp_path / 'repo'), planner=bad)


def test_a_question_packet_never_queues_a_remainder(tmp_path):
    service, plan = start(tmp_path)
    flow = next(p for p in plan['packets'] if p['review'] == 'flow')
    payload = worker(plan, [], attempt='att_flow', role='investigator')
    payload.update(packetId=flow['packetId'],
                   coverage=[{'unit': 'file:' + flow['files'][0], 'state': 'deferred', 'note': 'answered one question'}])
    assert service.submit_worker_result(payload)['attemptId'] == 'att_flow'
    reviews = [p['review'] for p in service.get_scan(plan['scanId'], 'packets', limit=200)['items']]
    assert 'remainder' not in reviews


@pytest.mark.parametrize('chosen', [{}, {'planner': 4}])
def test_a_workbench_scan_keeps_planner_4_after_its_background_inventory(tmp_path, chosen):
    service, started = start(tmp_path, background=True, **chosen)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        options = service.get_scan(started['scanId'])['options']
        if not options.get('preparing') and not options.get('dependenciesPending'):
            break
        time.sleep(0.05)
    assert options['planner'] == 4 and options['flowMap']['controls']


def test_the_start_tool_defaults_to_planner_4():
    field = tools._SCHEMAS['security_scan_start']['parameters']['properties']['planner']
    assert field['enum'] == [3, 4] and field['default'] == 4
