"""Focused standard plan: production code first, a reading budget, every skip named."""
from hermes_security.orchestration import standard
from hermes_security.standards import ledger
from hermes_security.target import signals


def f(path, lines=1000, kind='source', sig=()):
    item = {'path': path, 'lines': lines, 'kind': kind}
    if sig:
        item['signals'] = list(sig)
    return item


def big_repo():
    # Well over the small-scan threshold, so by-design skips apply.
    src = [f(f'app/src/mod{i}.py', 3000) for i in range(30)]
    return src + [f('app/tests/test_mod.py', 5000), f('app/src/mod0.test.ts', 2000),
                  f('docs/guide.md', 400, 'doc'), f('examples/demo.py', 300),
                  f('web/public/logo.png', 1, 'binary'), f('dist/app.min.js', 9000),
                  f('pnpm-lock.yaml', 40000, 'lock'), f('tests/fixtures/keys.py', 50, sig=['secret'])]


def read(plan):
    return [p for area in plan['areas'] for p in area]


def test_roles_come_from_generic_path_conventions():
    assert standard.role(f('src/api/users.py')) == 'source'
    assert standard.role(f('src/api/users.test.ts')) == 'test'
    assert standard.role(f('pkg/tests/test_users.py')) == 'test'
    assert standard.role(f('internal/users_test.go')) == 'test'
    assert standard.role(f('src/__mocks__/db.js')) == 'fixture'
    assert standard.role(f('docs/conf.py')) == 'doc'
    assert standard.role(f('README.md', kind='doc')) == 'doc'
    assert standard.role(f('examples/client.py')) == 'example'
    assert standard.role(f('web/dist/bundle.min.js')) == 'generated'
    assert standard.role(f('img/a.png', kind='binary')) == 'asset'
    assert standard.role(f('yarn.lock', kind='lock')) == 'lock'
    # Config, CI, IaC and migrations can hold the only copy of a control; they stay in.
    assert standard.role(f('.github/workflows/ci.yml', kind='iac')) == 'source'
    assert standard.role(f('settings/policy.yaml', kind='config')) == 'source'
    assert standard.role(f('migrations/0001_init.sql')) == 'source'


def test_large_repo_reads_production_code_and_names_every_skip():
    files = big_repo()
    plan = standard.plan(files)
    assert plan['skipped']['app/tests/test_mod.py'] == 'standard_test'
    assert plan['skipped']['app/src/mod0.test.ts'] == 'standard_test'
    assert plan['skipped']['docs/guide.md'] == 'standard_doc'
    assert plan['skipped']['examples/demo.py'] == 'standard_example'
    assert plan['skipped']['dist/app.min.js'] == 'standard_generated'
    assert plan['skipped']['pnpm-lock.yaml'] == 'standard_lock'
    # Every inventory file is either read or skipped with a reason, never silently dropped.
    assert set(read(plan)) | set(plan['skipped']) == {x['path'] for x in files}
    assert not set(read(plan)) & set(plan['skipped'])


def test_a_committed_secret_brings_a_skipped_file_back():
    plan = standard.plan(big_repo())
    assert 'tests/fixtures/keys.py' not in plan['skipped'] and 'tests/fixtures/keys.py' in read(plan)


def test_a_small_repo_is_read_in_full():
    files = [f('src/app.py', 2000), f('tests/test_app.py', 1500), f('docs/a.md', 100, 'doc')]
    plan = standard.plan(files)
    assert plan['skipped'] == {} and sorted(read(plan)) == sorted(x['path'] for x in files)


def test_the_reading_budget_keeps_the_riskiest_areas():
    quiet = [f(f'lib{i}/m{j}.py', 5000) for i in range(30) for j in range(5)]
    hot = [f('svc/auth/login.py', 4000, sig=['route', 'input', 'auth']), f('svc/auth/session.py', 4000, sig=['auth', 'input'])]
    plan = standard.plan(quiet + hot)
    lines = {x['path']: x['lines'] for x in quiet + hot}
    assert 'svc/auth/login.py' in read(plan)
    assert sum(lines[p] for p in read(plan)) <= standard.STANDARD_LINE_BUDGET
    assert 'standard_budget' in plan['skipped'].values()


