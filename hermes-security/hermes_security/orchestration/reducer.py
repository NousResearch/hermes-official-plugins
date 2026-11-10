"""Conservative root-cause deduplication with auditable source conservation."""
from collections import Counter
from copy import deepcopy

LIVE = {'candidate', 'source_supported', 'runtime_confirmed'}
DEFINING_ROLES = {'root_control', 'sink'}   # with the first location, the spans that define a bug


def reduce_candidates(candidates):
    # Candidate identities are plugin-computed. Distinct attempts of one identity
    # are repeated observations, not separate source identities. Callers pass rows
    # oldest first, so on an equal state the later review (a validator) wins.
    priority = {'runtime_confirmed': 5, 'source_supported': 4, 'rejected': 3, 'inconclusive': 2, 'candidate': 1}
    unique, same, distinct = {}, {}, set()
    for raw in candidates:
        cid = raw['candidateId']
        old = unique.get(cid)
        # Validator verdicts outlive the record that carried them: a later record without the
        # field does not erase one. The latest duplicateOf wins; distinctFrom is a union.
        verdict = raw.get('provenance', {})
        if verdict.get('duplicateOf'):
            same[cid] = verdict['duplicateOf']
        distinct.update(tuple(sorted((cid, other))) for other in verdict.get('distinctFrom') or [] if other != cid)
        if old is None or priority.get(raw.get('evidenceState'), 0) >= priority.get(old.get('evidenceState'), 0):
            unique[cid] = deepcopy(raw)
        if old:
            unique[cid].setdefault('provenance', {})['workerAttemptIds'] = sorted(set(old.get('provenance', {}).get('workerAttemptIds', []) + raw.get('provenance', {}).get('workerAttemptIds', [])))
    kept, absorbed, rejected = [], [], []
    for cid, row in sorted(unique.items(), key=lambda pair: (-priority.get(pair[1].get('evidenceState'), 0), pair[0])):
        if row.get('evidenceState') == 'rejected':
            rejected.append(row)
            continue
        key = (row['ruleId'], row['identity']['anchor'])
        winner = next((k for k in kept if (k['ruleId'], k['identity']['anchor']) == key), None)
        if winner is None:
            kept.append(row)
        else:
            absorbed.append({'candidateId': cid, 'into': winner['candidateId'], 'reason': 'same ruleId and anchor'})
    # A subsumption statement names every absorbed source; CWE alone is never a key.
    for winner in list(kept):
        if winner not in kept:
            continue
        declared = winner.get('provenance', {}).get('subsumes', [])
        for row in list(kept) + list(rejected):
            if row is not winner and row['candidateId'] in declared:
                (kept if row in kept else rejected).remove(row)
                for edge in absorbed:
                    if edge['into'] == row['candidateId']:
                        edge['into'] = winner['candidateId']
                absorbed.append({'candidateId': row['candidateId'], 'into': winner['candidateId'], 'reason': 'explicit remediation subsumption'})
    # A duplicateOf verdict absorbs its candidate into the survivor it ends at, as if that survivor
    # had subsumed it. Every target is resolved before anything moves, so chains do not depend on order.
    for row, into in [(row, _survivor(row['candidateId'], same, kept, absorbed)) for row in kept]:
        if into:
            kept.remove(row)
            for edge in absorbed:
                if edge['into'] == row['candidateId']:
                    edge['into'] = into
            absorbed.append({'candidateId': row['candidateId'], 'into': into, 'reason': 'validator duplicate verdict'})
    for row in kept:
        ids = sorted(e['candidateId'] for e in absorbed if e['into'] == row['candidateId'])
        provenance = row.setdefault('provenance', {})
        provenance['sourceCandidateIds'] = sorted(set([row['candidateId'], *ids, *provenance.get('sourceCandidateIds', [])]))
        provenance['supersedes'] = ids or None
        provenance['workerAttemptIds'] = sorted({aid for cid in [row['candidateId'], *ids] for aid in unique[cid].get('provenance', {}).get('workerAttemptIds', [])})
        provenance.pop('subsumes', None)
        provenance.pop('duplicateOf', None)
        provenance.pop('distinctFrom', None)
    # The verdict fields never reach a sealed finding, so the pairs judged different are returned here.
    result = {'retained': kept, 'absorbed': absorbed, 'rejected': rejected, 'distinct': sorted(list(pair) for pair in distinct)}
    result['problems'] = conservation_check(candidates, result)
    return result


