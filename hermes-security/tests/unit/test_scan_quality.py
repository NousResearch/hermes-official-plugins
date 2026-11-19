"""Report and coverage quality: own packages, duplicate hints, lane notes, applicability."""
import json

from hermes_security import dependencies
from hermes_security.orchestration.reducer import possible_duplicates
from hermes_security.service import SecurityService
from hermes_security.standards.ledger import detect_surfaces

UV_LOCK = '''version = 1
[[package]]
name = "hermes-agent"
version = "0.0.0"
source = { editable = "." }

[[package]]
name = "filelock"
version = "3.0"
source = { registry = "https://pypi.org/simple" }
'''


def test_the_projects_own_packages_are_not_matched_by_name(tmp_path):
    # OSV matches by name only: an unpublished workspace package picks up a namesake's advisories.
    lock = {'name': 'web', 'packages': {'': {'name': 'web'}, 'apps/desktop': {'name': 'desktop'},
                                        'node_modules/desktop': {'link': True}, 'node_modules/zod': {'version': '4'}}}
    files = {'uv.lock': UV_LOCK, 'package-lock.json': json.dumps(lock)}
    assert dependencies.local_packages('uv.lock', files.get) == {'hermes-agent'}
    assert dependencies.local_packages('package-lock.json', files.get) == {'web', 'desktop'}
    assert dependencies.local_packages('uv.lock', lambda p: 'not [toml') == set()
    report = {'results': [{'source': {'path': 'uv.lock'}, 'packages': [
        {'package': {'name': 'hermes-agent', 'version': '0.0.0', 'ecosystem': 'PyPI'}, 'vulnerabilities': [{'id': 'PYSEC-1'}]},
        {'package': {'name': 'filelock', 'version': '3.0', 'ecosystem': 'PyPI'}, 'vulnerabilities': []}]}]}
    summary = dependencies.summarize(report, tmp_path, {'uv.lock'},
                                     local=lambda p: dependencies.local_packages(p, files.get))
    assert (summary['packages'], summary['vulnerablePackages'], summary['localPackages']) == (1, 0, 1)


def test_overlapping_first_locations_are_flagged_not_merged():
    def row(cid, start, end, state='source_supported'):
        return {'candidateId': cid, 'evidenceState': state, 'locations': [{'path': 'x.py', 'startLine': start, 'endLine': end}]}
    rows = [row('cand_a', 147, 174), row('cand_b', 157, 157), row('cand_c', 200, 210), row('cand_d', 150, 150, 'rejected')]
    assert possible_duplicates(rows) == [{'candidateIds': ['cand_a', 'cand_b'], 'path': 'x.py', 'lines': [157, 157]}]


def test_a_lane_closed_by_several_reviews_names_them_all(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('x = 1\n')
    service = SecurityService(tmp_path / 'data')
    sid = service.start_scan(path=str(root))['scanId']
    for aid in ('att_a', 'att_b'):
        service.store.record_coverage(sid, [{'unit': 'lane:A01:2025', 'state': 'reviewed', 'note': 'note ' + aid}],
                                      source=aid, monotonic=True)
    lane = next(c for c in service.coverage(sid)['standards']['owaspTop10']['categories'] if c['id'] == 'A01:2025')
    assert lane['name'] == 'Broken Access Control'
    assert lane['reason'] == 'Closed by 2 reviews (att_a, att_b); latest note: note att_b'


def test_a_plain_source_file_is_not_a_web_route():
    assert 'web-route' not in detect_surfaces([{'path': 'tools/ansi_strip.py'}])
    assert 'web-route' in detect_surfaces([{'path': 'app/routes/users.py'}])
