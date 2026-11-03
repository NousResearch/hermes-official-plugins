"""Stdlib offline release canaries and externally supplied scan-bundle scoring.

Nothing in this module generates model findings. A perfect canary result is not
an estimate of discovery quality. Gold matching is explicit rule+semantic anchor.
"""
from collections import Counter
from copy import deepcopy
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
BANDS = ('informational', 'low', 'medium', 'high', 'critical')
THRESHOLDS = {'precision': .95, 'recall': .85, 'highCriticalRecall': .95,
              'severityExact': .85, 'severityWithinOne': .95, 'highCriticalPrecision': .95}


def load_gold(path=None):
    """Validate the versioned, unique, location-grounded synthetic gold corpus."""
    doc = json.loads(Path(path or ROOT / 'evals/discovery/gold.json').read_text())
    if doc.get('schemaVersion') != '1.0' or not isinstance(doc.get('cases'), list) or not doc['cases']:
        raise ValueError('gold needs schemaVersion 1.0 and nonempty cases')
    ids = set()
    for case in doc['cases']:
        if not isinstance(case, dict) or not all(isinstance(case.get(k), str) and case[k] for k in ('caseId', 'family', 'fixture')):
            raise ValueError('case identity/family/fixture missing')
        if case['caseId'] in ids or case.get('variant') not in ('vulnerable', 'fixed'):
            raise ValueError('duplicate case or invalid variant')
        ids.add(case['caseId'])
        fixture = (ROOT / case['fixture']).resolve()
        if not fixture.is_relative_to(ROOT / 'tests/fixtures') or not fixture.is_dir():
            raise ValueError('fixture must exist inside tests/fixtures')
        roots = case.get('roots')
        if not isinstance(roots, list) or bool(roots) != (case['variant'] == 'vulnerable'):
            raise ValueError('vulnerable requires roots; fixed requires empty roots')
        seen = set()
        for r in roots:
            if not all(isinstance(r.get(k), str) and r[k] for k in ('rootCauseId', 'ruleId', 'anchor', 'description')) or r.get('severity') not in BANDS:
                raise ValueError('invalid gold root')
            key = (r['ruleId'], r['anchor'])
            if key in seen: raise ValueError('duplicate root')
            seen.add(key)
            loc = r.get('location', {})
            p = (fixture / loc.get('path', '')).resolve()
            if not p.is_relative_to(fixture) or not p.is_file(): raise ValueError('invalid gold location')
            if not (type(loc.get('startLine')) is int and type(loc.get('endLine')) is int and 1 <= loc['startLine'] <= loc['endLine'] <= len(p.read_text().splitlines())):
                raise ValueError('invalid gold line range')
    return doc['cases']


def jaccard(left, right):
    """Empty/empty sets agree; missing runs are handled separately as failures."""
    return len(left & right) / len(left | right) if left | right else 1.0


def score_records(gold, records):
    """One record per case, one-to-one exact gold matching; duplicates are FPs.

    Missing/error/abstention/deferred/unsupported cases keep their gold roots in
    all recall and severity denominators. Precision has its conventional reported
    findings denominator (zero reports => null, never an automatic pass).
    """
    by_case = {}
    expected_ids = {c['caseId'] for c in gold}
    for record in records:
        cid = record['caseId']
        if cid not in expected_ids or cid in by_case: raise ValueError('unknown/duplicate case record')
        if record.get('status') not in ('ok', 'abstained', 'error', 'deferred', 'unsupported'):
            raise ValueError('invalid record status')
        by_case[cid] = record
    counts = Counter()
    per_case = []
    for case in gold:
        record = by_case.get(case['caseId'], {'status': 'missing', 'findings': []})
        counts[record['status']] += 1
        roots = {(r['ruleId'], r['anchor']): r for r in case['roots']}
        counts['goldRoots'] += len(roots)
        counts['goldHighCritical'] += sum(r['severity'] in ('high', 'critical') for r in roots.values())
        matched = set()
        predicted = set()
        rows = record.get('findings', [])
        if not isinstance(rows, list): raise ValueError('findings must be list')
        # Partial/error scans may still report findings; never erase their FPs.
        for finding in rows:
            key = (finding['ruleId'], finding['identity']['anchor'])
            band = finding['severity']['level']
            if band not in BANDS: raise ValueError('invalid severity')
            predicted.add(key)
            high = band in ('high', 'critical')
            counts['reported'] += 1
            counts['reportedHighCritical'] += high
            if key in roots and key not in matched:
                matched.add(key); counts['truePositives'] += 1
                want = roots[key]['severity']
                counts['highCriticalHits'] += want in ('high', 'critical')
                counts['highCriticalCorrect'] += high and want in ('high', 'critical')
                counts['exact'] += band == want
                counts['withinOne'] += abs(BANDS.index(band) - BANDS.index(want)) <= 1
            else:
                counts['falsePositives'] += 1
        counts['falseNegatives'] += len(roots) - len(matched)
        counts['retained'] += record.get('retained', 0)
        per_case.append({'caseId': case['caseId'], 'status': record['status'],
                         'matchedRoots': sorted(roots[k]['rootCauseId'] for k in matched),
                         'missedRoots': sorted(roots[k]['rootCauseId'] for k in roots.keys() - matched),
                         'predictionSet': [list(k) for k in sorted(predicted)],
                         'correctVerdict': record['status'] == 'ok' and predicted == set(roots)})
    def ratio(num, den): return counts[num] / counts[den] if counts[den] else None
    result = {k: counts[k] for k in ('goldRoots', 'goldHighCritical', 'reported', 'truePositives', 'falsePositives', 'falseNegatives', 'retained')}
    result.update(precision=ratio('truePositives', 'reported'), recall=ratio('truePositives', 'goldRoots'),
                  highCriticalRecall=ratio('highCriticalHits', 'goldHighCritical'),
                  severityExact=ratio('exact', 'goldRoots'), severityWithinOne=ratio('withinOne', 'goldRoots'),
                  highCriticalPrecision=ratio('highCriticalCorrect', 'reportedHighCritical'),
                  abstentions=counts['abstained'], errors=counts['error'], deferred=counts['deferred'],
                  unsupported=counts['unsupported'], missing=counts['missing'], cases=per_case)
    return result


