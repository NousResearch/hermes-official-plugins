"""Planner 3: worker-sized packets, a budget counted in packets, and a plan saved at start."""
from hermes_security.orchestration import standard
from hermes_security.service import SecurityService

SCAN = 'scan_' + 'a' * 24


def f(path, lines=200, kind='source', signals=()):
    return {'path': path, 'lines': lines, 'kind': kind, 'signals': sorted(signals), 'size': lines * 30}


def test_small_repo_reads_all_production_code_and_skips_the_rest_by_design():
    files = [f('src/app.py', 900, signals={'route', 'input'}), f('src/util.py', 300),
             f('tests/test_app.py', 400), f('docs/guide.md', 100, kind='doc'),
             f('docs/setup.md', 50, kind='doc', signals={'secret'})]
    result = standard.plan(files, 3)
    assert {p for area in result['areas'] for p in area} == {'src/app.py', 'src/util.py', 'docs/setup.md'}
    assert result['skipped'] == {'tests/test_app.py': 'standard_test', 'docs/guide.md': 'standard_doc'}


def test_packets_are_sized_for_one_worker():
    files = [f(f'src/mod{i // 10}/f{i}.py', 700) for i in range(60)]
    lines = {x['path']: x['lines'] for x in files}
    areas = standard.plan(files, 3)['areas']
    assert all(sum(lines[p] for p in area) <= standard.WORKER_PACKET_LINES for area in areas)
    assert {p for area in areas for p in area} == set(lines)


def test_large_repo_keeps_the_riskiest_areas_within_the_packet_budget(monkeypatch):
    monkeypatch.setattr(standard, 'STANDARD_AREA_PACKETS', 3)
    files = [f(f'lib/plain{i}/x.py', 3_900) for i in range(10)]
    files += [f('tools/approval.py', 3_000, signals={'exec', 'auth', 'input'}),
              f('web/routes.py', 3_000, signals={'route', 'input'}),
              f('web/i18n/fr.ts', 3_000, signals={'auth', 'file', 'webhook'})]
    result = standard.plan(files, 3)
    read = {p for area in result['areas'] for p in area}
    assert len(result['areas']) == 3
    assert {'tools/approval.py', 'web/routes.py'} <= read
    assert result['skipped']['web/i18n/fr.ts'] == 'standard_locale'
    assert sum(reason == 'standard_budget' for reason in result['skipped'].values()) == 9


def test_code_under_locale_folders_that_handles_input_stays_source():
    assert standard.role(f('app/i18n/loader.py', signals={'input', 'file'}), 3) == 'source'
    assert standard.role(f('app/i18n/fr.ts', signals={'auth'}), 3) == 'locale'
    assert standard.role(f('app/i18n/fr.ts', signals={'auth'})) == 'source'  # planner 2 unchanged


def test_build_counts_as_output_only_at_a_project_root():
    assert standard.role(f('scripts/build/release.py'), 3) == 'source'
    assert standard.role(f('build/bundle.js'), 3) == 'generated'
    assert standard.role(f('apps/web/build/app.js'), 3) == 'generated'
    assert standard.role(f('packages/ui/dist/index.js'), 3) == 'generated'
    assert standard.role(f('scripts/build/release.py')) == 'generated'  # planner 2 unchanged


def test_test_folders_with_suffixes_are_tests():
    assert standard.role(f('tests-js/helpers.js'), 3) == 'test'
    assert standard.role(f('pkg/integration_tests/run.py'), 3) == 'test'
    assert standard.role(f('contest/app.py'), 3) == 'source'


def test_developer_tooling_ranks_below_the_same_code_in_the_product():
    script = f('scripts/release.py', signals={'exec', 'input'})
    product = f('server/run.py', signals={'exec', 'input'})
    assert standard._file_rank(script) < standard._file_rank(product)


def test_more_sink_calls_rank_higher():
    busy = {**f('core/runner.py', signals={'exec'}), 'signalHits': 30}
    stray = {**f('core/helper.py', signals={'exec'}), 'signalHits': 1}
    assert standard._file_rank(busy) > standard._file_rank(stray)


