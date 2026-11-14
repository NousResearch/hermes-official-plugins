"""Automatic scan recovery driven only by host hooks (no plugin timers or threads).

* ``on_session_end`` (end of every chat turn): if the chat owns an unfinished
  scan whose next step is actionable, queue one "Continue security scan" turn
  through ``ctx.inject_message``. A guard in the scan options stops loops: at
  most one queued continue, at most ``CAP`` continues without progress, and
  never after the user stopped the turn.
* ``subagent_start`` / ``subagent_stop``: remember which delegate is working on
  which attempt, and mark it missing when it ends without a saved result.

The scan database stays the source of truth; hooks only trigger a check. Every
entry point swallows its own errors so a plugin fault never reaches the host.
"""
from __future__ import annotations

import logging
import os
import re

from .canonical import utcnow

logger = logging.getLogger(__name__)

CAP = 3
PAUSED_STUCK = 'paused: needs attention'
PAUSED_OFF = 'paused: could not continue automatically'
ACTIONABLE = {'dispatch', 'validate', 'chains', 'finalize'}

_ATTEMPT_LABELED = re.compile(r'attempt[ _-]?id\W{0,3}(att_[A-Za-z0-9_-]{1,64})', re.I)
_ATTEMPT = re.compile(r'(?<![A-Za-z0-9_-])(att_[A-Za-z0-9_-]{1,64})')
_PACKET_LABELED = re.compile(r'packet[ _-]?id\W{0,3}(pkt_[0-9a-f]{24})', re.I)
_PACKET = re.compile(r'(?<![A-Za-z0-9_])(pkt_[0-9a-f]{24})(?![0-9a-f])')
_SCAN = re.compile(r'(?<![A-Za-z0-9_])(scan_[0-9a-f]{24})(?![0-9a-f])')
_CANDIDATE = re.compile(r'(?<![A-Za-z0-9_])(cand_[0-9a-f]{24})(?![0-9a-f])')
_ROLE = re.compile(r'worker[ _-]?role\W{0,3}([a-z][a-z-]{1,30})', re.I)


def continue_message(scan_id, step, count):
    return (f'Continue security scan {scan_id}: next step is {step} ({count} items). '
            f'Read security_scan_get section next, then do that step.')


def _first(pattern, text):
    match = pattern.search(text)
    return match.group(1) if match else None


def _unique(pattern, text):
    found = sorted(set(pattern.findall(text)))
    return found[0] if len(found) == 1 else None


# ---------------------------------------------------------------- turn end

def _write_guard(service, scan_id, change):
    def update(old):
        guard = dict(old or {})
        change(guard)
        return guard
    service.store.update_scan_option(scan_id, 'autoResume', update)


def _set_reason(service, scan, reason):
    if scan['reason'] != reason:
        service.store.set_scan_status(scan['scan_id'], scan['status'], reason=reason)


