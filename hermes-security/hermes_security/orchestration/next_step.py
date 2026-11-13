"""What the parent agent should do next, derived only from stored scan state.

A wedged or dead parent turn loses its place even when every worker result is
saved. This pure function rebuilds that place from the store, so any later turn
(or an automatic continue) can pick up exactly where the scan stopped.
"""
from datetime import datetime, timezone
from hashlib import sha256

from ..canonical import stable_id

ITEM_LIMIT = 50
STALE_DISPATCH_SECONDS = 2 * 3600   # a worker silent this long is treated as lost
CHAINABLE = {'candidate', 'source_supported', 'runtime_confirmed'}
STEPS = ('dispatch', 'wait', 'validate', 'chains', 'finalize', 'done', 'blocked')

HOW = {
    'dispatch': ('Send one delegate_task call per packet, at most `slots` now. Put "attemptId: <attemptId>" and '
                 '"packetId: <packetId>" from the item in each goal, and read each packet\'s files with '
                 'security_scan_get section packets.'),
    'wait': 'Workers are still running. Wait for their results; do not start them again.',
    'validate': ('Send these candidates to fresh validator delegates (workerRole validator). Put "attemptId: <id>" '
                 '(start from `attemptId`, one per validator) and every candidate ID it checks in each goal.'),
    'chains': 'Call security_scan_record_chains for this scan.',
    'finalize': 'Call security_scan_finalize for this scan, then read it back and report the result.',
    'done': 'The scan is finished. Read the report with security_scan_get section report.',
    'blocked': 'Do nothing unless the user asks; tell them why the scan cannot continue.',
}

# The validate step again, for pairs of candidates that may be one bug reported twice.
DUPLICATE_HOW = ('Send each pair to a fresh validator delegate (workerRole validator). Put "attemptId: <id>" (start from '
                 '`attemptId`, one per validator) and both candidate IDs in the goal. The validator rereads both and '
                 'resubmits ONE of them: with provenance.duplicateOf naming the candidate to keep when both are the '
                 'same broken control fixed by one remediation, or with provenance.distinctFrom naming the other when '
                 'they are different bugs, or, only when the code read cannot settle it, with '
                 'provenance.maybeDuplicateOf naming the other and the reason in proofGaps (the scan then finishes '
                 'with partial coverage). provenance.subsumes on a resubmitted survivor still works.')


def _parse(stamp):
    try:
        return datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _covers(dispatch_id, attempt_id):
    """Workers submit incrementally as <attemptId>_1, _2, ...; any of them answers the dispatch."""
    return attempt_id == dispatch_id or attempt_id.startswith(dispatch_id + '_')


def _pairs_key(duplicates):
    """Progress while pairs wait for a verdict: it changes only when a pair leaves or joins the set.

    Every accepted result writes an event, so the event key would count a validator that answers
    without a verdict as progress and the automatic-continue limit would never trip. A string
    cannot equal the integer event keys of the other steps.
    """
    ids = sorted(' '.join(sorted(pair['candidateIds'])) for pair in duplicates)
    return 'pairs:' + sha256('\n'.join(ids).encode()).hexdigest()[:16]


def _result(scan, step, reason, progress_key, items=(), actionable=None, **extra):
    """``items`` lists work to do first, then work already running; ``actionable`` counts the former."""
    items = list(items)
    if actionable is None:
        actionable = 1 if step in {'chains', 'finalize'} else len(items)
    return {'scanId': scan['scan_id'], 'step': step, 'reason': reason, 'items': items[:ITEM_LIMIT],
            'total': len(items), 'actionable': actionable, 'progressKey': progress_key, 'how': HOW[step], **extra}