def _survivor(cid, same, kept, absorbed):
    """The retained live candidate a duplicateOf verdict ends at, or None when it must be ignored.

    Ignored: it names itself, an unknown or rejected candidate, runs in a cycle, or ends at a
    candidate that is not live. A target already merged elsewhere is followed to where it went.
    """
    rows = {row['candidateId']: row for row in kept}
    merged = {edge['candidateId']: edge['into'] for edge in absorbed}
    seen, current = {cid}, cid
    while True:
        following = merged.get(current) or (same.get(current) if current in rows else None)
        if following is None or following == current or not (following in rows or following in merged):
            break   # nothing further to follow: a self, unknown or rejected target counts as no verdict
        if following in seen:
            return None
        seen.add(following)
        current = following
    if current == cid or rows.get(current, {}).get('evidenceState') not in LIVE:
        return None
    return current


def _span(location):
    start = location.get('startLine') or 0
    return start, location.get('endLine') or start


def _spans(row):
    """The locations that define the bug: the first one, plus every root control and sink."""
    return [(loc.get('path'), *_span(loc)) for i, loc in enumerate(row['locations']) if i == 0 or loc.get('role') in DEFINING_ROLES]


def possible_duplicates(rows, distinct=()):
    """Pairs of live candidates whose bug-defining locations overlap in one file.

    Workers often lead with different locations for one bug, so any overlap between the two
    candidates' defining spans pairs them; other roles (callers, entry points) overlap too often
    to mean anything. One entry per pair, reporting its overlap with the lowest path, then the
    lowest line range. Pairs a validator judged different (``distinct`` from reduce_candidates,
    or provenance.distinctFrom still on a row) are left out.

    Different rule IDs or anchors are never merged here: a validator decides, and records the
    verdict with provenance.duplicateOf, provenance.subsumes or provenance.distinctFrom.
    """
    live = [r for r in rows if r.get('evidenceState') in LIVE and r.get('locations')]
    # Sealed findings carry findingId; unsealed rows carry candidateId.
    ids = [r.get('candidateId') or r.get('findingId') for r in live]
    judged = {tuple(pair) for pair in distinct}
    judged.update(tuple(sorted((cid, other))) for cid, r in zip(ids, live) for other in r.get('provenance', {}).get('distinctFrom') or [])
    pairs = []
    for i, a in enumerate(live):
        for j, b in enumerate(live[i + 1:], i + 1):
            if tuple(sorted((ids[i], ids[j]))) in judged:
                continue
            overlaps = [(pa, [max(sa, sb), min(ea, eb)]) for pa, sa, ea in _spans(a) for pb, sb, eb in _spans(b)
                        if pa == pb and sa <= eb and sb <= ea]
            if overlaps:
                path, lines = min(overlaps, key=lambda o: (o[0] or '', o[1]))
                pairs.append({'candidateIds': sorted([ids[i], ids[j]]), 'path': path, 'lines': lines})
    return sorted(pairs, key=lambda p: (p['path'], p['lines'], p['candidateIds']))


def conservation_check(sources, result):
    expected = {c['candidateId'] for c in sources}
    seen = Counter([c['candidateId'] for c in result['retained']] + [c['candidateId'] for c in result['absorbed']] + [c['candidateId'] for c in result['rejected']])
    problems = []
    if set(seen) != expected:
        problems.append('source candidate set changed')
    if any(n != 1 for n in seen.values()):
        problems.append('source candidate accounted for more than once')
    survivors = {c['candidateId'] for c in result['retained']}
    if any(c['into'] not in survivors for c in result['absorbed']):
        problems.append('absorption target is not retained')
    return problems
