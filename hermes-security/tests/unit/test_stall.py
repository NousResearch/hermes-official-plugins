"""A scan the agent has stopped working on reads as stalled, not running.

Derived on read from stored state only (the workbench polls), so no timers or threads.
"""
from datetime import datetime, timedelta, timezone

import pytest

from hermes_security.orchestration.stall import STALL_SECONDS, derive
from hermes_security.service import SecurityService

NOW = datetime(2026, 10, 25, 2, 40, tzinfo=timezone.utc)


def stamp(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')


def scan(**over):
    return {'status': 'awaiting_analysis', 'sealed_at': None, 'reason': None, **over}


def event(minutes_ago, kind='scan.checkpoint', message='note'):
    return {'kind': kind, 'message': message, 'created_at': stamp(minutes_ago)}


def test_actionable_step_without_progress_for_a_while_is_stalled():
    got = derive(scan(), {'step': 'dispatch'}, event(STALL_SECONDS // 60 + 1), NOW)
    assert got['since'] == stamp(STALL_SECONDS // 60 + 1)
    assert 'review' in got['why']


def test_recent_progress_is_not_stalled():
    assert derive(scan(), {'step': 'dispatch'}, event(2), NOW) is None


@pytest.mark.parametrize('step', ['wait', 'done'])
def test_running_workers_or_finished_work_is_not_stalled(step):
    assert derive(scan(), {'step': step}, event(600), NOW) is None


@pytest.mark.parametrize('over', [{'sealed_at': stamp(1)}, {'status': 'canceled'}, {'status': 'completed'},
                                  {'reason': 'paused: needs attention'}])
def test_finished_stopped_or_paused_scans_are_not_stalled(over):
    assert derive(scan(**over), {'step': 'dispatch'}, event(600), NOW) is None


def test_agent_reported_block_is_stalled_at_once_with_its_reason():
    got = derive(scan(), {'step': 'dispatch'}, event(1, 'scan.blocked', 'A plugin error stopped the review.'), NOW)
    assert got == {'since': stamp(1), 'why': 'A plugin error stopped the review.'}


def test_blocked_step_is_stalled_at_once():
    got = derive(scan(), {'step': 'blocked', 'reason': 'Saved candidate records conflict.'}, event(1), NOW)
    assert got['why'] == 'Saved candidate records conflict.'


def _started(tmp_path):
    root = tmp_path / 'repo'; root.mkdir(); (root / 'app.py').write_text('print(1)\n')
    service = SecurityService(tmp_path / 'data')
    return service, service.start_scan(path=str(root))['scanId']


def test_blocked_checkpoint_shows_in_the_scan_and_list(tmp_path):
    service, sid = _started(tmp_path)
    assert service.get_scan(sid)['stall'] is None
    service.checkpoint(sid, coverage=[], note='A plugin error stopped the review; results so far are saved.', blocked=True)
    detail = service.get_scan(sid)['stall']
    assert detail['why'].startswith('A plugin error stopped the review')
    listed = service.workbench_scans()['items'][0]['stall']
    assert listed == detail


def test_blocked_checkpoint_is_not_progress_for_automatic_continue(tmp_path):
    service, sid = _started(tmp_path)
    before = service.next_step(sid)['progressKey']
    service.checkpoint(sid, coverage=[], note='stuck', blocked=True)
    assert service.next_step(sid)['progressKey'] == before


def test_blocked_must_be_a_boolean(tmp_path):
    from hermes_security.errors import ValidationError
    service, sid = _started(tmp_path)
    with pytest.raises(ValidationError):
        service.checkpoint(sid, coverage=[], note='x', blocked='yes')