def gate(name, threshold, value, passed, **details):
    return {'gate': name, 'threshold': threshold, 'measured': value,
            'status': 'PASS' if passed else 'FAIL', **details}


def _pytest_gate(name, selectors):
    # Running the eval tests here would recurse. The full suite is run separately.
    with tempfile.TemporaryDirectory(prefix='security-contract-') as temp:
        report = Path(temp) / 'result.xml'
        cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
               '--ignore=tests/unit/test_run_evals.py', '--junitxml=' + str(report), *selectors]
        run = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
        counts = Counter()
        if report.exists():
            xml = ET.parse(report).getroot()
            for suite in xml.iter('testsuite'):
                for k in ('tests', 'failures', 'errors', 'skipped'): counts[k] += int(suite.get(k, 0))
            counts['expectedFailures'] = sum(node.get('type') == 'pytest.xfail' for node in xml.iter('skipped'))
        applicable = counts['tests'] - counts['skipped']
        passed = applicable - counts['failures'] - counts['errors']
        rate = passed / applicable if applicable else 0
        return gate(name, '100% executed non-skipped tests', rate,
                    run.returncode == 0 and applicable > 0 and rate == 1,
                    tests=dict(counts), command=cmd, output=run.stdout.strip() + run.stderr.strip())


def deterministic(contract=True):
    """Run real service tests and existing chain evaluator, no model simulation."""
    gates = []
    if contract:
        gates.append(_pytest_gate('deterministic-contract-tests', ['tests']))
    groups = {
        'citation-resolution-and-forgery-rejection': ['tests/integration/test_end_to_end_static.py::test_end_to_end_static', 'tests/integration/test_service_security.py::test_reject_candidate_citations_claims_and_secrets'],
        'zero-fabricated-runtime-claims': ['tests/integration/test_service_security.py::test_runtime_receipt_cannot_be_forged_by_worker_or_tool'],
        'zero-cross-scan-contamination': ['tests/integration/test_service_security.py::test_packet_scan_and_attempt_binding', 'tests/integration/test_profile_isolation.py', 'tests/unit/test_schemas.py::test_cross_scan_and_reference_checks'],
        'no-hidden-truncation-input-canaries': ['tests/unit/test_schemas.py::test_nested_limits_and_unknowns', 'tests/unit/test_target_hostile.py::test_hardlinks_classification_and_limits'],
        'no-false-clean-missing-sections': ['tests/integration/test_service_modes.py::test_missing_sections_retained', 'tests/unit/test_standards_mapping.py'],
    }
    for name, selectors in groups.items(): gates.append(_pytest_gate(name, selectors))
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/eval_chains.py')], text=True, capture_output=True)
    try: chains = json.loads(run.stdout)
    except ValueError: chains = {'error': run.stderr or run.stdout}
    for key, threshold in [('precision', .95), ('recall', .85), ('inventedEdgeCount', 0)]:
        v = chains.get(key)
        gates.append(gate('chain-' + key, threshold, v, run.returncode == 0 and v is not None and (v == 0 if threshold == 0 else v >= threshold), evidence=chains))
    gates.extend(_coverage_canaries())
    gates.extend(_reducer_canaries())
    gates.append(_severity_canaries())
    return {'mode': 'deterministic', 'gates': gates, 'notRun': model_not_run()}