def continue_scan(service, scan, *, interrupted, inject):
    """Decide and act for one scan at the end of a turn; returns a short action label."""
    scan_id = scan['scan_id']
    nxt = service.next_step(scan_id)
    key = nxt['progressKey']
    guard = dict(scan['options'].get('autoResume') or {})
    if guard.get('lastProgressKey') != key:
        # The scan moved forward since the last look: start counting again.
        guard.update(lastProgressKey=key, count=0, pending=None, stoppedAt=None, offAt=None)
        _write_guard(service, scan_id, lambda g: g.update(lastProgressKey=key, count=0, pending=None,
                                                          stoppedAt=None, offAt=None))
        if scan['reason'] in (PAUSED_STUCK, PAUSED_OFF):
            _set_reason(service, scan, None)
            scan = service.store.get_scan(scan_id)
    if interrupted:
        # The user stopped the turn: never continue on our own until real progress is made.
        _write_guard(service, scan_id, lambda g: g.update(stoppedAt=key))
        return 'stopped-by-user'
    if guard.get('stoppedAt') == key:
        return 'stopped-by-user'
    if nxt['step'] not in ACTIONABLE:
        return 'nothing-to-do:' + nxt['step']
    pending = guard.get('pending')
    if pending:
        if not pending.get('seen'):
            # One queued continue at a time. Allow one turn ahead of it in the queue, then
            # treat it as used (an injected turn that never touched the scan).
            _write_guard(service, scan_id, lambda g: g.update(pending={**(g.get('pending') or {}), 'seen': 1}))
            return 'already-queued'
        guard['pending'] = None
    count = guard.get('count') or 0
    if count >= CAP:
        if scan['reason'] != PAUSED_STUCK:
            _set_reason(service, scan, PAUSED_STUCK)
            service.store.add_event(scan_id, 'autoresume.paused',
                                    f'Paused: no progress after {CAP} automatic continues. Press Continue scan or ask the agent to continue.',
                                    {'step': nxt['step'], 'progressKey': key})
        _write_guard(service, scan_id, lambda g: g.update(pending=None))
        return 'capped'
    # Record the queued continue before sending it, so a fast injected turn can clear it.
    _write_guard(service, scan_id, lambda g: g.update(count=count + 1, pending={'since': utcnow(), 'seen': 0},
                                                      lastProgressKey=key))
    try:
        delivered = bool(inject(continue_message(scan_id, nxt['step'], nxt['actionable'])))
    except Exception:  # host fault: treat as not delivered
        logger.exception('hermes-security: inject_message failed')
        delivered = False
    if delivered:
        if scan['reason'] in (PAUSED_STUCK, PAUSED_OFF):
            _set_reason(service, scan, None)
        service.store.add_event(scan_id, 'autoresume.scheduled', f'Continuing automatically: next step is {nxt["step"]}.',
                                {'step': nxt['step'], 'count': count + 1, 'progressKey': key})
        return 'scheduled'
    if nxt.get('running'):
        # Running workers report back into this chat and drive its next turn: not a pause.
        _write_guard(service, scan_id, lambda g: g.update(count=count, pending=None))
        if scan['reason'] == PAUSED_OFF:
            _set_reason(service, scan, None)
        return 'workers-running'
    _write_guard(service, scan_id, lambda g: g.update(count=count, pending=None, offAt=key))
    if guard.get('offAt') != key:
        _set_reason(service, scan, PAUSED_OFF)
        service.store.add_event(scan_id, 'autoresume.paused',
                                'Could not continue automatically: automatic continue is off or the chat is closed. '
                                'Turn it on with `hermes config set plugins.entries.hermes-security.allow_gateway_injection '
                                'true` and restart Hermes, or press Continue scan.',
                                {'step': nxt['step'], 'progressKey': key})
    return 'not-delivered'


def on_turn_end(service, *, session_id, interrupted, inject):
    """Run the guard for every open scan this chat owns; queue at most one continue per turn."""
    actions = []
    for scan in service.store.open_scans_for_session(session_id):
        try:
            if any(a == 'scheduled' for _, a in actions):
                break
            actions.append((scan['scan_id'], continue_scan(service, scan, interrupted=interrupted, inject=inject)))
        except Exception:
            logger.exception('hermes-security: automatic continue failed for %s', scan['scan_id'])
            actions.append((scan['scan_id'], 'error'))
    return actions


def note_activity(service, scan_id):
    """A security tool touched the scan: the queued continue (if any) has been picked up."""
    scan = service.store.get_scan(scan_id)
    if (scan['options'].get('autoResume') or {}).get('pending'):
        _write_guard(service, scan_id, lambda g: g.update(pending=None))


# ---------------------------------------------------------------- workers

