"""Offline scoring must retain missing/error runs in recall denominators."""
from copy import deepcopy
import pytest
from evals.harness import load_gold, score_records, jaccard, deterministic


def test_scorer_denominators():
    gold = [{'caseId': 'a', 'roots': [{'rootCauseId': 'r', 'ruleId': 'x', 'anchor': 'a/f', 'severity': 'high'}]},
            {'caseId': 'b', 'roots': [{'rootCauseId': 's', 'ruleId': 'y', 'anchor': 'b/f', 'severity': 'critical'}]},
            {'caseId': 'fixed', 'roots': []}]
    records = [{'caseId': 'a', 'status': 'ok', 'findings': [{'ruleId': 'x', 'identity': {'anchor': 'a/f'}, 'severity': {'level': 'medium'}}]},
               {'caseId': 'b', 'status': 'abstained', 'findings': []},
               {'caseId': 'fixed', 'status': 'ok', 'findings': [{'ruleId': 'z', 'identity': {'anchor': 'z'}, 'severity': {'level': 'high'}}]}]
    result = score_records(gold, records)
    assert result['precision'] == .5 and result['recall'] == .5
    assert result['highCriticalRecall'] == .5
    assert result['severityExact'] == 0 and result['severityWithinOne'] == .5
    assert result['abstentions'] == 1 and result['goldRoots'] == 2
    assert score_records(gold, records[:1])['missing'] == 2
    assert jaccard({'a', 'b'}, {'b', 'c'}) == pytest.approx(1/3)
    assert jaccard(set(), set()) == 1
    duplicate = deepcopy(records[0]); duplicate['findings'] *= 2
    assert score_records(gold, [duplicate])['falsePositives'] == 1
    for status in ('error', 'deferred', 'unsupported'):
        failed = dict(records[1], status=status)
        scored = score_records(gold, [records[0], failed])
        assert scored['recall'] == .5
    with pytest.raises(ValueError): score_records(gold, [records[0], records[0]])


def test_gold_schema(tmp_path):
    gold = load_gold()
    assert len(gold) == 26
    assert {r['variant'] for r in gold} == {'vulnerable', 'fixed'}
    path = tmp_path / 'bad.json'
    import json
    doc = {'schemaVersion': '1.0', 'cases': deepcopy(gold)}
    doc['cases'][0]['roots'][0]['severity'] = 'enormous'
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError): load_gold(path)
    doc['cases'][0] = deepcopy(gold[0]); doc['cases'].append(deepcopy(gold[0]))
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError): load_gold(path)


def test_bundle_scoring_verifies_seals_and_snapshot(tmp_path):
    import json
    import shutil
    from pathlib import Path
    from evals.harness import ROOT, score_bundle_directory
    from hermes_security.service import SecurityService
    gold = load_gold()
    case = gold[0]
    target = tmp_path / 'target'
    shutil.copytree(ROOT / case['fixture'], target)
    service = SecurityService(tmp_path / 'state')
    # Deliberately empty synthetic scan to exercise mechanics, not a model output.
    plan = service.start_scan(path=str(target), provider='unit-test-only', model='no-inference')
    sealed = service.finalize(plan['scanId'])
    directory = tmp_path / 'inputs'; directory.mkdir()
    shutil.copytree(sealed['artifactDir'], directory / 'bundle')
    index = {'runs': [{'runId': '1', 'provider': 'unit-test-only', 'model': 'no-inference',
                       'records': [{'caseId': case['caseId'], 'status': 'ok', 'bundle': 'bundle'}]}]}
    (directory / 'index.json').write_text(json.dumps(index))
    result = score_bundle_directory(directory)
    score = result['runs'][0]['score']
    assert score['deferred'] == 1 and score['missing'] == 25
    assert score['recall'] == 0 and score['precision'] is None
    (directory / 'bundle/findings.json').write_text('{}')
    assert score_bundle_directory(directory)['runs'][0]['score']['errors'] == 1


def test_five_runs_with_missing_cases_do_not_agree(tmp_path):
    import json
    from evals.harness import score_bundle_directory
    index = {'runs': [{'runId': str(i), 'provider': 'unit-test-only', 'model': 'no-inference', 'records': []} for i in range(5)]}
    (tmp_path / 'index.json').write_text(json.dumps(index))
    result = score_bundle_directory(tmp_path)
    for g in result['gates'][-2:]:
        assert g['measured'] == 0 and g['status'] == 'FAIL'


def test_compare_without_pairs_is_not_run():
    import importlib.util
    from evals.harness import ROOT
    spec = importlib.util.spec_from_file_location('compare_upstream', ROOT / 'scripts/compare-upstream.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    result = module.compare(None)
    assert not result['gates'] and result['notRun'][0]['status'] == 'NOT RUN'


def test_deterministic_current_code():
    result = deterministic(contract=False)
    assert all(g['status'] == 'PASS' for g in result['gates']), result
