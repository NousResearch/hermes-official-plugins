"""The parent's next step comes from stored scan state only (pure function matrix)."""
from datetime import datetime, timedelta, timezone

from hermes_security.orchestration import next_step, reducer

NOW = datetime(2026, 10, 24, 20, 0, 0, tzinfo=timezone.utc)
SCAN = {'scan_id': 'scan_1', 'status': 'awaiting_analysis', 'sealed_at': None}
PACKETS = [{'packetId': 'pkt_base', 'role': 'baseline', 'review': 'baseline', 'area': None, 'fileCount': 3, 'priority': 0},
           {'packetId': 'pkt_area', 'role': 'investigator', 'review': 'area', 'area': 'src', 'fileCount': 3, 'priority': 1}]


def stamp(delta=timedelta()):
    return (NOW - delta).strftime('%Y-%m-%dT%H:%M:%SZ')


def attempt(aid, packet=None, role='investigator', status='accepted'):
    return {'attempt_id': aid, 'packet_id': packet, 'role': role, 'status': status, 'finished_at': stamp()}


def dispatch(aid, packet=None, status='running', age=timedelta(minutes=5), candidates=(), role=None):
    return {'attempt_id': aid, 'packet_id': packet, 'role': role, 'status': status,
            'dispatched_at': stamp(age), 'candidate_ids': list(candidates)}


def cand(cid, aid, state='candidate', rule='r', anchor=None, subsumes=None):
    row = {'candidateId': cid, 'ruleId': rule + cid, 'identity': {'anchor': anchor or cid}, 'evidenceState': state,
           'title': 'Title ' + cid, 'locations': [{'path': 'a.py', 'startLine': 1, 'endLine': 2}],
           'provenance': {'workerAttemptIds': [aid]}}
    if subsumes:
        row['provenance']['subsumes'] = list(subsumes)
    return row


def derive(**kw):
    args = dict(scan=SCAN, packets=PACKETS, attempts=[], dispatches=[], reduced={'retained': [], 'rejected': []},
                receipts=[], chains_recorded=False, progress_key=7, now=NOW)
    args.update(kw)
    return next_step.derive(**args)


def test_sealed_scan_is_done_and_canceled_scan_is_blocked():
    assert derive(scan={**SCAN, 'sealed_at': stamp(), 'status': 'completed'})['step'] == 'done'
    blocked = derive(scan={**SCAN, 'status': 'canceled'})
    assert blocked['step'] == 'blocked' and 'stopped' in blocked['reason']


def test_fresh_scan_dispatches_every_packet_with_fresh_attempt_ids():
    result = derive()
    assert result['step'] == 'dispatch' and result['total'] == result['actionable'] == 2 and result['slots'] == 6
    assert [i['packetId'] for i in result['items']] == ['pkt_base', 'pkt_area']
    assert all(i['state'] == 'pending' and i['attemptId'].startswith('att_') for i in result['items'])
    assert result['progressKey'] == 7 and result['scanId'] == 'scan_1'
    assert len({i['attemptId'] for i in result['items']}) == 2


def test_running_workers_mean_wait_not_redispatch():
    result = derive(attempts=[attempt('att_b', 'pkt_base', 'baseline')], dispatches=[dispatch('att_a', 'pkt_area')])
    assert result['step'] == 'wait' and result['running'] == 1 and result['actionable'] == 0
    assert result['items'][0]['state'] == 'running'


def test_missing_or_stale_dispatch_is_pending_again_with_a_new_attempt_id():
    for row in (dispatch('att_a', 'pkt_area', status='missing'), dispatch('att_a', 'pkt_area', age=timedelta(hours=3))):
        result = derive(attempts=[attempt('att_b', 'pkt_base', 'baseline')], dispatches=[row])
        assert result['step'] == 'dispatch' and [i['packetId'] for i in result['items']] == ['pkt_area']
        assert result['items'][0]['state'] == 'missing' and result['items'][0]['attemptId'] != 'att_a'


def test_attempt_marked_missing_by_resume_is_pending():
    result = derive(attempts=[attempt('att_b', 'pkt_base', 'baseline'), attempt('att_a', 'pkt_area', status='missing')])
    assert result['step'] == 'dispatch' and result['items'][0]['state'] == 'missing'


def test_remainder_packet_is_dispatched():
    packets = PACKETS + [{'packetId': 'pkt_rem', 'role': 'investigator', 'review': 'remainder', 'fileCount': 1, 'priority': 4}]
    result = derive(packets=packets, attempts=[attempt('att_b', 'pkt_base', 'baseline'), attempt('att_a', 'pkt_area')])
    assert result['step'] == 'dispatch' and [i['packetId'] for i in result['items']] == ['pkt_rem']


def test_full_pool_waits_for_a_free_slot():
    packets = PACKETS + [{'packetId': f'pkt_{i}', 'role': 'investigator', 'review': 'area', 'priority': 2} for i in range(6)]
    rows = [dispatch(f'att_{i}', f'pkt_{i}') for i in range(6)]
    result = derive(packets=packets, dispatches=rows)
    assert result['step'] == 'wait' and result['slots'] == 0 and result['running'] == 6


