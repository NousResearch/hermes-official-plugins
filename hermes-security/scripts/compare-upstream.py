#!/usr/bin/env python3
"""Paired completed-scan comparison; no bundles means NOT RUN, never a score."""
import argparse
import json
import math
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.harness import load_gold, score_records, read_hermes_bundle, validate_case_target, gate, print_result
from hermes_security.compat.codex_import import import_codex_bundle


def compare(path):
    if path is None or not path.exists():
        return {'gates': [], 'notRun': [{'gate': 'paired-upstream-comparison', 'status': 'NOT RUN',
            'command': 'python scripts/compare-upstream.py --pairs /path/to/paired-bundles/pairs.json',
            'reason': 'No paired completed scan bundles supplied.'}]}
    doc = json.loads(path.read_text())
    pairs = doc.get('pairs', [])
    if not pairs: return compare(None)
    gold = load_gold(); by_id = {r['caseId']: r for r in gold}
    sides = {'upstream': [], 'hermes': []}; errors = []; seen = set()
    for pair in pairs:
        cid = pair['caseId']
        if cid not in by_id or cid in seen: raise ValueError('unknown/duplicate paired case')
        seen.add(cid)
        for side in sides:
            try:
                directory = (path.parent / pair[side]).resolve()
                if not directory.is_relative_to(path.parent.resolve()): raise ValueError('paired path escapes directory')
                if side == 'upstream':
                    original = json.loads((directory / 'scan-manifest.json').read_text())
                    if original.get('scan', {}).get('status') != 'completed':
                        raise ValueError('upstream must be a completed v1 scan')
                    bundle = import_codex_bundle(directory)
                else:
                    bundle = read_hermes_bundle(directory)
                    if bundle['scan-manifest.json']['status'] != 'completed': raise ValueError('Hermes scan is not completed')
                validate_case_target(bundle['scan-manifest.json'], by_id[cid])
                findings = bundle['findings.json']
                sides[side].append({'caseId': cid, 'status': 'ok', 'findings': findings['findings'], 'retained': len(findings.get('retained', []))})
            except Exception as exc:
                errors.append({'caseId': cid, 'side': side, 'error': str(exc)})
                sides[side].append({'caseId': cid, 'status': 'error', 'findings': []})
    scores = {side: score_records(gold, rows) for side, rows in sides.items()}
    # Independent unit is a repository case (not individual findings). A one-sided
    # Hoeffding 95% bound for paired correctness differences in [-1,1] is
    # deliberately conservative and distribution-free. Small corpora cannot
    # establish noninferiority; report the failing bound rather than fake parity.
    deltas = [int(h['correctVerdict']) - int(u['correctVerdict'])
              for h,u in zip(scores['hermes']['cases'], scores['upstream']['cases'])]
    mean = sum(deltas) / len(gold)
    lower = max(-1.0, mean - math.sqrt(2 * math.log(20) / len(gold)))
    new_misses = []
    for h,u,case in zip(scores['hermes']['cases'], scores['upstream']['cases'], gold):
        high = {r['rootCauseId'] for r in case['roots'] if r['severity'] in ('high','critical')}
        new_misses.extend(case['caseId'] + ':' + root for root in (set(u['matchedRoots']) - set(h['matchedRoots'])) & high)
    return {'mode': 'paired-comparison', 'scores': scores, 'errors': errors,
            'pairedCases': len(pairs), 'expectedCases': len(gold), 'meanCorrectVerdictDelta': mean,
            'confidenceMethod': 'one-sided 95% Hoeffding; repository cases; range [-1,1]',
            'gates': [gate('all-comparable-pairs-present', len(gold), len(pairs), len(pairs) == len(gold) and not errors),
                      gate('paired-lower-confidence-bound', '> -0.02', lower, lower > -.02 and not errors and len(pairs) == len(gold)),
                      gate('no-new-high-critical-miss', 0, len(new_misses), not new_misses and not errors, misses=new_misses)]}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--pairs', type=Path)
    args = p.parse_args(argv)
    try: result = compare(args.pairs)
    except Exception as exc: result = {'gates': [gate('paired-input', 'valid', str(exc), False)]}
    return print_result(result)


if __name__ == '__main__':
    raise SystemExit(main())