def worker_started(service, *, parent_session_id, child_session_id, goal):
    """Record a delegate as running for the attempt named in its goal (``attemptId: att_…``)."""
    if not isinstance(goal, str) or not isinstance(parent_session_id, str) or not parent_session_id:
        return None
    goal = goal[:200_000]
    attempt = _first(_ATTEMPT_LABELED, goal) or _unique(_ATTEMPT, goal)
    if not attempt:
        return None
    scans = service.store.open_scans_for_session(parent_session_id)
    named = set(_SCAN.findall(goal))
    owned = [s for s in scans if s['scan_id'] in named] if named else scans
    if len(owned) != 1:
        return None   # not ours, or ambiguous: stay conservative
    scan_id = owned[0]['scan_id']
    packet = _first(_PACKET_LABELED, goal) or _unique(_PACKET, goal)
    candidates = sorted(set(_CANDIDATE.findall(goal)))[:500]
    role = _first(_ROLE, goal)
    if role is None and candidates and not packet:
        role = 'validator'
    row = service.store.record_dispatch(scan_id, attempt, packet_id=packet, role=role, candidate_ids=candidates,
                                        parent_session_id=parent_session_id,
                                        child_session_id=child_session_id if isinstance(child_session_id, str) else None)
    if row is not None:
        service.store.add_event(scan_id, 'worker.dispatched', 'Worker started.',
                                {'attemptId': attempt, 'packetId': packet, 'role': role, 'candidates': len(candidates)})
    return row


def worker_stopped(service, *, parent_session_id, child_session_id, status=None, summary=None):
    """Close the delegate's dispatch; it becomes 'missing' unless its result was accepted."""
    row = service.store.dispatch_for_child(child_session_id) if isinstance(child_session_id, str) and child_session_id else None
    if row is None and isinstance(summary, str) and isinstance(parent_session_id, str) and parent_session_id:
        # The child's session id changes when it compresses; fall back to the attempt ID it reports.
        attempt = _first(_ATTEMPT_LABELED, summary[:200_000]) or _unique(_ATTEMPT, summary[:200_000])
        if attempt:
            for scan in service.store.open_scans_for_session(parent_session_id):
                match = next((d for d in service.store.dispatches(scan['scan_id'])
                              if d['attempt_id'] == attempt and d['status'] == 'running'), None)
                if match:
                    row = match
                    break
    if row is None or row['status'] != 'running':
        return None
    done = service.store.finish_dispatch(row['scan_id'], row['attempt_id'],
                                         detail=status if isinstance(status, str) else None)
    if done is not None and done['status'] == 'missing':
        service.store.add_event(row['scan_id'], 'worker.missing', 'A worker ended without a saved result; its work is queued again.',
                                {'attemptId': row['attempt_id'], 'packetId': row['packet_id'], 'status': status})
    return done


# ---------------------------------------------------------------- host adapters

def _route_key():
    """The calling session's route key, read through the documented plugin accessor.

    ``get_session_env`` falls back to ``os.environ``, which a process can inherit from another
    session. A key that only mirrors the environment is dropped: the key-less inject that follows
    is routed by the host from the call's own context, so it can never reach another chat.
    """
    try:
        from gateway.session_context import get_session_env
        key = get_session_env('HERMES_SESSION_KEY') or ''
    except Exception:  # not inside the gateway, or an older host
        return ''
    return '' if key == os.environ.get('HERMES_SESSION_KEY') else key


def make_hooks(ctx, get_service):
    """Hook callbacks for ``ctx.register_hook``; each one never raises into the host."""

    def inject(text):
        key = _route_key()
        return ctx.inject_message(text, session_key=key) if key else ctx.inject_message(text)

    def on_session_end(session_id=None, interrupted=False, **_):
        try:
            if isinstance(session_id, str) and session_id:
                on_turn_end(get_service(), session_id=session_id, interrupted=bool(interrupted), inject=inject)
        except Exception:
            logger.exception('hermes-security: on_session_end failed')

    def subagent_start(parent_session_id=None, child_session_id=None, child_goal=None, **_):
        try:
            worker_started(get_service(), parent_session_id=parent_session_id,
                           child_session_id=child_session_id, goal=child_goal)
        except Exception:
            logger.exception('hermes-security: subagent_start failed')

    def subagent_stop(parent_session_id=None, child_session_id=None, child_status=None, child_summary=None, **_):
        try:
            worker_stopped(get_service(), parent_session_id=parent_session_id, child_session_id=child_session_id,
                           status=child_status, summary=child_summary)
        except Exception:
            logger.exception('hermes-security: subagent_stop failed')

    return {'on_session_end': on_session_end, 'subagent_start': subagent_start, 'subagent_stop': subagent_stop}