def test_dispatch_with_an_accepted_incremental_submit_does_not_hold_a_slot():
    result = derive(attempts=[attempt('att_a_1', 'pkt_area')], dispatches=[dispatch('att_a', 'pkt_area')])
    assert result['step'] == 'dispatch' and result['running'] == 0
    assert [i['packetId'] for i in result['items']] == ['pkt_base']


def test_unvalidated_candidates_go_to_validators():
    rows = [cand('c1', 'att_b', 'source_supported'), cand('c2', 'att_a')]
    result = derive(attempts=[attempt('att_b', 'pkt_base', 'baseline'), attempt('att_a', 'pkt_area')],
                    reduced={'retained': rows, 'rejected': []})
    assert result['step'] == 'validate' and [i['candidateId'] for i in result['items']] == ['c1', 'c2']
    assert result['attemptId'].startswith('att_') and result['items'][0]['title'] == 'Title c1'


def test_candidate_with_a_validation_receipt_counts_as_checked():
    rows = [cand('c1', 'att_b', 'source_supported')]
    result = derive(attempts=[attempt('att_b', 'pkt_base', 'baseline'), attempt('att_a', 'pkt_area')],
                    reduced={'retained': rows, 'rejected': []}, receipts=[{'candidateId': 'c1', 'receiptId': 'vrc_1'}])
    assert result['step'] == 'chains'


def test_running_validator_means_wait():
    rows = [cand('c1', 'att_b'), cand('c2', 'att_b')]
    accepted = [attempt('att_b', 'pkt_base', 'baseline'), attempt('att_a', 'pkt_area')]
    named = derive(attempts=accepted, reduced={'retained': rows, 'rejected': []},
                   dispatches=[dispatch('att_v', candidates=['c1', 'c2'], role='validator')])
    assert named['step'] == 'wait'
    partial = derive(attempts=accepted, reduced={'retained': rows, 'rejected': []},
                     dispatches=[dispatch('att_v', candidates=['c1'], role='validator')])
    assert partial['step'] == 'validate' and partial['actionable'] == 1
    assert [(i['candidateId'], i['state']) for i in partial['items']] == [('c2', 'pending'), ('c1', 'running')]
    unnamed = derive(attempts=accepted, reduced={'retained': rows, 'rejected': []},
                     dispatches=[dispatch('att_v', role='validator')])
    assert unnamed['step'] == 'wait'


def test_real_stuck_scan_shape_needs_chains_then_finalize():
    """scan_848b…: 2 packets accepted, 3 validators accepted (packet NULL), 2 candidates subsumed, no chains."""
    attempts = [attempt('att_area_deps_01', 'pkt_area'), attempt('att_baseline_01', 'pkt_base', 'baseline'),
                attempt('att_validator_init_01', None, 'validator'), attempt('att_validator_osv_01', None, 'validator'),
                attempt('att_validator_osv_02', None, 'validator')]
    discovery = [cand('c1f60', 'att_area_deps_01'), cand('c415e', 'att_baseline_01', 'source_supported'),
                 cand('c8c83', 'att_baseline_01', 'source_supported'), cand('cae7c', 'att_area_deps_01', 'source_supported'),
                 cand('cc2d2', 'att_area_deps_01', 'source_supported'), cand('cd41d', 'att_baseline_01')]
    validators = [cand('c415e', 'att_validator_osv_01', 'source_supported'),
                  cand('c8c83', 'att_validator_init_01', 'source_supported', subsumes=['c1f60']),
                  cand('cae7c', 'att_validator_init_01', 'source_supported'),
                  cand('cc2d2', 'att_validator_osv_02', 'source_supported', subsumes=['cd41d'])]
    reduced = reducer.reduce_candidates(discovery + validators)
    assert not reduced['problems']
    result = derive(attempts=attempts, reduced=reduced)
    assert result['step'] == 'chains' and result['total'] == 0 and result['actionable'] == 1
    assert derive(attempts=attempts, reduced=reduced, chains_recorded=True)['step'] == 'finalize'


def test_nothing_to_chain_goes_straight_to_finalize():
    rows = [cand('c1', 'att_v', 'rejected')]
    result = derive(attempts=[attempt('att_b', 'pkt_base', 'baseline'), attempt('att_a', 'pkt_area'),
                              attempt('att_v', None, 'validator')], reduced={'retained': [], 'rejected': rows})
    assert result['step'] == 'finalize'


def test_conflicting_candidate_records_block():
    result = derive(attempts=[attempt('att_b', 'pkt_base', 'baseline'), attempt('att_a', 'pkt_area')], reduced=None)
    assert result['step'] == 'blocked'


def test_stopped_deep_scan_skips_remaining_passes():
    result = derive(attempts=[attempt('att_b', 'pkt_base', 'baseline')], deep_stopped=True)
    assert result['step'] == 'finalize'


def test_items_are_capped():
    packets = [{'packetId': f'pkt_{i:03d}', 'role': 'investigator', 'review': 'area', 'priority': 1} for i in range(120)]
    result = derive(packets=packets)
    assert result['total'] == 120 and len(result['items']) == next_step.ITEM_LIMIT
