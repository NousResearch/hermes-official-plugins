"""Possible duplicates: wider detection, explicit same-bug-or-different verdicts, and the step that asks for them."""
import pytest

from hermes_security.domain.validate import public_problems, validate_worker_result
from hermes_security.orchestration.reducer import conservation_check, possible_duplicates, reduce_candidates
from test_next_step import attempt, derive, dispatch
from test_schemas import worker

A, B, C, D = ('cand_' + letter * 24 for letter in 'abcd')
ACCEPTED = [attempt('att_b', 'pkt_base', 'baseline'), attempt('att_a', 'pkt_area'), attempt('att_v', None, 'validator')]


def loc(path, start, end, role=None):
    return {'path': path, 'startLine': start, 'endLine': end, **({'role': role} if role else {})}


def row(cid, locations, state='source_supported', aid='att_v', **verdict):
    return {'candidateId': cid, 'ruleId': 'rule.' + cid, 'identity': {'anchor': cid}, 'evidenceState': state,
            'title': 'Title ' + cid, 'locations': locations, 'provenance': {'workerAttemptIds': [aid], **verdict}}


def pair_rows(**verdict_on_b):
    """Two live candidates led by different files that share one sink."""
    return [row(A, [loc('config.py', 1, 5), loc('route.py', 10, 20, 'sink')]),
            row(B, [loc('route.py', 40, 41), loc('route.py', 12, 14, 'sink')], **verdict_on_b)]


def pairs_of(rows):
    reduced = reduce_candidates(rows)
    assert reduced['problems'] == [] and conservation_check(rows, reduced) == []
    return reduced, [p['candidateIds'] for p in possible_duplicates(reduced['retained'], reduced['distinct'])]


def test_a_shared_sink_or_root_control_pairs_candidates_with_different_first_locations():
    assert possible_duplicates(pair_rows()) == [{'candidateIds': [A, B], 'path': 'route.py', 'lines': [12, 14]}]
    control = [row(A, [loc('a.py', 1, 2), loc('auth.py', 30, 40, 'root_control')]),
               row(B, [loc('b.py', 1, 2), loc('auth.py', 40, 44, 'root_control')])]
    assert possible_duplicates(control) == [{'candidateIds': [A, B], 'path': 'auth.py', 'lines': [40, 40]}]
    # One side's first location against the other side's sink also counts.
    mixed = [row(A, [loc('route.py', 10, 20)]), row(B, [loc('b.py', 1, 2), loc('route.py', 20, 22, 'sink')])]
    assert [p['candidateIds'] for p in possible_duplicates(mixed)] == [[A, B]]


@pytest.mark.parametrize('role', ['entry_point', 'caller', 'wrapper'])
def test_an_overlap_only_in_a_supporting_location_is_not_a_pair(role):
    rows = [row(A, [loc('a.py', 1, 2), loc('route.py', 10, 20, role)]),
            row(B, [loc('b.py', 1, 2), loc('route.py', 12, 14, role)])]
    assert possible_duplicates(rows) == []


def test_several_overlapping_spans_still_give_one_pair_with_the_lowest_overlap():
    rows = [row(A, [loc('z.py', 5, 9), loc('m.py', 50, 60, 'sink'), loc('m.py', 10, 20, 'root_control')]),
            row(B, [loc('z.py', 7, 8), loc('m.py', 55, 56, 'sink'), loc('m.py', 15, 30, 'root_control')])]
    assert possible_duplicates(rows) == [{'candidateIds': [A, B], 'path': 'm.py', 'lines': [15, 20]}]


@pytest.mark.parametrize('state', ['rejected', 'inconclusive'])
def test_rejected_and_inconclusive_rows_are_never_paired(state):
    rows = pair_rows()
    rows[1]['evidenceState'] = state
    assert possible_duplicates(rows) == []


def test_distinct_from_on_either_side_removes_the_pair():
    assert pairs_of(pair_rows())[1] == [[A, B]]
    reduced, pairs = pairs_of(pair_rows(distinctFrom=[A]))
    assert pairs == [] and reduced['distinct'] == [[A, B]] and len(reduced['retained']) == 2
    rows = pair_rows()
    rows[0]['provenance']['distinctFrom'] = [B]
    assert pairs_of(rows)[1] == []
    # Rows that still carry the field need no second argument.
    assert possible_duplicates(rows) == []
    # A verdict about another candidate leaves this pair flagged.
    assert pairs_of(pair_rows(distinctFrom=[C]))[1] == [[A, B]]


def test_distinct_from_survives_a_later_record_that_omits_it():
    first, second = pair_rows(distinctFrom=[A])
    later = row(B, second['locations'], aid='att_w')
    reduced, pairs = pairs_of([first, second, later])
    assert pairs == [] and reduced['distinct'] == [[A, B]]
    kept = next(r for r in reduced['retained'] if r['candidateId'] == B)
    assert kept['provenance']['workerAttemptIds'] == ['att_v', 'att_w']


