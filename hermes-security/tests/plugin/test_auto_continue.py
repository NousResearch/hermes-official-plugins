"""Hook-driven automatic continue and worker tracking, with a fake host context."""
import os
import sys
import types

import pytest

from hermes_security import recovery, tools
from hermes_security.service import SecurityService

SESSION = 'local-abc'


def worker(plan, candidates, attempt, role):
    return dict(scanId=plan['scanId'], attemptId=attempt, workerRole=role, snapshotDigest=plan['snapshotDigest'],
                methodologyVersion=plan['methodologyVersion'], packetId=None, candidates=candidates,
                coverage=[], negativeResults=[], notes='')


class FakeCtx:
    def __init__(self, result=True):
        self.result = result
        self.sent = []

    def inject_message(self, content, role='user', *, session_key=None, origin=None):
        self.sent.append((content, session_key))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def scan(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('def f():\n    return 1\n')
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=str(root))
    service.link_session(plan['scanId'], SESSION)
    return service, plan


def hooks(service, ctx):
    return recovery.make_hooks(ctx, lambda: service)


def end(h, session=SESSION, **flags):
    h['on_session_end'](session_id=session, task_id='t', turn_id='u', completed=flags.get('completed', True),
                        failed=False, interrupted=flags.get('interrupted', False), turn_exit_reason='x',
                        model='m', platform='local')


def touch(service, plan):
    """The injected turn reads `next` through the tool, which clears the queued continue."""
    tools.make_handler('security_scan_get', lambda: service)({'scan_id': plan['scanId'], 'section': 'next'})


def progress(service, plan, n):
    service.checkpoint(plan['scanId'], coverage=[], note=f'progress {n}')


def guard(service, plan):
    return service.store.get_scan(plan['scanId'])['options'].get('autoResume') or {}


def test_completed_turn_queues_one_plain_continue(scan):
    service, plan = scan
    ctx = FakeCtx()
    end(hooks(service, ctx))
    assert len(ctx.sent) == 1
    text, key = ctx.sent[0]
    assert text.startswith(f'Continue security scan {plan["scanId"]}: next step is dispatch (')
    assert 'security_scan_get section next' in text and key is None
    kinds = [e['kind'] for e in service.store.events(plan['scanId'])]
    assert 'autoresume.scheduled' in kinds


def test_inherited_environment_session_key_is_never_used(scan, monkeypatch):
    service, _ = scan
    monkeypatch.setenv('HERMES_SESSION_KEY', 'agent:main:telegram:dm:someone-else')
    module = types.ModuleType('gateway.session_context')
    module.get_session_env = lambda name, default='': os.environ.get(name, default)
    monkeypatch.setitem(sys.modules, 'gateway', types.ModuleType('gateway'))
    monkeypatch.setitem(sys.modules, 'gateway.session_context', module)
    ctx = FakeCtx()
    end(hooks(service, ctx))
    assert ctx.sent[0][1] is None


def test_session_key_is_used_when_available(scan, monkeypatch):
    service, _ = scan
    monkeypatch.delenv('HERMES_SESSION_KEY', raising=False)
    module = types.ModuleType('gateway.session_context')
    module.get_session_env = lambda name, default='': 'agent:main:local:dm:local-abc' if name == 'HERMES_SESSION_KEY' else default
    monkeypatch.setitem(sys.modules, 'gateway', types.ModuleType('gateway'))
    monkeypatch.setitem(sys.modules, 'gateway.session_context', module)
    ctx = FakeCtx()
    end(hooks(service, ctx))
    assert ctx.sent[0][1] == 'agent:main:local:dm:local-abc'


def test_max_iterations_turn_also_continues(scan):
    service, _ = scan
    ctx = FakeCtx()
    end(hooks(service, ctx), completed=False)   # max iterations: every flag false
    assert len(ctx.sent) == 1


def test_interrupted_turn_never_continues_until_progress(scan):
    service, plan = scan
    ctx = FakeCtx()
    h = hooks(service, ctx)
    end(h, interrupted=True)
    end(h)   # an unrelated later turn in the same chat
    assert ctx.sent == []
    progress(service, plan, 1)   # the user asked to continue and work resumed
    end(h)
    assert len(ctx.sent) == 1


def test_only_one_queued_continue_at_a_time(scan):
    service, plan = scan
    ctx = FakeCtx()
    h = hooks(service, ctx)
    end(h)
    end(h)          # a turn that was already queued ahead of the continue
    assert len(ctx.sent) == 1
    touch(service, plan)
    end(h)
    assert len(ctx.sent) == 2


def test_unused_queued_continue_expires_after_one_more_turn(scan):
    service, _ = scan
    ctx = FakeCtx()
    h = hooks(service, ctx)
    end(h); end(h); end(h)
    assert len(ctx.sent) == 2