def _coverage_canaries():
    from hermes_security.orchestration.coverage import build_coverage
    from hermes_security.standards.ledger import load_top10
    scan = {'scan_id': 'scan_eval', 'options': {'surfaces': []}}
    units = [{'unit': 'lane:' + c['id'], 'state': 'not_applicable', 'note': 'empty synthetic scope'} for c in load_top10()['categories']]
    baseline = build_coverage(scan, [], units, [], [], [])
    truncated = deepcopy(scan); truncated['options']['truncated'] = True
    coverage = build_coverage(truncated, [], units, [], [], [])
    hidden = int(coverage['completeness'] != 'partial' or 'Inventory was truncated.' not in coverage['gaps'])
    return [gate('complete-baseline-is-clean', 0, int(baseline['completeness'] != 'complete'), baseline['completeness'] == 'complete'),
            gate('zero-hidden-inventory-truncation', 0, hidden, hidden == 0)]


def _reducer_canaries():
    from hermes_security.orchestration.reducer import reduce_candidates
    cases = json.loads((ROOT / 'evals/dedup/cases.json').read_text())['cases']
    conserved = invalid = edges = 0
    details = []
    for case in cases:
        result = reduce_candidates(case['candidates'])
        conserved += not result['problems']
        allowed = {tuple(e) for e in case['allowedAbsorptions']}
        actual = {(r['candidateId'], r['into']) for r in result['absorbed']}
        invalid += len(actual - allowed); edges += len(actual)
        details.append({'case': case['id'], 'problems': result['problems'], 'unexpected': sorted(actual - allowed)})
    return [gate('dedup-source-conservation', 1.0, conserved / len(cases), conserved == len(cases), cases=details),
            gate('dedup-invalid-merges', 0, invalid, invalid == 0, observedMerges=edges)]


def _severity_canaries():
    from hermes_security.chains import build_chains_doc
    cases = json.loads((ROOT / 'evals/severity/cases.json').read_text())['cases']
    matched = total = 0
    for case in cases:
        source = json.loads((ROOT / 'evals/chains' / case['chainCase']).read_text())
        doc = build_chains_doc(source['scanId'], source['findings'], snapshot_digest=source['snapshotDigest'])
        for want in case['expected']:
            total += 1
            matched += any(r['findingIds'] == want['findingIds'] and r['severity']['level'] == want['level'] for r in doc['chains'])
    return gate('code-computed-chain-severity', 1.0, matched / total if total else None, total > 0 and matched == total, cases=total)


def model_not_run():
    return [{'gate': name, 'status': 'NOT RUN', 'command': command} for name, command in [
        ('blind-discovery-and-model-severity', 'python scripts/run-evals.py --score-bundles /path/to/sealed-bundles'),
        ('five-run-provider-agreement', 'python scripts/run-evals.py --score-bundles /path/to/five-run-bundles'),
        ('paired-upstream-comparison', 'python scripts/compare-upstream.py --pairs /path/to/paired-bundles/pairs.json')]]


def read_hermes_bundle(path):
    from hermes_security.reports import verify_bundle
    checked = verify_bundle(path)
    if not checked['ok']: raise ValueError('bundle verification: ' + '; '.join(checked['problems']))
    return {name: json.loads((Path(path) / name).read_text()) for name in ('scan-manifest.json', 'findings.json')}


def validate_case_target(manifest, case):
    """Bind gold to an isolated full-directory fixture, not an arbitrary scan."""
    from hermes_security.canonical import sha256_hex
    from hermes_security.target.snapshot import snapshot_digest
    fixture = ROOT / case['fixture']
    inventory = {'files': [{'path': p.relative_to(fixture).as_posix(), 'sha256': sha256_hex(p.read_bytes())}
                           for p in sorted(fixture.rglob('*')) if p.is_file()], 'excluded': [], 'truncated': False}
    target = manifest['target']
    if target.get('revision') or target.get('base') or target.get('head') or target.get('scope'):
        raise ValueError('eval scans must use isolated full-directory fixture copies, no git revision/scope')
    if target['snapshotDigest'] != snapshot_digest(inventory, target):
        raise ValueError('sealed snapshot does not match gold fixture')


