import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import pytest
import sys
from tests.support import make_link, outside_system_dir
from hermes_security.canonical import atomic_write, canonical_json, sha256_hex
from hermes_security.reports import finalize_bundle, verify_bundle, render_markdown, render_sarif, export, validate_finding_sections


def inputs() -> dict:
    f = json.loads((Path(__file__).parents[1] / 'fixtures/reports/invoice.json').read_text(encoding='utf-8'))
    f['codeEvidence'][0]['sha256'] = sha256_hex(f['codeEvidence'][0]['code'])
    scan = 'scan_' + '1' * 24
    def doc(kind, **kw):
        return dict(documentType='hermes-security.' + kind, schemaVersion='1.0', scanId=scan, **kw)
    manifest = doc('scan-manifest', producer={'name': 'hermes-security', 'version': '0.1.0'}, methodologyVersion='hermes-security/method-1', mode='standard', safetyLevel='static', status='completed', target={'root': '/example/app', 'repoKey': '1'*24, 'kind': 'directory', 'revision': None, 'dirty': False, 'snapshotDigest': 'sha256:'+'2'*64, 'scope': [], 'base': None, 'head': None, 'fileCount': 1}, runtime={'provider': None, 'model': None, 'profile': 'default', 'fallbacks': []}, startedAt='2026-10-05T01:00:00Z', finishedAt='2026-10-05T01:01:00Z', counts={}, artifacts={}, supersedes=None)
    coverage = doc('coverage', completeness='partial', files={'total': 1, 'reviewed': 1, 'deferred': 0, 'unsupported': 0, 'excluded': []}, units=[], standards={'owaspTop10': {'version': '2025', 'categories': [{'id': f'A{i:02}:2025', 'state': 'unknown', 'evidence': [], 'reason': 'Not reviewed'} for i in range(1,11)]}, 'asvs': {'version': '5.0.0', 'controls': []}}, workers=[], gaps=['Authentication review is missing.'])
    return dict(manifest=manifest, findings_doc=doc('findings', findings=[f], retained=[]), coverage=coverage, chains_doc=doc('chains', vocabularyVersion='hermes-security/capabilities-1', chains=[], broken=[]))


def write_bundle(path, bundle):
    for rel, data in bundle.items():
        atomic_write(path / rel, data)


def test_finalization_deterministic_reconciles_and_seals(tmp_path):
    data = inputs()
    original = copy.deepcopy(data)
    bundle = finalize_bundle(**data)
    assert bundle == finalize_bundle(**data)
    assert data == original
    m = json.loads(bundle['scan-manifest.json'])
    assert m['status'] == 'partial'
    assert m['counts'] == dict(critical=0, high=1, medium=0, low=0, informational=0, retained=0, chains=0)
    seal = m.pop('seal')
    assert seal['digest'] == 'sha256:' + sha256_hex(canonical_json(m))
    assert bundle['report.md'] == render_markdown(m, data['findings_doc'], data['coverage'], data['chains_doc']).encode()
    write_bundle(tmp_path, bundle)
    assert verify_bundle(tmp_path) == {'ok': True, 'problems': []}


@pytest.mark.parametrize('mutation', ['artifact', 'manifest', 'missing', 'extra', 'symlink'])
def test_verify_tamper(tmp_path, mutation):
    write_bundle(tmp_path, finalize_bundle(**inputs()))
    if mutation == 'artifact':
        (tmp_path/'report.md').write_text('changed')
    elif mutation == 'manifest':
        p = tmp_path/'scan-manifest.json'
        m = json.loads(p.read_text(encoding='utf-8')); m['target']['root'] = '/other'; p.write_text(json.dumps(m))
    elif mutation == 'missing':
        (tmp_path/'coverage.json').unlink()
    elif mutation == 'extra':
        (tmp_path/'exports/extra.csv').write_text('extra')
    else:
        (tmp_path/'report.md').unlink(); make_link(tmp_path/'report.md', outside_system_dir()/'win.ini' if sys.platform=='win32' else '/etc/passwd')
    result = verify_bundle(tmp_path)
    assert not result['ok'] and result['problems']


def test_markdown_layers_and_zero_partial():
    d = inputs()
    text = render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])
    assert 'CWE-' not in text.split('\n\n')[1]
    for term in ['This scan is incomplete', 'Root cause', 'Attack path', 'Evidence', 'src/api.py:40', '```', 'What could make this wrong', 'Not verified:', 'Validation', 'NOT RUN', 'Severity', 'Fix', 'Regression tests', 'OWASP Top 10:2025', 'Not yet reviewed']:
        assert term in text
    assert 'clean' not in text.lower()
    d['findings_doc']['findings'] = []
    text = render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])
    assert 'This scan is incomplete' in text and 'clean' not in text.lower()


