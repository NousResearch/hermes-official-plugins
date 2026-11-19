"""Standard scans read production code and report by-design skips as exclusions, not gaps."""
from hermes_security.orchestration import standard
from hermes_security.service import SecurityService
from test_end_to_end_static import worker


def start(tmp_path, monkeypatch):
    # A tiny threshold stands in for a large repository.
    monkeypatch.setattr(standard, 'SMALL_SCAN_LINES', 5)
    root = tmp_path / 'repo'
    (root / 'src').mkdir(parents=True); (root / 'tests').mkdir(); (root / 'docs').mkdir()
    (root / 'src' / 'app.py').write_text('def handler(request):\n    return request.args.get("q")\n')
    (root / 'src' / 'util.py').write_text('def add(a, b):\n    return a + b\n')
    (root / 'tests' / 'test_app.py').write_text('def test_handler():\n    assert True\n')
    (root / 'docs' / 'guide.md').write_text('# Guide\n\nUsage notes.\n')
    service = SecurityService(tmp_path / 'data')
    return service, service.start_scan(path=str(root))


def test_new_standard_scans_use_the_focused_planner(tmp_path, monkeypatch):
    service, plan = start(tmp_path, monkeypatch)
    scan = service.get_scan(plan['scanId'])
    assert scan['options']['planner'] == standard.PLANNER_VERSION == 4
    owned = {p for packet in plan['packets'] for p in packet['files']}
    assert owned == {'src/app.py', 'src/util.py'}
    assert plan['standardPlan']['skipped'] == {'standard_doc': 1, 'standard_test': 1}


def test_by_design_skips_are_listed_exclusions_not_gaps(tmp_path, monkeypatch):
    service, plan = start(tmp_path, monkeypatch)
    sid = plan['scanId']
    # The baseline defers a skipped file; that must not reopen it as unfinished work.
    service.submit_worker_result(worker(plan, [], coverage=[
        {'unit': 'file:tests/test_app.py', 'state': 'deferred', 'note': 'Not mine.'}]))
    coverage = service.coverage(sid)
    excluded = {e['path']: e['reason'] for e in coverage['files']['excluded']}
    assert excluded['tests/test_app.py'] == 'standard_test' and excluded['docs/guide.md'] == 'standard_doc'
    assert 'file:tests/test_app.py' not in {u['unitId'] for u in coverage['units']}
    assert not any('tests/test_app.py' in gap or 'docs/guide.md' in gap for gap in coverage['gaps'])
    assert coverage['files']['total'] == 2
    assert coverage['standardPlan']['skipped'] == {'standard_doc': 1, 'standard_test': 1}


def test_coverage_reads_count_by_design_skips_instead_of_listing_them(tmp_path, monkeypatch):
    service, plan = start(tmp_path, monkeypatch)
    sid = plan['scanId']
    view = service.coverage_view(sid)
    assert view['files']['excludedByDesign'] == {'standard_doc': 1, 'standard_test': 1}
    assert not any(e['reason'].startswith('standard_') for e in view['files']['excluded'])
    assert service.get_scan(sid, 'coverage') == view
    # The full document, which the sealed report uses, still lists every file.
    assert {e['path'] for e in service.coverage(sid)['files']['excluded']} >= {'tests/test_app.py', 'docs/guide.md'}


def test_a_skipped_file_a_worker_reviewed_counts_as_reviewed(tmp_path, monkeypatch):
    service, plan = start(tmp_path, monkeypatch)
    service.submit_worker_result(worker(plan, [], coverage=[
        {'unit': 'file:tests/test_app.py', 'state': 'reviewed', 'note': 'Followed a flow into it.'}]))
    coverage = service.coverage(plan['scanId'])
    assert 'tests/test_app.py' not in {e['path'] for e in coverage['files']['excluded']}
    assert {u['unitId']: u['state'] for u in coverage['units']}['file:tests/test_app.py'] == 'reviewed'


def test_deep_scans_still_read_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(standard, 'SMALL_SCAN_LINES', 5)
    root = tmp_path / 'repo'; (root / 'tests').mkdir(parents=True)
    (root / 'app.py').write_text('x = 1\n'); (root / 'tests' / 'test_app.py').write_text('y = 2\n')
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=str(root), mode='deep', deep_passes=1)
    assert set(plan['packets'][0]['files']) == {'app.py', 'tests/test_app.py'}
    assert 'planner' not in service.get_scan(plan['scanId'])['options']
