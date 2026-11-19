"""Standard-scan packet planning: disjoint areas, bounded size, rolling schedule."""
from hermes_security.orchestration import standard


def inventory(n, lines=900, dirs=('api', 'core', 'ui', 'docs')):
    return [{'path': f'{dirs[i % len(dirs)]}/sub{i % 3}/f{i}.py', 'lines': lines} for i in range(n)]


def test_areas_cover_every_file_exactly_once_within_budget():
    files = inventory(400)
    areas = standard.partition(files)
    flat = [p for area in areas for p in area]
    assert sorted(flat) == sorted(f['path'] for f in files) and len(flat) == len(set(flat))
    lines = {f['path']: f['lines'] for f in files}
    assert all(sum(lines[p] for p in area) <= standard.PACKET_LINE_BUDGET for area in areas)
    assert len(areas) >= 400 * 900 // standard.PACKET_LINE_BUDGET


def test_areas_follow_directories_and_oversized_files_stand_alone():
    files = [{'path': 'a/x.py', 'lines': 10}, {'path': 'a/y.py', 'lines': 10},
             {'path': 'b/huge.js', 'lines': standard.PACKET_LINE_BUDGET * 2}, {'path': 'c/z.py', 'lines': 10}]
    areas = standard.partition(files)
    assert ['b/huge.js'] in areas
    assert all(not ({'a/x.py', 'c/z.py'} <= set(area)) or 'b/huge.js' not in area for area in areas)


def test_binaries_and_lockfiles_do_not_crowd_out_source():
    files = [{'path': 'img/a.png', 'lines': 30000, 'kind': 'binary'},
             {'path': 'package-lock.json', 'lines': 20000, 'kind': 'lock'},
             {'path': 'src/app.py', 'lines': 30000, 'kind': 'source'}]
    assert len(standard.partition(files)) == 1


def test_file_cap_splits_many_tiny_files():
    files = [{'path': f'data/f{i}.json', 'lines': 1} for i in range(standard.PACKET_FILE_CAP * 2 + 5)]
    assert all(len(area) <= standard.PACKET_FILE_CAP for area in standard.partition(files))


def test_one_baseline_disjoint_areas_and_bounded_second_reviews():
    files = inventory(200) + [{'path': 'gateway/auth/session.py', 'lines': 30000}]
    packets = standard.build_packets('scan_x', files, [])
    baseline = [p for p in packets if p['role'] == 'baseline']
    areas = [p for p in packets if p['review'] == 'area']
    seconds = [p for p in packets if p['review'] == 'second']
    assert len(baseline) == 1 and baseline[0]['filesFrom'] == 'inventory' and baseline[0]['files'] == []
    assert baseline[0]['fileCount'] == len(files)
    assert sum(len(p['files']) for p in areas) == len(files)
    assert 1 <= len(seconds) <= max(1, -(-len(areas) // 4))
    risky = next(p for p in areas if 'gateway/auth/session.py' in p['files'])
    assert any(s['independentOf'] == risky['packetId'] and s['files'] == risky['files'] for s in seconds)
    assert len({p['packetId'] for p in packets}) == len(packets)
    # Identity is deterministic, so resumes and submissions see the same packets.
    assert [p['packetId'] for p in standard.build_packets('scan_x', files, [])] == [p['packetId'] for p in packets]


def test_the_riskiest_area_always_gets_a_second_reviewer():
    files = [{'path': f'lib/mod{i}.py', 'lines': 9000} for i in range(12)] + [{'path': 'lib/shell_runner.py', 'lines': 3000}]
    packets = standard.build_packets('scan_y', files, [])
    seconds = [p for p in packets if p['review'] == 'second']
    assert len(seconds) == 1 and 'lib/shell_runner.py' in seconds[0]['files']
    assert all(p['area'].startswith('lib/') for p in packets if p['review'] == 'area')


def test_schedule_runs_up_to_the_host_cap():
    assert standard.SCHEDULE['maxParallelWorkers'] == 10 <= standard.SCHEDULE['hostWorkerLimit']
    assert 'one delegate_task call per packet' in standard.SCHEDULE['dispatch']


def test_remainders_chain_and_stop_after_max_rounds():
    packets = standard.build_packets('scan_x', inventory(10), [])
    area = next(p for p in packets if p['review'] == 'area')
    first = standard.remainder_packets('scan_x', packets, {area['packetId']: area['files'][:3]})
    assert len(first) == 1 and first[0]['round'] == 1 and first[0]['files'] == sorted(area['files'][:3])
    chain, remainders, parent = list(packets), {area['packetId']: area['files'][:3]}, area['packetId']
    for _ in range(standard.MAX_REMAINDER_ROUNDS + 2):
        made = standard.remainder_packets('scan_x', chain, remainders)
        parent = made[-1]['packetId']
        remainders[parent] = area['files'][:1]
        chain = list(packets) + made
    assert max(p['round'] for p in standard.remainder_packets('scan_x', packets, remainders)) == standard.MAX_REMAINDER_ROUNDS
    assert standard.remainder_packets('scan_x', packets, {area['packetId']: []}) == []
