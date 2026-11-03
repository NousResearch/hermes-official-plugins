"""Total scan time: start to seal, or start to now while the scan is open."""
from hermes_security import timing
from hermes_security.reports import render_markdown


def test_duration_text_reads_like_a_person_wrote_it():
    assert timing.duration_text(0) == 'under a minute'
    assert timing.duration_text(59) == 'under a minute'
    assert timing.duration_text(60) == '1 min'
    assert timing.duration_text(3599) == '59 min'
    assert timing.duration_text(3600) == '1 h'
    assert timing.duration_text(3900) == '1 h 5 min'
    assert timing.duration_text(2 * 86400 + 3 * 3600) == '2 d 3 h'
    assert timing.duration_text(None) == 'not recorded'


def test_a_sealed_scan_runs_from_creation_to_seal():
    scan = {'created_at': '2026-10-26T20:52:00Z', 'sealed_at': '2026-10-27T01:07:30Z', 'status': 'partial'}
    result = timing.scan_timing(scan, now='2026-10-28T00:00:00Z')
    assert result == {'startedAt': '2026-10-26T20:52:00Z', 'finishedAt': '2026-10-27T01:07:30Z',
                      'durationSeconds': 15330, 'running': False, 'label': '4 h 15 min'}


def test_an_open_scan_counts_up_to_now():
    scan = {'created_at': '2026-10-26T20:52:00Z', 'sealed_at': None, 'status': 'running'}
    result = timing.scan_timing(scan, now='2026-10-26T21:30:00Z')
    assert result['running'] is True and result['finishedAt'] is None
    assert result['durationSeconds'] == 2280 and result['label'] == '38 min'


def test_bad_timestamps_never_raise():
    result = timing.scan_timing({'created_at': 'garbage', 'sealed_at': None}, now='2026-10-26T21:30:00Z')
    assert result['durationSeconds'] is None and result['label'] == 'not recorded'
    assert timing.elapsed_seconds('2026-10-27T00:00:00Z', '2026-10-26T00:00:00Z') is None


def test_the_report_states_total_scan_time():
    manifest = {'target': {}, 'status': 'completed',
                'startedAt': '2026-10-26T20:52:00Z', 'finishedAt': '2026-10-27T01:07:30Z'}
    coverage = {'completeness': 'complete', 'gaps': [], 'units': [], 'workers': [], 'standards': {},
                'files': {'total': 1, 'reviewed': 1, 'deferred': 0, 'unsupported': 0, 'excluded': []}}
    text = render_markdown(manifest, {'findings': []}, coverage, {'chains': []})
    assert 'Scan time: 4 h 15 min (2026-10-26T20:52:00Z to 2026-10-27T01:07:30Z).' in text


def test_scan_reads_carry_timing(tmp_path):
    from hermes_security.service import SecurityService
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('x = 1\n')
    service = SecurityService(tmp_path / 'data')
    sid = service.start_scan(path=str(root))['scanId']
    summary = service.get_scan(sid, 'summary')
    assert summary['timing']['running'] is True and summary['timing']['durationSeconds'] >= 0
    assert service.get_scan(sid)['timing']['startedAt'] == summary['timing']['startedAt']
    row = service.workbench_scans()['items'][0]
    assert row['timing']['startedAt'] == summary['timing']['startedAt']