def test_markdown_summarizes_workers_and_repeated_gaps():
    d = inputs()
    cov = d['coverage']
    cov['workers'] = ([{'attemptId': f'att_{i:024x}', 'role': 'investigator', 'status': 'accepted'} for i in range(120)]
                      + [{'attemptId': 'att_' + 'f' * 24, 'role': 'validator', 'status': 'rejected'},
                         {'attemptId': 'att_' + 'e' * 24, 'role': 'investigator', 'status': 'missing'}])
    asvs = [f'ASVS control V1.{i}.1 is unknown: no explicit control coverage unit.' for i in range(1, 41)]
    packets = [f'Worker packet pkt_{i:024x} has no accepted result.' for i in range(3)]
    cov['gaps'] = ['Authentication review is missing.', 'ASVS control V9.9.9 is unknown: finding mapped but control not closed.'] + asvs + packets
    text = render_markdown(d['manifest'], d['findings_doc'], cov, d['chains_doc'])
    # One line per kind of result, not one per worker; per-worker detail stays in coverage.json.
    assert '- Review worker' not in text and 'att\\_' not in text
    assert 'Review workers: 120 accepted, 1 rejected, 1 missing. coverage.json lists each one.' in text
    # Repeated gaps collapse to one line each; one-off gaps stay as written.
    assert '- 40 ASVS controls: not yet reviewed — no explicit control coverage unit. coverage.json lists each one.' in text
    assert '- 3 review packets have no accepted result. coverage.json lists each one.' in text
    assert '- ASVS control V9.9.9 is unknown: finding mapped but control not closed.' in text
    assert '- Authentication review is missing.' in text
    assert 'V1.1.1' not in text and 'pkt\\_' not in text
    cov['workers'] = [{'attemptId': 'att_' + 'a' * 24, 'role': 'investigator', 'status': 'accepted'}]
    cov['gaps'] = []
    assert 'Review workers: 1 accepted.' in render_markdown(d['manifest'], d['findings_doc'], cov, d['chains_doc'])


