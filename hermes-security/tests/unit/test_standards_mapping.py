"""Offline contract tests for the version-pinned standards and coverage ledger."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_security.standards.ledger import (
    load_top10, load_asvs, load_cwe_map, detect_surfaces, applicable_controls,
    build_ledger, mandatory_gaps, map_cwe, specialist_profiles_for,
)

NAMES = [
    'Broken Access Control', 'Security Misconfiguration',
    'Software Supply Chain Failures', 'Cryptographic Failures', 'Injection',
    'Insecure Design', 'Authentication Failures', 'Software or Data Integrity Failures',
    'Security Logging and Alerting Failures', 'Mishandling of Exceptional Conditions',
]


def test_official_names_and_references():
    cats = load_top10()['categories']
    assert [(c['id'], c['name']) for c in cats] == [
        (f'A{i:02}:2025', name) for i, name in enumerate(NAMES, 1)]
    assert all(c['reviewLanes'] and c['cwe'] and c['plainDescription'] for c in cats)
    controls = load_asvs()['controls']
    assert len(controls) >= 80
    ids = {c['id'] for c in controls}
    assert len(ids) == len(controls)
    assert all(re.fullmatch(r'V\d+\.\d+\.\d+', i) for i in ids)
    assert {c['chapter'] for c in controls} == {f'V{i}' for i in range(1, 18)}
    assert all(len(c['summary']) <= 200 and c['level'] in (1, 2, 3) for c in controls)
    from hermes_security.standards.ledger import SURFACES
    assert all(c['surfaces'] and set(c['surfaces']) <= SURFACES for c in controls)
    assert all(c['evidenceTypes'] for c in controls)
    cat_ids = {c['id'] for c in cats}
    mappings = load_cwe_map()['entries']
    assert len(mappings) >= 120
    for entry in mappings.values():
        assert entry['name']
        assert set(entry['owasp']) <= cat_ids
        assert set(entry['asvs']) <= ids


def test_zero_units_are_unknown_not_clean():
    surfaces = {'web-route', 'auth', 'session', 'config', 'logging'}
    ledger = build_ledger(surfaces, [], [])
    assert all(c['state'] == 'unknown' for c in ledger['owaspTop10']['categories'])
    applicable = {c['id'] for c in applicable_controls(surfaces)}
    for c in ledger['asvs']['controls']:
        assert c['state'] == ('unknown' if c['id'] in applicable else 'not_applicable')
    assert mandatory_gaps(ledger)


def test_findings_and_files_do_not_close_lanes():
    findings = [{'taxonomy': {'cwe': ['CWE-918'], 'owasp': ['A01:2025']}}]
    ledger = build_ledger({'api'}, [{'unit': 'file:routes/api.py', 'state': 'reviewed'}], findings)
    first = ledger['owaspTop10']['categories'][0]
    assert first['state'] == 'unknown'
    assert first['reason'] == 'finding mapped but lane not closed'
    assert all(c['state'] != 'reviewed' for c in ledger['asvs']['controls'])


def test_explicit_closures_and_evidence():
    units = [{'unitId': 'lane:A01:2025', 'state': 'reviewed', 'reason': 'traced', 'evidence': ['att_1']},
             {'unit': 'asvs:V8.2.2', 'state': 'deferred', 'note': 'needs owner'}]
    ledger = build_ledger({'authz'}, units, [])
    first = ledger['owaspTop10']['categories'][0]
    assert (first['state'], first['reason'], first['evidence']) == ('reviewed', 'traced', ['att_1'])
    control = next(c for c in ledger['asvs']['controls'] if c['id'] == 'V8.2.2')
    assert control['state'] == 'deferred' and control['reason'] == 'needs owner'
    assert any('V8.2.2' in gap for gap in mandatory_gaps(ledger))


def test_exceptional_conditions_and_ssrf():
    assert map_cwe('CWE-918')['owasp'] == ['A01:2025']
    for cwe in (703, 754, 755, 390, 636):
        assert 'A10:2025' in map_cwe(f'CWE-{cwe}')['owasp']
    for cwe in (1104, 829, 494, 778, 117, 532):
        assert map_cwe(f'CWE-{cwe}')['owasp']
    assert map_cwe('CWE-999999') == {'owasp': [], 'asvs': []}
    assert map_cwe('918') == {'owasp': [], 'asvs': []}


def test_detect_surfaces_and_profiles():
    inventory = [{'path': p} for p in ['routes/flask_api.py', 'package.json', 'pyproject.toml',
        '.github/workflows/build.yml', 'infra/main.tf', 'AndroidManifest.xml', 'screen.swift',
        'schema.graphql', 'auth/login.py', 'session/jwt.py', 'crypto/cipher.py', 'upload.py',
        'llm/agent.py', 'websocket/server.py', 'client/index.tsx', 'db/store.py']]
    surfaces = detect_surfaces(inventory)
    assert {'web-route', 'api', 'dependencies', 'ci', 'iac', 'mobile', 'graphql', 'auth',
            'session', 'crypto', 'file-upload', 'llm-agent', 'websocket', 'config', 'logging',
            'client-web', 'data-store'} <= surfaces
    assert detect_surfaces([{'path': 'README.md'}]) == set()
    profiles = specialist_profiles_for(surfaces)
    assert {'web-api', 'auth', 'crypto', 'business-logic', 'supply-chain', 'client',
            'cloud-iac', 'mobile', 'agentic-ai', 'exceptional-conditions'} <= set(profiles)
    assert specialist_profiles_for(set()) == []
    assert all((Path(__file__).resolve().parents[2] / 'references/specialist-profiles' / (p + '.md')).is_file() for p in profiles)


def test_closed_ledger_has_no_mandatory_gaps():
    units = [{'unit': 'lane:' + c['id'], 'state': 'not_applicable', 'note': 'explicit scope'}
             for c in load_top10()['categories']]
    assert mandatory_gaps(build_ledger(set(), units, [])) == []


def test_loaders_do_not_leak_mutation():
    first = load_top10()
    first['categories'].clear()
    assert len(load_top10()['categories']) == 10


def test_invalid_and_conflicting_units_fail_conservatively():
    units = [{'unit': 'lane:A01:2025', 'state': 'reviewed'},
             {'unit': 'lane:A01:2025', 'state': 'failed'},
             {'unit': 'lane:A02:2025', 'state': 'banana'}]
    ledger = build_ledger({'web-route'}, units, [])
    assert ledger['owaspTop10']['categories'][0]['state'] != 'reviewed'
    assert ledger['owaspTop10']['categories'][1]['state'] == 'unknown'


def test_each_explicit_state_is_preserved_without_findings():
    for state in ('reviewed', 'not_applicable', 'deferred', 'unsupported', 'unknown', 'failed'):
        units = [{'unit': 'lane:A10:2025', 'state': state, 'note': 'exception paths checked'},
                 {'unit': 'asvs:V16.5.3', 'state': state, 'note': 'fail closed'}]
        ledger = build_ledger({'logging'}, units, [])
        assert ledger['owaspTop10']['categories'][-1]['state'] == state
        assert next(c for c in ledger['asvs']['controls'] if c['id'] == 'V16.5.3')['state'] == state


def test_finding_contradicts_not_applicable_not_silently_clean():
    finding = {'taxonomy': {'cwe': ['CWE-636']}}
    units = [{'unit': 'lane:A10:2025', 'state': 'not_applicable'},
             {'unit': 'asvs:V16.5.3', 'state': 'not_applicable'}]
    ledger = build_ledger(set(), units, [finding])
    assert ledger['owaspTop10']['categories'][-1]['state'] == 'unknown'
    assert next(c for c in ledger['asvs']['controls'] if c['id'] == 'V16.5.3')['state'] == 'unknown'
    assert mandatory_gaps(ledger)


def test_exceptional_case_checklist_and_conflict_order_independence():
    profile = (Path(__file__).resolve().parents[2] /
               'references/specialist-profiles/exceptional-conditions.md').read_text().lower()
    for case in ('fail-open auth', 'partial writes', 'timeout defaults', 'rollback gaps',
                 'retry duplication', 'resource exhaustion'):
        assert case in profile
    units = [{'unit': 'lane:A10:2025', 'state': 'reviewed'},
             {'unit': 'lane:A10:2025', 'state': 'not_applicable'}]
    assert build_ledger(set(), units, []) == build_ledger(set(), units[::-1], [])
    assert build_ledger(set(), units, [])['owaspTop10']['categories'][-1]['state'] == 'unknown'
