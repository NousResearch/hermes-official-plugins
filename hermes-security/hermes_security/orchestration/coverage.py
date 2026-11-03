"""Coverage closure is independent of the number of findings."""
from ..standards import ledger
from . import standard

# Intentional or harmless skips: out of scope, dependencies, links (their in-repo
# targets are scanned directly), submodules, special files and oversized/generated
# files. Files that could not be read or changed mid-read remain gaps.
BENIGN_EXCLUSIONS = {'out_of_scope', 'git_metadata', 'vendored', 'symlink', 'submodule', 'not_regular', 'too_large'}


def build_coverage(scan, inventory, units, attempts, packets, findings):
    by_id = {}
    for unit in units:
        ident = unit.get('unitId', unit.get('unit'))
        reason = unit.get('reason', unit.get('note', 'explicit review disposition'))
        closed = unit.get('closedBy') or []
        if len(closed) > 1:
            # Lanes are closed by many workers; the latest note alone reads as the whole story.
            names = ', '.join(closed[:5]) + (', ...' if len(closed) > 5 else '')
            reason = f'Closed by {len(closed)} reviews ({names}); latest note: {reason}'
        by_id[ident] = {'unitId': ident, 'kind': ident.split(':', 1)[0], 'state': unit['state'],
                        'reason': reason,
                        'evidence': unit.get('evidence', [])}
    review_paths = {f['path'] for f in inventory}
    diff = scan['options'].get('diff', {})
    review_paths.update(diff.get('deleted', []))
    review_paths.update(diff.get('changed', []))
    review_paths.update(diff.get('added', []))
    # A focused standard scan leaves some files out by design: they are listed exclusions,
    # not unfinished work, unless a worker read one fully anyway. A worker's explicit
    # "cannot analyze this" (unsupported, failed) stays visible as a gap.
    planner = scan['options'].get('planner', 1) if scan.get('mode') == 'standard' else 0
    planned, frozen = planner >= 2, scan['options'].get('standardAreas')
    by_design = []
    for path, reason in sorted(standard.plan(inventory, planner, frozen)['skipped'].items()) if planned else ():
        if by_id.get('file:' + path, {}).get('state') in {None, 'deferred', 'unknown'}:
            review_paths.discard(path)
            by_id.pop('file:' + path, None)
            by_design.append({'path': path, 'reason': reason})
    for path in sorted(review_paths):
        ident = 'file:' + path
        by_id.setdefault(ident, {'unitId': ident, 'kind': 'file', 'state': 'unknown', 'reason': 'file has no accepted review', 'evidence': []})
    standards = ledger.build_ledger(scan['options']['surfaces'], list(by_id.values()), findings)
    gaps = ledger.mandatory_gaps(standards)
    done = {'reviewed', 'not_applicable'}
    files = [by_id['file:' + p] for p in sorted(review_paths)]
    for unit in by_id.values():
        if unit['state'] not in done:
            gaps.append(unit['unitId'] + ' is ' + unit['state'] + ': ' + unit['reason'])
    workers = [{'attemptId': a['attempt_id'], 'role': a['role'], 'status': a['status'] if a['status'] in {'accepted', 'rejected', 'late'} else 'missing'} for a in attempts]
    accepted = {a['packet_id'] for a in attempts if a['status'] == 'accepted'}
    for packet in packets:
        if packet['packetId'] not in accepted:
            workers.append({'attemptId': packet.get('attemptId', 'att_missing_' + packet['packetId'][4:]), 'role': packet['role'], 'status': 'missing'})
            gaps.append('Worker packet ' + packet['packetId'] + ' has no accepted result.')
    if scan['options'].get('truncated'):
        gaps.append('Inventory was truncated.')
    for excluded in scan['options'].get('excluded', []):
        # Listed in the report either way; only files we failed to read are gaps.
        if excluded['reason'] not in BENIGN_EXCLUSIONS:
            gaps.append('Excluded ' + excluded['path'] + ': ' + excluded['reason'])
    result = {'documentType': 'hermes-security.coverage', 'schemaVersion': '1.0', 'scanId': scan['scan_id'],
              'completeness': 'partial' if gaps else 'complete',
              'files': {'total': len(files), 'reviewed': sum(f['state'] == 'reviewed' for f in files),
                        'notApplicable': sum(f['state'] == 'not_applicable' for f in files),
                        'deferred': sum(f['state'] == 'deferred' for f in files), 'unsupported': sum(f['state'] == 'unsupported' for f in files),
                        'excluded': scan['options'].get('excluded', []) + by_design},
              'units': list(by_id.values()), 'standards': standards, 'workers': workers, 'gaps': list(dict.fromkeys(gaps))}
    if planned:
        result['standardPlan'] = standard.summary(inventory, planner, frozen)
    return result