def test_signals_count_hits_and_know_common_server_styles():
    from hermes_security.target import signals
    assert signals.profile(b'import subprocess\n' + b'subprocess.run(cmd)\n' * 12) == (['exec'], 12)
    assert 'route' in signals.detect(b'app.router.add_post("/api", handler)\n')
    assert 'route' in signals.detect(b'@api.get("/items")\ndef items(): ...\n')
    assert 'route' in signals.detect(b'config = uvicorn.Config(app)\nserver = uvicorn.Server(config)\n')
    assert 'exec' in signals.detect(b'proc = await asyncio.create_subprocess_exec(*argv)\n')
    assert 'exec' in signals.detect(b'from subprocess import Popen\np = Popen(argv)\n')
    assert 'input' in signals.detect(b'name = request.match_info["name"]\n')
    assert 'auth' in signals.detect(b'if not authenticate(user, password):\n')


def test_unclassified_code_files_are_scored_on_content():
    assert standard._file_score(f('server/index.mjs', kind='other', signals={'route', 'input'})) >= standard.RISKY_SIGNAL_SCORE
    assert standard._file_score(f('config/app.json', kind='config', signals={'route', 'input'})) == 0


def test_a_saved_plan_survives_planner_changes(monkeypatch):
    files = [f(f'src/m{i}/a.py', 1_500, signals={'input'}) for i in range(8)]
    frozen = standard.frozen_plan(files)
    before = standard.build_packets(SCAN, files, [], planner=3, frozen=frozen)
    monkeypatch.setattr(standard, 'WORKER_PACKET_LINES', 1_000)
    after = standard.build_packets(SCAN, files, [], planner=3, frozen=frozen)
    assert [(p['packetId'], p['files']) for p in after] == [(p['packetId'], p['files']) for p in before]
    # Without the saved plan the same change would have moved every packet.
    assert [p['files'] for p in standard.build_packets(SCAN, files, [], planner=3)] != [p['files'] for p in before]


def test_follow_ups_continue_while_workers_make_progress_and_stop_on_a_stall():
    files = [f('src/a.py'), f('src/b.py'), f('src/c.py')]
    packets = standard.build_packets(SCAN, files, [], planner=3)
    area = next(p for p in packets if p['review'] == 'area')
    left = ['src/b.py', 'src/c.py']
    # The first worker finished a.py; the rest goes to a fresh worker.
    r1 = standard.remainder_packets(SCAN, packets, {area['packetId']: left}, files, max_rounds=6, progress_rule=True)
    assert [p['files'] for p in r1] == [left] and not r1[0].get('stalled')
    # That worker finished nothing: one stalled retry.
    queued = {area['packetId']: left, r1[0]['packetId']: left}
    r2 = standard.remainder_packets(SCAN, packets, queued, files, max_rounds=6, progress_rule=True)
    assert len(r2) == 2 and r2[-1]['stalled'] is True
    # The retry also finished nothing: the chain ends and the files stay a visible gap.
    queued[r2[-1]['packetId']] = left
    assert len(standard.remainder_packets(SCAN, packets, queued, files, max_rounds=6, progress_rule=True)) == 2
    assert standard.remainder_rounds(3) == standard.WORKER_REMAINDER_ROUNDS


def test_a_new_standard_scan_saves_its_plan(tmp_path):
    root = tmp_path / 'repo'
    (root / 'src').mkdir(parents=True)
    (root / 'src' / 'app.py').write_text('from flask import request\n' + 'x = request.args\n' * 50)
    (root / 'tests').mkdir()
    (root / 'tests' / 'test_app.py').write_text('def test_x():\n    pass\n')
    service = SecurityService(tmp_path / 'data')
    plan = service.start_scan(path=str(root), dependency_check=False)
    options = service._scan(plan['scanId'])['options']
    assert options['planner'] == 4
    assert options['standardAreas']['areas'] == [['src/app.py']]
    areas = [p for p in plan['packets'] if p['review'] == 'area']
    assert [p['files'] for p in areas] == [['src/app.py']]
    assert plan['standardPlan']['skipped'] == {'standard_test': 1}
    assert plan['schedule']['maxParallelWorkers'] == 10