def test_markdown_is_laid_out_for_a_reader():
    d = inputs()
    d['manifest']['target'].update(root='/data/repos/github.com/acme/shop', revision='0123456789abcdef', dirty=True)
    main = d['findings_doc']['findings'][0]
    main['taxonomy']['asvs'] = []
    memory = copy.deepcopy(main)
    memory.update(findingId='hsf_' + 'b' * 24, title='A large upload can exhaust server memory')
    memory['severity'] = {**memory['severity'], 'level': 'medium'}
    memory['taxonomy'] = {**memory['taxonomy'], 'cwe': ['CWE-400']}
    memory['codeEvidence'] = [{**memory['codeEvidence'][0], 'path': 'src/upload.py', 'startLine': 10,
                               'code': ''.join(f'line {i}\n' for i in range(1, 41))}]
    dependency = copy.deepcopy(main)
    dependency.update(findingId='hsf_' + 'c' * 24, ruleId='dependency.known-advisories', codeEvidence=[],
                      title='Dependencies with known vulnerabilities (worst: low)',
                      locations=[{'path': 'uv.lock', 'startLine': 1, 'role': 'manifest'}])
    dependency['severity'] = {**dependency['severity'], 'level': 'low'}
    dependency['taxonomy'] = {**dependency['taxonomy'], 'cwe': ['CWE-1395']}
    d['findings_doc']['findings'] = [main, memory, dependency]
    cov = d['coverage']
    cov['files'] = {'total': 3, 'reviewed': 2, 'deferred': 0, 'unsupported': 1,
                    'excluded': [{'path': '.git', 'reason': 'git_metadata'}]}
    cov['standards']['owaspTop10']['categories'][0].update(
        state='reviewed', reason='Closed by 7 reviews (att_' + 'a' * 24 + ', att_' + 'b' * 24 + ', ...); latest note: Checked.')
    cov['gaps'] = [f'ASVS control V1.{i}.1 is unknown: no explicit control coverage unit.' for i in range(1, 5)] + [
        'file:web/min.js is unsupported: minified bundle.']
    text = render_markdown(d['manifest'], d['findings_doc'], cov, d['chains_doc'])
    # Header names the repository and short commit, not the clone path or a 40-character hash.
    assert text.startswith('# Security review: acme/shop\n\nCommit `0123456` with uncommitted changes · Standard scan · 2026-10-05\n')
    assert '/data/repos' not in text and '0123456789abcdef' not in text
    # The banner says what is still open, in counts.
    assert ('Reviewed 2 of 3 files and 1 of 10 OWASP Top 10 categories. Still open: 4 ASVS checklist controls '
            'with no recorded review; 1 file not fully reviewed.') in text
    # A summary table links every finding; a manifest location has no fake line number.
    assert '**3 findings:** 1 high, 1 medium, 1 low.' in text
    assert '| 1 | High | [' in text and '](#finding-1)' in text and '| `uv.lock` |' in text
    # Findings are grouped by weakness type, worst group first, and numbered in reading order.
    assert text.index('## Access control (1)') < text.index('## Memory and resource exhaustion (1)') \
        < text.index('## Code and dependency integrity (1)')
    assert '<a id="finding-2"></a>\n\n### 2. A large upload can exhaust server memory' in text
    # Severity, location and validation sit under the title; the fix comes before the proof.
    section = text[text.index('### 1. '):text.index('### 2. ')]
    assert '**High** · `src/api.py:40` · Supported by code · Validation: NOT RUN' in section
    assert section.index('**Fix:**') < section.index('#### Root cause') < section.index('#### Evidence')
    assert 'Proof gaps' not in section and 'Not verified:' in section
    assert 'ASVS' not in section.split('Classification:')[1].split('\n')[0]
    assert '```python\n' in section
    # Long excerpts keep their head and tail and name the lines left out.
    memory_section = text[text.index('### 2. '):text.index('### 3. ')]
    assert 'line 12\n' in memory_section and 'line 13\n' not in memory_section and 'line 29\n' in memory_section
    assert '*16 lines left out (lines 22–37); findings.json has the full excerpt.*' in memory_section
    # The dependency finding points to its table instead of repeating generic attack steps.
    dependency_section = text[text.index('### 3. '):]
    assert '[Dependency check](#dependency-check)' in dependency_section
    assert '#### Attack path' not in dependency_section.split('## Coverage')[0]
    # Machine labels and worker IDs stay out of the coverage section.
    assert '- `.git` — Git metadata' in text and 'git\\_metadata' not in text
    assert '| Reviewed | Reviewed by 7 workers. Latest note: Checked. |' in text and 'att\\_' not in text
    assert '- web/min.js: not supported — minified bundle.' in text
    assert text.index('## Code and dependency integrity') < text.index('## Coverage') < text.index('## Limitations')


def test_asvs_is_reported_as_an_informational_count():
    d = inputs()
    d['coverage']['standards']['asvs']['controls'] = (
        [{'id': f'V1.{i}.1', 'state': 'unknown', 'evidence': [], 'reason': 'no explicit control coverage unit'} for i in range(3)]
        + [{'id': 'V2.1.1', 'state': 'reviewed', 'evidence': ['att_x'], 'reason': 'checked'},
           {'id': 'V3.1.1', 'state': 'unknown', 'evidence': [], 'reason': 'finding mapped but control not closed'},
           {'id': 'V4.1.1', 'state': 'not_applicable', 'evidence': [], 'reason': 'no matching surface'}])
    text = render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])
    assert ('ASVS 5.0.0 checklist (informational; it does not decide completeness): 1 of 5 applicable controls '
            'recorded as reviewed (4 not yet reviewed). 1 control relate to reported findings.') not in text
    assert ('ASVS 5.0.0 checklist (informational; it does not decide completeness): 1 of 5 applicable controls '
            'recorded as reviewed (4 not yet reviewed). 1 control relates to reported findings. '
            'coverage.json lists each control.') in text


def test_reportable_section_validation():
    f = inputs()['findings_doc']['findings'][0]
    assert validate_finding_sections(f) == []
    f['rootCause'] = ' '.join(f['summary'].upper().split())
    assert any('duplicat' in p for p in validate_finding_sections(f))
    d = inputs(); d['findings_doc']['findings'][0] = f
    with pytest.raises(ValueError):
        finalize_bundle(**d)
    f['rootCause'] = 'TODO'
    assert validate_finding_sections(f)