def test_cap_without_progress_pauses_and_progress_resets_it(scan):
    service, plan = scan
    ctx = FakeCtx()
    h = hooks(service, ctx)
    for _ in range(recovery.CAP + 2):
        end(h)
        touch(service, plan)
    assert len(ctx.sent) == recovery.CAP
    row = service.store.get_scan(plan['scanId'])
    assert row['reason'] == recovery.PAUSED_STUCK
    paused = [e for e in service.store.events(plan['scanId']) if e['kind'] == 'autoresume.paused']
    assert len(paused) == 1
    progress(service, plan, 1)
    end(h)
    assert len(ctx.sent) == recovery.CAP + 1
    assert service.store.get_scan(plan['scanId'])['reason'] is None
    assert guard(service, plan)['count'] == 1


def test_injection_refused_records_pause_once_and_does_not_count(scan):
    service, plan = scan
    ctx = FakeCtx(result=False)
    h = hooks(service, ctx)
    end(h); end(h); end(h)
    assert len(ctx.sent) == 3
    assert guard(service, plan)['count'] == 0 and not guard(service, plan).get('pending')
    paused = [e for e in service.store.events(plan['scanId']) if e['kind'] == 'autoresume.paused']
    assert len(paused) == 1
    assert service.store.get_scan(plan['scanId'])['reason'] == recovery.PAUSED_OFF
    ctx.result = True   # switch turned on
    end(h)
    assert service.store.get_scan(plan['scanId'])['reason'] is None and guard(service, plan)['count'] == 1


def test_hook_errors_never_reach_the_host(scan):
    service, _ = scan
    ctx = FakeCtx(result=RuntimeError('host down'))
    end(hooks(service, ctx))   # inject raises: handled
    broken = recovery.make_hooks(ctx, lambda: (_ for _ in ()).throw(RuntimeError('no service')))
    end(broken)
    broken['subagent_start'](parent_session_id=SESSION, child_session_id='c', child_goal='attemptId: att_x')
    broken['subagent_stop'](parent_session_id=SESSION, child_session_id='c', child_status='failed')
    hooks(service, ctx)['on_session_end']()   # missing kwargs


def test_other_sessions_and_finished_scans_are_ignored(scan):
    service, plan = scan
    ctx = FakeCtx()
    h = hooks(service, ctx)
    end(h, session='local-other')
    assert ctx.sent == []
    service.cancel(plan['scanId'])
    end(h)
    assert ctx.sent == []


def test_waiting_for_workers_does_not_continue(scan):
    service, plan = scan
    ctx = FakeCtx()
    h = hooks(service, ctx)
    for packet in plan['packets']:
        h['subagent_start'](parent_session_id=SESSION, child_session_id='child-' + packet['packetId'],
                            child_goal=f'scanId: {plan["scanId"]}\nattemptId: att_{packet["packetId"][4:12]}\npacketId: {packet["packetId"]}')
    assert service.next_step(plan['scanId'])['step'] == 'wait'
    end(h)
    assert ctx.sent == []


# ------------------------------------------------------------ worker tracking

def goal(plan, attempt, packet):
    return f'Review packet.\nscanId: {plan["scanId"]}\nattemptId: {attempt}\npacketId: {packet["packetId"]}\n'


def submit(service, plan, packet, attempt):
    payload = worker(plan, [], role=packet['role'], attempt=attempt)
    payload['packetId'] = packet['packetId']
    service.submit_worker_result(payload)


def test_start_records_one_running_dispatch_and_is_idempotent(scan):
    service, plan = scan
    h = hooks(service, FakeCtx())
    packet = plan['packets'][0]
    for _ in range(2):
        h['subagent_start'](parent_session_id=SESSION, child_session_id='c1', child_goal=goal(plan, 'att_w1', packet))
    rows = service.store.dispatches(plan['scanId'])
    assert len(rows) == 1 and rows[0]['status'] == 'running' and rows[0]['packet_id'] == packet['packetId']
    assert rows[0]['child_session_id'] == 'c1'
    item = next(i for i in service.next_step(plan['scanId'])['items'] if i['packetId'] == packet['packetId'])
    assert item['state'] == 'running'


def test_start_for_another_chat_or_unnamed_attempt_is_ignored(scan):
    service, plan = scan
    h = hooks(service, FakeCtx())
    packet = plan['packets'][0]
    h['subagent_start'](parent_session_id='local-other', child_session_id='c1', child_goal=goal(plan, 'att_w1', packet))
    h['subagent_start'](parent_session_id=SESSION, child_session_id='c2', child_goal='Review the code, no ids here.')
    assert service.store.dispatches(plan['scanId']) == []