def test_the_reading_budget_grows_with_repo_size():
    assert standard.line_budget(100_000) == standard.STANDARD_LINE_BUDGET
    assert standard.line_budget(2_000_000) == 500_000
    assert standard.line_budget(10_000_000) == standard.STANDARD_LINE_BUDGET_CAP


def test_a_large_repo_reads_more_than_the_base_budget():
    files = [f(f'svc{i}/m{j}.py', 5000) for i in range(80) for j in range(5)]
    plan = standard.plan(files)
    assert plan['readLines'] > standard.STANDARD_LINE_BUDGET
    assert plan['readLines'] <= standard.line_budget(2_000_000)


def test_path_words_need_content_to_count_as_risk():
    hooks = [f(f'apps/web/session/hooks/use{i}.ts', 4000, sig=['outbound']) for i in range(3)]
    api = [f(f'apps/api/routes/r{i}.py', 4000, sig=['route', 'input']) for i in range(3)]
    assert standard._content_risk(hooks) == 0
    assert standard._content_risk(api) == 1
    # A risky path word still counts once the code backs it up.
    assert standard._content_risk([f('svc/auth/check.py', 100, sig=['auth'])]) == 1


def test_translations_are_skipped_but_locale_code_is_not():
    assert standard.role(f('locales/de.yaml', kind='config')) == 'locale'
    assert standard.role(f('web/i18n/en/messages.json', kind='config')) == 'locale'
    assert standard.role(f('src/i18n/middleware.ts')) == 'source'


def test_only_code_signals_count_as_risk():
    strings = [f(f'conf/strings{i}.json', 4000, 'config', sig=['route', 'input', 'auth']) for i in range(3)]
    assert standard._content_risk(strings) == 0
    # CI and IaC hold deploy-time authority: always worth reading.
    assert standard._content_risk([f('.github/workflows/ci.yml', 100, 'iac')]) == 1
    # A config file named for a control still counts.
    assert standard._content_risk([f('settings/permissions.yaml', 100, 'config')]) == 1


def test_every_component_gets_its_riskiest_area_before_any_goes_deep():
    hot = [f(f'hot/m{i}.py', 5000, sig=['route', 'input']) for i in range(100)]
    core = [f(f'core/c{i}.py', 5000, sig=['route', 'input'] if i % 2 else ()) for i in range(10)]
    apps = [f(f'apps/web/server/s{i}.ts', 5000, sig=['exec', 'input'] if i % 3 == 0 else ()) for i in range(6)]
    plan = standard.plan(hot + core + apps)
    assert any(p.startswith('core/') for p in read(plan))
    assert any(p.startswith('apps/web/') for p in read(plan))
    assert plan['readLines'] <= standard.line_budget(sum(x['lines'] for x in hot + core + apps))


def test_small_areas_never_earn_a_second_review():
    files = [f(f'lib/m{i}.py', 6250) for i in range(8)] + [f('svc/env.py', 500, sig=['route', 'input', 'exec'])]
    packets = standard.build_packets('scan_s', files, [], planner=2)
    assert any(p['review'] == 'area' and p['files'] == ['svc/env.py'] for p in packets)
    assert not [p for p in packets if p['review'] == 'second']


def test_snapshot_checks_skip_content_signals(tmp_path):
    from hermes_security.target import build_inventory, resolve_target
    (tmp_path / 'a.py').write_text('import subprocess\nsubprocess.run(x)\n')
    resolved = resolve_target(str(tmp_path))
    assert build_inventory(resolved)['files'][0]['signals'] == ['exec']
    assert 'signals' not in build_inventory(resolved, with_signals=False)['files'][0]


def test_area_packets_use_the_smaller_packet_budget():
    plan = standard.plan([f(f'src/m{i}.py', 4000) for i in range(12)])
    assert len(plan['areas']) >= 2
    assert all(len(area) == 1 or 4000 * len(area) <= standard.FOCUSED_PACKET_LINE_BUDGET for area in plan['areas'])


def test_second_reviews_are_few_and_follow_content_not_folder_names():
    files = [f(f'apps/server/agents/m{i}.ts', 6000) for i in range(40)]
    files += [f(f'apps/{name}/handler.ts', 6000, sig=['route', 'input', 'exec']) for name in ('hooks', 'jobs', 'shell', 'sync')]
    packets = standard.build_packets('scan_z', files, [], planner=2)
    seconds = [p for p in packets if p['review'] == 'second']
    assert 1 <= len(seconds) <= standard.STANDARD_SECOND_REVIEWS
    assert all(not any('/server/' in path for path in s['files']) for s in seconds)