def test_verdict_fields_are_stripped_from_retained_rows():
    reduced, _ = pairs_of(pair_rows(distinctFrom=[A], duplicateOf=B, subsumes=[]))
    for kept in reduced['retained']:
        assert not {'duplicateOf', 'distinctFrom', 'subsumes'} & set(kept['provenance'])


def test_duplicate_of_absorbs_the_candidate_into_the_named_survivor():
    first, second = pair_rows(duplicateOf=A)
    first['provenance']['workerAttemptIds'] = ['att_b']
    first['severity'] = {'level': 'high'}
    reduced, pairs = pairs_of([first, second])
    assert pairs == [] and [r['candidateId'] for r in reduced['retained']] == [A]
    assert reduced['absorbed'] == [{'candidateId': B, 'into': A, 'reason': 'validator duplicate verdict'}]
    survivor = reduced['retained'][0]
    assert survivor['provenance']['sourceCandidateIds'] == [A, B]
    assert survivor['provenance']['supersedes'] == [B]
    assert survivor['provenance']['workerAttemptIds'] == ['att_b', 'att_v']
    # The survivor keeps its own locations and severity; only subsumes carries locations over.
    assert survivor['locations'] == first['locations'] and survivor['severity'] == {'level': 'high'}


def test_the_latest_record_carrying_duplicate_of_wins():
    first, second = pair_rows(duplicateOf=C)
    third = row(C, [loc('other.py', 1, 2)])
    newer = row(B, second['locations'], aid='att_w', duplicateOf=A)
    plain = row(B, second['locations'], aid='att_x')
    reduced, _ = pairs_of([first, third, second, newer, plain])
    assert {'candidateId': B, 'into': A, 'reason': 'validator duplicate verdict'} in reduced['absorbed']
    assert sorted(r['candidateId'] for r in reduced['retained']) == [A, C]


def test_duplicate_of_chains_end_at_one_survivor():
    rows = [row(A, [loc('route.py', 10, 20)], duplicateOf=B), row(B, [loc('route.py', 10, 20)], duplicateOf=C),
            row(C, [loc('route.py', 10, 20)])]
    reduced, pairs = pairs_of(rows)
    assert pairs == [] and [r['candidateId'] for r in reduced['retained']] == [C]
    assert sorted((e['candidateId'], e['into']) for e in reduced['absorbed']) == [(A, C), (B, C)]
    assert reduced['retained'][0]['provenance']['sourceCandidateIds'] == [A, B, C]


def test_duplicate_of_follows_a_target_that_was_already_merged():
    # C shares B's rule and anchor, so B absorbed it; a verdict naming C lands on B.
    merged = row(C, [loc('other.py', 1, 2)], state='candidate')
    merged.update(ruleId='rule.' + B, identity={'anchor': B})
    rows = [row(A, [loc('route.py', 10, 20)], duplicateOf=C), row(B, [loc('route.py', 10, 20)]), merged]
    reduced, pairs = pairs_of(rows)
    assert pairs == [] and [r['candidateId'] for r in reduced['retained']] == [B]
    assert reduced['retained'][0]['provenance']['sourceCandidateIds'] == [A, B, C]


@pytest.mark.parametrize('target', [B, D, C], ids=['itself', 'unknown', 'rejected'])
def test_duplicate_of_that_names_no_live_survivor_merges_nothing(target):
    rows = pair_rows(duplicateOf=target) + [row(C, [loc('route.py', 12, 14, 'sink')], state='rejected')]
    reduced, pairs = pairs_of(rows)
    assert pairs == [[A, B]] and reduced['absorbed'] == []
    assert [r['candidateId'] for r in reduced['rejected']] == [C]


def test_duplicate_of_a_rejected_candidate_is_ignored_even_when_a_survivor_subsumed_it():
    rows = pair_rows(duplicateOf=C) + [row(C, [loc('z.py', 1, 2)], state='rejected')]
    rows[0]['provenance']['subsumes'] = [C]
    reduced, pairs = pairs_of(rows)
    assert [r['candidateId'] for r in reduced['retained']] == [A, B] and pairs == [[A, B]]
    assert reduced['absorbed'] == [{'candidateId': C, 'into': A, 'reason': 'explicit remediation subsumption'}]


def test_duplicate_of_an_inconclusive_candidate_merges_nothing():
    rows = pair_rows(duplicateOf=C) + [row(C, [loc('route.py', 12, 14, 'sink')], state='inconclusive')]
    reduced, pairs = pairs_of(rows)
    assert pairs == [[A, B]] and reduced['absorbed'] == [] and len(reduced['retained']) == 3


def test_a_two_way_cycle_merges_nothing_and_stays_flagged():
    first, second = pair_rows(duplicateOf=A)
    first['provenance']['duplicateOf'] = B
    reduced, pairs = pairs_of([first, second])
    assert pairs == [[A, B]] and reduced['absorbed'] == [] and len(reduced['retained']) == 2
    # A third candidate pointing into the cycle has no survivor to join either.
    third = row(C, [loc('route.py', 12, 14, 'sink')], duplicateOf=A)
    reduced, pairs = pairs_of([first, second, third])
    assert reduced['absorbed'] == [] and pairs == [[A, B], [A, C], [B, C]]


