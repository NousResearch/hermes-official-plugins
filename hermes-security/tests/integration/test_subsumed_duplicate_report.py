"""An explicit one-fix subsumption is not a false-positive rejection."""
from copy import deepcopy

from test_service_modes import setup
from test_end_to_end_static import candidate_for, worker


def test_explicit_subsumption_of_rejected_duplicate_is_conserved_as_duplicate(tmp_path):
    root, service, plan = setup(tmp_path, dependency_check=False)
    first = candidate_for(root)
    other = deepcopy(first)
    other['ruleId'] = 'authz.same-root-alternate-rule'
    other['identity']['anchor'] = 'app.py/alternate-observation'
    service.submit_worker_result(worker(plan, [first, other]))
    rows = service.get_scan(plan['scanId'], 'candidates')['items']
    winner, duplicate = rows
    winner['evidenceState'] = 'source_supported'
    winner['provenance']['subsumes'] = [duplicate['candidateId']]
    duplicate['evidenceState'] = 'rejected'
    service.submit_worker_result(worker(plan, [winner, duplicate], attempt='att_dupe_validator', role='validator'))
    reduced = service._reduced(plan['scanId'])
    assert not reduced['problems']
    assert not reduced['rejected']
    assert len(reduced['absorbed']) == 1
    service.finalize(plan['scanId'])
    report = service.get_scan(plan['scanId'], 'report')['content']
    assert '1 duplicate or subsumed report' in report
    assert 'Not an issue' not in report