def test_sarif_shape_and_reportable_filter():
    d = inputs()
    rejected = copy.deepcopy(d['findings_doc']['findings'][0]); rejected['evidenceState'] = 'rejected'
    d['findings_doc']['findings'].append(rejected)
    sarif = render_sarif(d['manifest'], d['findings_doc'])
    assert sarif['version'] == '2.1.0' and sarif['$schema'].startswith('https://')
    run = sarif['runs'][0]; driver = run['tool']['driver']
    assert driver['name'] == 'hermes-security' and driver['version']
    assert 'CWE-639' in driver['rules'][0]['properties']['tags']
    assert 'security-severity' in driver['rules'][0]['properties']
    assert len(run['results']) == 1
    r = run['results'][0]
    assert r['level'] == 'error' and r['ruleId'] == driver['rules'][0]['id']
    assert r['properties']['evidenceState'] == 'source_supported'
    assert r['partialFingerprints']['hermesSecurity/v1']
    assert r['locations'][0]['physicalLocation']['region']['startLine'] == 40


def test_exports(tmp_path):
    import csv
    import io
    bundle = finalize_bundle(**inputs()); write_bundle(tmp_path, bundle)
    for fmt, rel in [('md','report.md'), ('json','findings.json'), ('sarif','exports/results.sarif')]:
        content, mime, name = export(tmp_path, fmt)
        assert content == bundle[rel] and mime and name == Path(rel).name
    content, mime, name = export(tmp_path, 'csv')
    rows = list(csv.DictReader(io.StringIO(content.decode())))
    assert list(rows[0]) == ['findingId','title','severity','evidenceState','path','startLine','cwe','owasp','validation']
    assert rows[0]['validation'] == 'NOT_RUN'
    assert name == 'findings.csv' and mime.startswith('text/csv')
    with pytest.raises(ValueError): export(tmp_path, 'xml')
    (tmp_path/'report.md').write_text('tampered')
    with pytest.raises(ValueError): export(tmp_path, 'json')


def test_a_long_excerpt_keeps_its_key_line():
    d = inputs()
    f = d['findings_doc']['findings'][0]
    code = ''.join(f'line {i}\n' for i in range(1, 61))
    f['codeEvidence'] = [{**f['codeEvidence'][0], 'path': 'src/big.py', 'startLine': 100, 'endLine': 159,
                          'keyLine': 140, 'code': code}]
    text = render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])
    # line 41 of the excerpt is file line 140: a 25-line window around it is kept.
    assert 'Key line 140: `line 41`.' in text
    assert 'line 29\n' in text and 'line 53\n' in text and 'line 28\n' not in text and 'line 54\n' not in text
    assert '*28 lines left out (lines 100–127); findings.json has the full excerpt.*' in text
    assert '*7 lines left out (lines 153–159); findings.json has the full excerpt.*' in text
    # Near the end, the window stays full-size; a key line outside the excerpt is ignored.
    f['codeEvidence'][0]['keyLine'] = 159
    end = render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])
    assert 'line 36\n' in end and 'line 60\n' in end and 'line 35\n' not in end
    assert '*35 lines left out (lines 100–134)' in end
    f['codeEvidence'][0]['keyLine'] = 400
    ignored = render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])
    assert 'Key line' not in ignored and 'line 12\n' in ignored and 'line 13\n' not in ignored


def test_findings_sharing_a_weakness_roll_up_into_one_summary_row():
    d = inputs()
    base = d['findings_doc']['findings'][0]
    found = []
    for i, (cwe, level) in enumerate([('CWE-400', 'low'), ('CWE-400', 'medium'), ('CWE-400', 'low'),
                                      ('CWE-613', 'low'), ('CWE-613', 'low')]):
        f = copy.deepcopy(base)
        f.update(findingId=f'hsf_{i:024x}', title=f'Finding {i}')
        f['severity'] = {**f['severity'], 'level': level}
        f['taxonomy'] = {**f['taxonomy'], 'cwe': [cwe]}
        f['remediation'] = {**f['remediation'], 'summary': f'Fix number {i}. More detail.'}
        found.append(f)
    d['findings_doc']['findings'] = found
    text = render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])
    # Three or more with one primary CWE share a row; two stay separate.
    assert ('| 1–3 | Medium to low | [3 findings: CWE-400 (memory and resource exhaustion)](#pattern-1) '
            '| 3 places | One fix per place; see the list. |') in text
    assert '| 4 | Low | [Finding 3](#finding-4) |' in text and '| 5 | Low | [Finding 4](#finding-5) |' in text
    # The pattern lists every place with its fix, and each finding keeps its own full section.
    assert '<a id="pattern-1"></a>\n\n**3 findings: CWE-400 (memory and resource exhaustion).**' in text
    assert '- [1. Finding 1](#finding-1) — `src/api.py:40` — Fix number 1.' in text
    for n in range(1, 6):
        assert f'<a id="finding-{n}"></a>' in text
    assert text.index('<a id="pattern-1">') < text.index('<a id="finding-1">')