def score_bundle_directory(directory, gold=None):
    """Index maps case IDs to sealed bundles; absent cases remain missing.

    index.json: {"runs":[{"runId":"1","provider":"...","model":"...",
      "records":[{"caseId":"...","status":"ok","bundle":"relative/path"}]}]}
    Explicit non-ok records may omit bundle. All supplied bundles are verified.
    """
    gold = gold or load_gold()
    directory = Path(directory).resolve()
    index = json.loads((directory / 'index.json').read_text())
    runs = index.get('runs', [])
    if not runs: return {'mode': 'score-bundles', 'gates': [], 'notRun': model_not_run()}
    results = []; gates = []; ids = set(); scan_ids = set()
    cases_by_id = {c['caseId']: c for c in gold}
    for run in runs:
        rid = run['runId']
        if rid in ids or not run.get('provider') or not run.get('model'): raise ValueError('unique runId/provider/model required')
        ids.add(rid); records = []
        for raw in run['records']:
            row = dict(raw, findings=[], retained=0)
            if raw.get('bundle'):
                path = (directory / raw['bundle']).resolve()
                if not path.is_relative_to(directory): raise ValueError('bundle path escapes input directory')
                try:
                    bundle = read_hermes_bundle(path)
                    manifest = bundle['scan-manifest.json']
                    validate_case_target(manifest, cases_by_id[raw['caseId']])
                    if manifest['scanId'] in scan_ids: raise ValueError('reused scan is not an independent run')
                    scan_ids.add(manifest['scanId'])
                    if manifest['runtime'].get('provider') != run['provider'] or manifest['runtime'].get('model') != run['model']:
                        raise ValueError('provider/model does not match sealed manifest')
                    row['findings'] = bundle['findings.json']['findings']
                    row['retained'] = len(bundle['findings.json'].get('retained', []))
                    if manifest['status'] != 'completed' and row['status'] == 'ok': row['status'] = 'deferred'
                except Exception as exc:
                    row['status'] = 'error'; row['error'] = str(exc)
            elif row['status'] == 'ok':
                row['status'] = 'error'; row['error'] = 'ok record missing sealed bundle'
            records.append(row)
        score = score_records(gold, records)
        results.append({'runId': rid, 'provider': run['provider'], 'model': run['model'], 'score': score,
                        'inputErrors': [{'caseId': r['caseId'], 'error': r['error']} for r in records if 'error' in r]})
        incomplete = sum(score[k] for k in ('abstentions', 'errors', 'deferred', 'unsupported', 'missing'))
        gates.append(gate(rid + ':completed-case-coverage', 1.0, (len(gold) - incomplete) / len(gold), incomplete == 0))
        for name, threshold in THRESHOLDS.items():
            v = score[name]; gates.append(gate(rid + ':' + name, threshold, v, v is not None and v >= threshold))
    not_run = []
    groups = {}
    for r in results: groups.setdefault((r['provider'], r['model']), []).append(r)
    for key, group in groups.items():
        if len(group) != 5:
            not_run.append({'gate': 'five-run-agreement:' + '/'.join(key), 'status': 'NOT RUN', 'reason': 'exactly five independent runs required', 'command': model_not_run()[1]['command']}); continue
        verdicts = []; sets = []
        for rows in zip(*(r['score']['cases'] for r in group)):
            available = all(r['status'] == 'ok' for r in rows)
            predictions = [{tuple(k) for k in r['predictionSet']} for r in rows]
            # Verdict agreement is positive vs negative, not correctness.
            verdicts.append(float(available and len({bool(p) for p in predictions}) == 1))
            sets.append(sum(jaccard(a,b) for a,b in itertools.combinations(predictions,2)) / 10 if available else 0)
        for name, values, threshold in [('verdict-agreement', verdicts, .95), ('root-set-jaccard', sets, .90)]:
            value = sum(values) / len(gold)
            gates.append(gate('/'.join(key) + ':' + name, threshold, value, value >= threshold))
    return {'mode': 'score-bundles', 'runs': results, 'gates': gates, 'notRun': not_run}


def print_result(result):
    print(json.dumps(result, indent=2, sort_keys=True))
    print('\nGate | Threshold | Measured | Status\n--- | --- | --- | ---')
    for g in result.get('gates', []): print(f"{g['gate']} | {g['threshold']} | {g['measured']} | {g['status']}")
    for g in result.get('notRun', []): print(f"{g['gate']} | — | — | NOT RUN: {g.get('command', '')}")
    return int(any(g['status'] == 'FAIL' for g in result.get('gates', [])))
