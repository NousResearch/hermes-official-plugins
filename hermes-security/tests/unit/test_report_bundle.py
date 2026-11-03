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
    f = json.loads((Path(__file__).parents[1] / 'fixtures/reports/invoice.json').read_text())
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
        m = json.loads(p.read_text()); m['target']['root'] = '/other'; p.write_text(json.dumps(m))
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
    for term in ['This scan is incomplete', 'Root cause', 'Attack path', 'Evidence', 'src/api.py:40', '```', 'What could make this wrong', 'Proof gaps', 'Validation', 'NOT RUN', 'Severity', 'Fix', 'Regression tests', 'OWASP Top 10:2025', 'Not yet reviewed']:
        assert term in text
    assert 'clean' not in text.lower()
    d['findings_doc']['findings'] = []
    text = render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])
    assert 'This scan is incomplete' in text and 'clean' not in text.lower()


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