def test_a_rejected_candidate_is_not_absorbed_through_duplicate_of():
    rows = pair_rows(duplicateOf=A)
    rows[1]['evidenceState'] = 'rejected'
    reduced, pairs = pairs_of(rows)
    assert pairs == [] and reduced['absorbed'] == []
    assert [r['candidateId'] for r in reduced['rejected']] == [B]


def _checked(*rows):
    return {'retained': list(rows), 'rejected': []}


def test_open_pairs_are_sent_to_validators_before_chains():
    rows = pair_rows()
    pairs = possible_duplicates(rows)
    result = derive(attempts=ACCEPTED, reduced=_checked(*rows), duplicates=pairs)
    assert result['step'] == 'validate' and result['total'] == result['actionable'] == 1
    assert result['items'] == [{'kind': 'duplicate', 'candidateIds': [A, B], 'path': 'route.py', 'lines': [12, 14],
                                'titles': ['Title ' + A, 'Title ' + B], 'state': 'pending'}]
    assert '1 possible duplicate pair' in result['reason']
    assert 'provenance.duplicateOf' in result['how'] and 'provenance.distinctFrom' in result['how']
    assert result['attemptId'].startswith('att_') and result['slots'] == 6
    # Unchecked candidates still come first, with their own items and instructions.
    unchecked = derive(attempts=ACCEPTED[:2], reduced=_checked(*rows), duplicates=pairs)
    assert unchecked['step'] == 'validate' and [i['candidateId'] for i in unchecked['items']] == [A, B]
    assert 'duplicateOf' not in unchecked['how']


def test_a_running_validator_that_names_both_ids_means_wait():
    rows = pair_rows()
    pairs = possible_duplicates(rows)
    both = derive(attempts=ACCEPTED, reduced=_checked(*rows), duplicates=pairs,
                  dispatches=[dispatch('att_d', candidates=[A, B], role='validator')])
    assert both['step'] == 'wait' and both['actionable'] == 0 and both['items'][0]['state'] == 'running'
    one = derive(attempts=ACCEPTED, reduced=_checked(*rows), duplicates=pairs,
                 dispatches=[dispatch('att_d', candidates=[A], role='validator')])
    assert one['step'] == 'validate' and one['items'][0]['state'] == 'pending'
    unnamed = derive(attempts=ACCEPTED, reduced=_checked(*rows), duplicates=pairs,
                     dispatches=[dispatch('att_d', role='validator')])
    assert unnamed['step'] == 'wait'


def test_a_pending_pair_is_listed_before_a_running_one():
    rows = pair_rows() + [row(C, [loc('route.py', 13, 13, 'sink')])]
    result = derive(attempts=ACCEPTED, reduced=_checked(*rows), duplicates=possible_duplicates(rows),
                    dispatches=[dispatch('att_d', candidates=[A, B], role='validator')])
    assert result['step'] == 'validate' and result['total'] == 3 and result['actionable'] == 2
    assert [(i['candidateIds'], i['state']) for i in result['items']] == [
        ([A, C], 'pending'), ([B, C], 'pending'), ([A, B], 'running')]


def test_without_open_pairs_the_scan_goes_to_chains_then_finalize():
    rows = pair_rows(distinctFrom=[A])
    reduced, pairs = pairs_of(rows)
    assert pairs == []
    for kwargs in ({}, {'duplicates': []}):
        assert derive(attempts=ACCEPTED, reduced=reduced, **kwargs)['step'] == 'chains'
        assert derive(attempts=ACCEPTED, reduced=reduced, chains_recorded=True, **kwargs)['step'] == 'finalize'


def test_the_worker_schema_accepts_both_verdict_fields():
    payload = worker()
    payload['candidates'][0].setdefault('provenance', {}).update(duplicateOf=A, distinctFrom=[B, C])
    assert validate_worker_result(payload) == []


@pytest.mark.parametrize('field,value,path', [
    ('duplicateOf', 'cand_nope', '$.candidates[0].provenance.duplicateOf'),
    ('duplicateOf', [A], '$.candidates[0].provenance.duplicateOf'),
    ('distinctFrom', ['cand_nope'], '$.candidates[0].provenance.distinctFrom[0]'),
    ('distinctFrom', A, '$.candidates[0].provenance.distinctFrom'),
    ('distinctFrom', [A] * 201, '$.candidates[0].provenance.distinctFrom')])
def test_a_malformed_verdict_is_refused_with_an_echoable_path(field, value, path):
    payload = worker()
    payload['candidates'][0].setdefault('provenance', {})[field] = value
    problems = public_problems(validate_worker_result(payload))
    assert problems and all(p.startswith(path + ':') for p in problems)