def test_start_never_reopens_an_accepted_attempt(scan):
    service, plan = scan
    packet = plan['packets'][0]
    submit(service, plan, packet, 'att_w1')
    hooks(service, FakeCtx())['subagent_start'](parent_session_id=SESSION, child_session_id='c1',
                                                child_goal=goal(plan, 'att_w1', packet))
    assert service.store.dispatches(plan['scanId']) == []


def test_stop_without_result_marks_missing_and_requeues(scan):
    service, plan = scan
    h = hooks(service, FakeCtx())
    packet = plan['packets'][0]
    h['subagent_start'](parent_session_id=SESSION, child_session_id='c1', child_goal=goal(plan, 'att_w1', packet))
    for _ in range(2):   # idempotent
        h['subagent_stop'](parent_session_id=SESSION, child_session_id='c1', child_status='failed', child_summary='')
    rows = service.store.dispatches(plan['scanId'])
    assert rows[0]['status'] == 'missing' and rows[0]['detail'] == 'failed'
    nxt = service.next_step(plan['scanId'])
    item = next(i for i in nxt['items'] if i['packetId'] == packet['packetId'])
    assert nxt['step'] == 'dispatch' and item['state'] == 'missing' and item['attemptId'] != 'att_w1'
    assert sum(e['kind'] == 'worker.missing' for e in service.store.events(plan['scanId'])) == 1


def test_stop_after_accepted_result_is_never_missing(scan):
    service, plan = scan
    h = hooks(service, FakeCtx())
    packet = plan['packets'][0]
    h['subagent_start'](parent_session_id=SESSION, child_session_id='c1', child_goal=goal(plan, 'att_w1', packet))
    submit(service, plan, packet, 'att_w1_1')   # incremental submit under the dispatch's attempt ID
    h['subagent_stop'](parent_session_id=SESSION, child_session_id='c1', child_status='completed')
    assert service.store.dispatches(plan['scanId'])[0]['status'] == 'finished'
    assert service.store.get_scan(plan['scanId'])['status'] == 'awaiting_analysis'
    attempts = {a['attempt_id']: a['status'] for a in service._attempts(plan['scanId'])}
    assert attempts == {'att_w1_1': 'accepted'}


def test_late_stop_after_redispatch_leaves_the_new_worker_running(scan):
    service, plan = scan
    h = hooks(service, FakeCtx())
    packet = plan['packets'][0]
    h['subagent_start'](parent_session_id=SESSION, child_session_id='c1', child_goal=goal(plan, 'att_w1', packet))
    h['subagent_start'](parent_session_id=SESSION, child_session_id='c2', child_goal=goal(plan, 'att_w2', packet))
    h['subagent_stop'](parent_session_id=SESSION, child_session_id='c1', child_status='timeout')
    states = {d['attempt_id']: d['status'] for d in service.store.dispatches(plan['scanId'])}
    assert states == {'att_w1': 'missing', 'att_w2': 'running'}
    item = next(i for i in service.next_step(plan['scanId'])['items'] if i['packetId'] == packet['packetId'])
    assert item['state'] == 'running' and item['attemptId'] == 'att_w2'


def test_compressed_child_id_falls_back_to_reported_attempt(scan):
    service, plan = scan
    h = hooks(service, FakeCtx())
    packet = plan['packets'][0]
    h['subagent_start'](parent_session_id=SESSION, child_session_id='c1', child_goal=goal(plan, 'att_w1', packet))
    h['subagent_stop'](parent_session_id=SESSION, child_session_id='c1-compressed', child_status='failed',
                       child_summary='Stopped early. attemptId: att_w1')
    assert service.store.dispatches(plan['scanId'])[0]['status'] == 'missing'
    h['subagent_stop'](parent_session_id=SESSION, child_session_id='unknown', child_status='failed', child_summary='no ids')


def test_validator_goal_records_its_candidates(scan):
    service, plan = scan
    h = hooks(service, FakeCtx())
    ids = 'cand_' + 'a' * 24 + ', cand_' + 'b' * 24
    h['subagent_start'](parent_session_id=SESSION, child_session_id='v1',
                        child_goal=f'scanId: {plan["scanId"]}\nattemptId: att_v1\nworkerRole: validator\nCandidates: {ids}')
    row = service.store.dispatches(plan['scanId'])[0]
    assert row['role'] == 'validator' and row['packet_id'] is None and len(row['candidate_ids']) == 2


def test_start_tool_links_the_calling_chat(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('x = 1\n')
    service = SecurityService(tmp_path / 'data')
    import json
    raw = tools.make_handler('security_scan_start', lambda: service)({'path': str(root)}, session_id='local-chat', task_id='t')
    sid = json.loads(raw)['result']['scanId']
    assert service.store.get_scan(sid)['options']['session'] == {'sessionId': 'local-chat', 'storedSessionId': 'local-chat'}
