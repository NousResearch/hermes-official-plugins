"""Planner 4: a connection map from imports, and the cross-file questions it yields."""
from hermes_security.orchestration import flows


def f(path, lines=200, kind='source', signals=()):
    return {'path': path, 'lines': lines, 'kind': kind, 'signals': sorted(signals), 'size': lines * 30}


def L(*imports, **facets):
    return {'imports': list(imports), 'facets': facets}


def test_python_imports_resolve_relative_to_the_package_and_in_a_src_layout():
    files = [f('src/app/__init__.py'), f('src/app/web/routes.py'), f('src/app/core/__init__.py'),
             f('src/app/core/service.py'), f('src/app/json.py')]
    links = {'src/app/web/routes.py': L('..core.service', '..core', 'app.core.service.run', 'app.core.service', 'json')}
    assert flows.edges(files, links)['src/app/web/routes.py'] == {'src/app/core/service.py', 'src/app/core/__init__.py'}


def test_javascript_imports_resolve_extensions_index_files_and_path_aliases():
    files = [f('web/src/app.ts'), f('web/src/lib/auth.ts'), f('web/src/db/index.ts'), f('web/src/util.ts')]
    links = {'web/src/app.ts': L('./lib/auth.js', './db', '@/util', 'react')}
    assert flows.edges(files, links)['web/src/app.ts'] == {'web/src/lib/auth.ts', 'web/src/db/index.ts', 'web/src/util.ts'}


def test_ruby_requires_and_go_packages_resolve_by_path_suffix():
    files = [f('lib/zip.rb'), f('lib/zip/entry.rb'), f('lib/zip/file.rb'), f('cmd/app/main.go'),
             f('internal/auth/auth.go'), f('internal/auth/token.go'), f('internal/auth/auth_test.go')]
    links = {'lib/zip.rb': L('zip/entry', './zip/file'),
             'cmd/app/main.go': L('fmt', 'net/http', 'example.com/app/internal/auth')}
    edges = flows.edges(files, links)
    assert edges['lib/zip.rb'] == {'lib/zip/entry.rb', 'lib/zip/file.rb'}
    assert edges['cmd/app/main.go'] == {'internal/auth/auth.go', 'internal/auth/token.go'}


AREAS = [['web/admin.py', 'web/routes.py'], ['core/jobs.py', 'core/models.py'], ['authz/guard.py', 'runner/shell.py']]


def service_repo():
    files = [f('web/routes.py', 400, signals={'route', 'input'}), f('web/admin.py', 300, signals={'route', 'input'}),
             f('core/jobs.py', 500), f('core/models.py', 300), f('runner/shell.py', 200, signals={'exec'}),
             f('authz/guard.py', 150, signals={'auth'})]
    links = {'web/routes.py': L('core.jobs', 'authz.guard'), 'web/admin.py': L('authz.guard', 'core.models'),
             'core/jobs.py': L('runner.shell', 'core.models'), 'authz/guard.py': L(checks=2)}
    return files, links


def test_a_route_that_reaches_a_shell_call_in_another_folder_becomes_one_question():
    flow = next(x for x in flows.build_map(*service_repo(), AREAS)['flows'] if x['sink'] == 'runner/shell.py')
    assert flow['files'] == ['web/routes.py', 'core/jobs.py', 'runner/shell.py']
    assert flow['entries'] == ['web/routes.py'] and flow['sinkKinds'] == ['exec'] and flow['crossFile'] is True
    assert flow['controls'] == ['authz/guard.py']


def test_a_check_shared_by_several_entry_points_becomes_a_control_question():
    control = next(x for x in flows.build_map(*service_repo(), AREAS)['controls'] if x['control'] == 'authz/guard.py')
    assert control['callers'] == ['web/admin.py', 'web/routes.py'] and control['files'][0] == 'authz/guard.py'
    assert control['controlKinds'] == ['auth', 'checks']


def test_area_packets_learn_where_their_data_goes_in_other_folders():
    connections = flows.build_map(*service_repo(), AREAS)['connections']['0']
    assert 'web/routes.py -> core/jobs.py (leads to exec in runner/shell.py)' in connections
    assert 'web/routes.py -> authz/guard.py (control: auth, checks)' in connections


def test_a_library_verifier_that_parses_untrusted_input_is_a_control_question_without_any_routes():
    files = [f('jose/jws.py', 400), f('jose/jwt.py', 300), f('jose/util.py', 100)]
    links = {'jose/jws.py': L('jose.util', api=3, checks=2), 'jose/jwt.py': L('jose.jws', api=2)}
    control = flows.build_map(files, links, [['jose/jws.py', 'jose/jwt.py', 'jose/util.py']])['controls'][0]
    assert control['control'] == 'jose/jws.py' and control['callers'] == ['jose/jwt.py']


def test_a_file_that_takes_input_and_reaches_a_sink_itself_gets_a_focused_question():
    files = [f('src/directives/include.py', 120)]
    links = {'src/directives/include.py': L(api=1, path=3)}
    flow = flows.build_map(files, links, [['src/directives/include.py']])['flows'][0]
    assert flow['files'] == ['src/directives/include.py'] and flow['crossFile'] is False