def test_test_files_never_earn_a_second_review():
    files = [f(f'src/m{i}.py', 2000) for i in range(3)] + [f('src/auth/login.test.ts', 3000, sig=['auth', 'input', 'route'])]
    seconds = [p for p in standard.build_packets('scan_t', files, [], planner=2) if p['review'] == 'second']
    assert all('src/auth/login.test.ts' not in s['files'] for s in seconds)


def test_packets_get_only_the_profiles_their_files_need():
    files = [f('web/components/Button.tsx', 20000), f('infra/main.tf', 20000, 'iac')]
    profiles = ['client', 'cloud-iac', 'crypto', 'exceptional-conditions']
    packets = standard.build_packets('scan_p', files, profiles, planner=2)
    areas = [p for p in packets if p['review'] == 'area']
    assert len(areas) == 2
    for packet in areas:
        wanted = ledger.specialist_profiles_for(ledger.detect_surfaces([{'path': p} for p in packet['files']]))
        assert packet['profiles'] == [p for p in profiles if p in wanted] and 'crypto' not in packet['profiles']
    assert next(p for p in packets if p['role'] == 'baseline')['profiles'] == profiles


def test_skipped_files_have_no_packet_and_the_baseline_maps_instead_of_reading():
    packets = standard.build_packets('scan_b', big_repo(), [], planner=2)
    owned = {p for packet in packets for p in packet['files']}
    assert 'app/tests/test_mod.py' not in owned and 'pnpm-lock.yaml' not in owned
    baseline = packets[0]
    assert baseline['role'] == 'baseline' and 'entry-point map' in baseline['brief']


def test_existing_scans_keep_the_planner_they_started_with():
    files = [f(f'api/sub{i % 3}/f{i}.py', 900) for i in range(200)]
    assert standard.build_packets('scan_l', files, []) == standard.build_packets('scan_l', files, [], planner=1)
    assert standard.build_packets('scan_l', files, [], planner=1) != standard.build_packets('scan_l', files, [], planner=2)


def test_standard_allows_one_remainder_round():
    packets = standard.build_packets('scan_r', [f(f'src/m{i}.py', 100) for i in range(5)], [], planner=2)
    area = next(p for p in packets if p['review'] == 'area')
    first = standard.remainder_packets('scan_r', packets, {area['packetId']: area['files'][:2]},
                                       max_rounds=standard.remainder_rounds(2))
    assert len(first) == 1 and first[0]['round'] == 1
    second = standard.remainder_packets('scan_r', packets + first,
                                        {area['packetId']: area['files'][:2], first[0]['packetId']: area['files'][:1]},
                                        max_rounds=standard.remainder_rounds(2))
    assert [p['round'] for p in second] == [1]
    assert standard.remainder_rounds(1) == standard.MAX_REMAINDER_ROUNDS


def test_the_report_counts_by_design_skips_instead_of_listing_them():
    from hermes_security.reports import render_markdown
    excluded = [{'path': f'tests/t{i}.py', 'reason': 'standard_test'} for i in range(300)]
    excluded += [{'path': 'big.bin', 'reason': 'too_large'}]
    coverage = {'completeness': 'complete', 'gaps': [], 'units': [], 'workers': [], 'standards': {},
                'files': {'total': 10, 'reviewed': 10, 'deferred': 0, 'unsupported': 0, 'excluded': excluded}}
    text = render_markdown({'target': {}, 'status': 'completed'}, {'findings': []}, coverage, {'chains': []})
    assert 'Not read by this standard scan, by design: 300 test files.' in text
    assert 'tests/t1.py' not in text and 'big.bin' in text


def test_signals_mark_where_input_meets_authority():
    code = b"@app.post('/run')\ndef run():\n    subprocess.run(request.json['cmd'], shell=True)\n"
    assert {'route', 'input', 'exec'} <= set(signals.detect(code))
    assert signals.detect(b'def add(a, b):\n    return a + b\n') == []
    assert 'secret' in signals.detect(b'KEY = "AKIA' + b'Q7X2M9P4R8S6T1V3' + b'"\n')