def derive(*, scan, packets, attempts, dispatches, reduced, receipts, chains_recorded, progress_key, now,
           max_parallel=6, deep_stopped=False, duplicates=()):
    """Return {step, items, total, reason, progressKey, how, ...} for one scan.

    Order: packets without an accepted result -> candidates without a validator
    verdict -> possible duplicate pairs without a verdict -> chains -> finalize
    -> done. A canceled scan is blocked. While pairs wait, progressKey is derived
    from ``duplicates`` instead of ``progress_key``.
    """
    if scan.get('sealed_at'):
        return _result(scan, 'done', 'The scan is finished.', progress_key)
    if scan.get('status') == 'canceled':
        return _result(scan, 'blocked', 'The user stopped this scan; it continues only if they resume it.', progress_key)
    if (scan.get('options') or {}).get('dependenciesPending'):
        # Packets carry dependency leads, so none go out before the background check saves them.
        why = ('The target is still being inventoried.' if scan['options'].get('preparing')
               else 'The dependency check is still running.')
        return _result(scan, 'wait', why, progress_key, actionable=0,
                       slots=0, running=0, how='Wait about 30 seconds, then read security_scan_get section next '
                                               'again. Do not dispatch workers until the step changes.')
    if 'snapshot_digest' in scan and not scan['snapshot_digest']:
        return _result(scan, 'blocked', scan.get('reason') or
                       'The target has no inventory snapshot; resume preparation before reviewing.', progress_key)
    accepted = [a for a in attempts if a.get('status') == 'accepted']
    accepted_ids = [a['attempt_id'] for a in accepted]

    def live(row):
        if row.get('status') != 'running':
            return False
        if any(_covers(row['attempt_id'], aid) for aid in accepted_ids):
            return False   # its result is saved; the stop hook may simply not have fired yet
        started = _parse(row.get('dispatched_at'))
        return started is not None and (now - started).total_seconds() < STALE_DISPATCH_SECONDS

    running = [d for d in dispatches if live(d)]
    slots = max(0, max_parallel - len(running))
    done_packets = {a.get('packet_id') for a in accepted}
    items, waiting = [], []
    if not deep_stopped:
        for packet in sorted(packets, key=lambda p: (p.get('priority', 0), p['packetId'])):
            pid = packet['packetId']
            if pid in done_packets:
                continue
            tries = [d for d in dispatches if d.get('packet_id') == pid]
            failed = [a for a in attempts if a.get('packet_id') == pid and a.get('status') != 'accepted']
            if tries and live(tries[-1]):
                waiting.append({'packetId': pid, 'role': packet.get('role'), 'state': 'running',
                                'attemptId': tries[-1]['attempt_id']})
                continue
            state = 'missing' if tries or failed else 'pending'
            items.append({'packetId': pid, 'role': packet.get('role'), 'review': packet.get('review'),
                          'area': packet.get('area'), 'fileCount': packet.get('fileCount', len(packet.get('files', []))),
                          'state': state,
                          'attemptId': stable_id('att', scan['scan_id'], pid, 'next', len(tries) + len(failed))})
    if items and slots:
        return _result(scan, 'dispatch', f'{len(items)} review packet(s) have no saved result yet.', progress_key,
                       items + waiting, actionable=len(items), slots=slots, running=len(running))
    if items or waiting:
        return _result(scan, 'wait', f'{len(running)} worker(s) are still running.', progress_key,
                       waiting + items, actionable=0, slots=slots, running=len(running))
    if reduced is None:
        return _result(scan, 'blocked', 'Saved candidate records conflict; read security_scan_get section candidates.',
                       progress_key)
    validator_ids = {a['attempt_id'] for a in accepted if a.get('role') == 'validator'}
    receipted = {r.get('candidateId') for r in receipts}
    unchecked = [row for row in reduced['retained']
                 if row['candidateId'] not in receipted
                 and not validator_ids.intersection(row.get('provenance', {}).get('workerAttemptIds', []))]
    if unchecked:
        validators = [d for d in running if d.get('role') == 'validator' or not d.get('packet_id')]
        assigned = {cid for d in validators for cid in d.get('candidate_ids') or []}
        unnamed = any(not d.get('candidate_ids') for d in validators)
        rows = []
        for row in unchecked:
            location = (row.get('locations') or [{}])[0]
            rows.append({'candidateId': row['candidateId'], 'title': row.get('title'), 'ruleId': row.get('ruleId'),
                         'evidenceState': row.get('evidenceState'), 'path': location.get('path'),
                         'startLine': location.get('startLine'),
                         'state': 'running' if unnamed or row['candidateId'] in assigned else 'pending'})
        pending = [r for r in rows if r['state'] == 'pending']
        prior = len([a for a in attempts if a.get('role') == 'validator']) + len([d for d in dispatches if not d.get('packet_id')])
        if pending and slots:
            return _result(scan, 'validate', f'{len(pending)} candidate(s) have not been checked by a validator.',
                           progress_key, pending + [r for r in rows if r['state'] == 'running'],
                           actionable=len(pending), slots=slots, running=len(running),
                           attemptId=stable_id('att', scan['scan_id'], 'validator', 'next', prior))
        return _result(scan, 'wait', f'{len(running)} validator(s) are still running.', progress_key, rows,
                       actionable=0, slots=slots, running=len(running))
    if duplicates:
        # Still the validate step: only the item kind and the instructions differ from the candidates above.
        validators = [d for d in running if d.get('role') == 'validator' or not d.get('packet_id')]
        unnamed = any(not d.get('candidate_ids') for d in validators)
        titles = {row['candidateId']: row.get('title') for row in reduced['retained']}
        rows = []
        for pair in duplicates:
            ids = pair['candidateIds']
            assigned = any(set(ids) <= set(d.get('candidate_ids') or []) for d in validators)
            rows.append({'kind': 'duplicate', 'candidateIds': ids, 'path': pair.get('path'), 'lines': pair.get('lines'),
                         'titles': [titles.get(cid) for cid in ids],
                         'state': 'running' if unnamed or assigned else 'pending'})
        pending = [r for r in rows if r['state'] == 'pending']
        prior = len([a for a in attempts if a.get('role') == 'validator']) + len([d for d in dispatches if not d.get('packet_id')])
        pairs_key = _pairs_key(duplicates)
        if pending and slots:
            return _result(scan, 'validate', f'{len(pending)} possible duplicate pair(s) need a same-bug-or-different verdict.',
                           pairs_key, pending + [r for r in rows if r['state'] == 'running'],
                           actionable=len(pending), slots=slots, running=len(running), how=DUPLICATE_HOW,
                           attemptId=stable_id('att', scan['scan_id'], 'validator', 'next', prior))
        return _result(scan, 'wait', f'{len(running)} validator(s) are still running.', pairs_key, rows,
                       actionable=0, slots=slots, running=len(running))
    if not chains_recorded and any(r.get('evidenceState') in CHAINABLE for r in reduced['retained']):
        return _result(scan, 'chains', 'Every candidate is checked; attack paths are not recorded yet.', progress_key)
    return _result(scan, 'finalize', 'All review work is saved; the scan is ready to finish.', progress_key)