def test_questions_are_capped_by_the_size_of_the_scan():
    files = [f(f'web/r{i}.py', signals={'route'}) for i in range(10)]
    files += [f(f'run/s{i}.py', signals={'exec'}) for i in range(10)]
    links = {f'web/r{i}.py': L(f'run.s{i}') for i in range(10)}
    result = flows.build_map(files, links, [[x['path'] for x in files]])
    assert len(result['flows']) + len(result['controls']) == flows.MIN_QUESTIONS


def test_a_go_import_links_only_the_files_that_define_what_is_used():
    files = [f('cmd/app/main.go'), f('internal/auth/verify.go'), f('internal/auth/token.go'), f('internal/auth/store.go')]
    links = {'cmd/app/main.go': dict(L('example.com/app/internal/auth'),
                                     uses={'example.com/app/internal/auth': ['Token', 'Verify']}),
             'internal/auth/verify.go': dict(L(), defines=['Verify']),
             'internal/auth/token.go': dict(L(), defines=['Claims', 'Token']),
             'internal/auth/store.go': dict(L(), defines=['Store'])}
    assert flows.edges(files, links)['cmd/app/main.go'] == {'internal/auth/verify.go', 'internal/auth/token.go'}


def test_tests_and_support_code_stay_out_of_the_map():
    files = [f('benchmark/bench.py', signals={'input'}), f('pkg/e2etests/run.py', signals={'route'}),
             f('lib/render.py'), f('lib/test_render.py', signals={'route'})]
    links = {path: L('lib.render') for path in ('benchmark/bench.py', 'pkg/e2etests/run.py', 'lib/test_render.py')}
    links['lib/render.py'] = L(api=1, path=2)
    result = flows.build_map(files, links, [[x['path'] for x in files]])
    assert result['stats']['files'] == 1 and result['stats']['aside'] == 3
    assert [x['files'] for x in result['flows']] == [['lib/render.py']]


def test_a_file_that_only_gathers_imports_never_carries_a_path():
    files = [f('lib/app.rb', signals={'input'}), f('lib/app/all.rb', 10), f('lib/app/cli.rb', signals={'input'}),
             f('lib/app/disk.rb')] + [f(f'lib/app/m{i}.rb') for i in range(8)]
    links = {'lib/app.rb': L('./app/all'), 'lib/app/all.rb': L(*[f'./m{i}' for i in range(8)], './disk'),
             'lib/app/cli.rb': L('./disk'), 'lib/app/disk.rb': L(path=3)}
    result = flows.build_map(files, links, [[x['path'] for x in files]])
    assert result['stats']['hubs'] == 1
    assert [x['files'] for x in result['flows']] == [['lib/app/cli.rb', 'lib/app/disk.rb']]


def test_archive_code_is_a_sink_only_where_it_also_writes_paths():
    files = [f('lib/zip/dos_time.rb'), f('lib/zip/entry.rb')]
    links = {'lib/zip/dos_time.rb': L(api=2, archive=5), 'lib/zip/entry.rb': L(api=2, archive=5, path=3)}
    flow, = flows.build_map(files, links, [[x['path'] for x in files]])['flows']
    assert flow['sink'] == 'lib/zip/entry.rb' and flow['sinkKinds'] == ['archive', 'path']


def test_checks_that_more_files_reach_rank_first():
    files = [f('jose/jws.py'), f('jose/jwt.py'), f('authz/guard.py', signals={'auth'}), f('web/one.py')]
    files += [f(f'apps/a{i}.py') for i in range(12)]
    links = {'jose/jws.py': L(api=3, checks=2, parse=2), 'jose/jwt.py': L('jose.jws'),
             'authz/guard.py': L(checks=1), 'web/one.py': L('authz.guard')}
    links.update({f'apps/a{i}.py': L('jose.jwt') for i in range(12)})
    controls = flows.build_map(files, links, [[x['path'] for x in files]])['controls']
    assert [c['control'] for c in controls] == ['jose/jws.py', 'authz/guard.py'] and controls[0]['trustedBy'] == 13


def test_questions_spread_over_entry_points_and_folders_before_repeating(monkeypatch):
    monkeypatch.setattr(flows, 'MIN_QUESTIONS', 2)
    files = [f('web/main.py', signals={'route'}), f('hooks/admin.py', signals={'webhook'}),
             f('jobs/run.py', signals={'exec'})] + [f(f'store/s{i}.py', signals={'sql'}) for i in range(4)]
    links = {'web/main.py': L(*[f'store.s{i}' for i in range(4)]), 'hooks/admin.py': L('jobs.run')}
    result = flows.build_map(files, links, [[x['path'] for x in files]])
    assert [x['sink'] for x in result['flows']] == ['store/s0.py', 'jobs/run.py']


def test_config_files_and_documents_are_not_questions():
    files = [f('config-raw.json', kind='config', signals={'input', 'file'}), f('app/run.py', signals={'input', 'exec'})]
    result = flows.build_map(files, {}, [[x['path'] for x in files]])
    assert [x['sink'] for x in result['flows']] == ['app/run.py']
