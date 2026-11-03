"""Boundary and failure checks for report finalization."""
import copy
import json
import sys
from pathlib import Path
from types import ModuleType
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import pytest
from hermes_security.reports import finalize_bundle, render_markdown, render_sarif, export, validate_finding_sections, verify_bundle
from hermes_security.canonical import canonical_json, sha256_hex
from test_report_bundle import inputs, write_bundle


def test_bad_excerpt_digest_is_rejected():
    d = inputs(); d['findings_doc']['findings'][0]['codeEvidence'][0]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='excerpt'):
        finalize_bundle(**d)


def test_partial_steps_cannot_be_placeholder():
    f = inputs()['findings_doc']['findings'][0]
    f['attackPath']['steps'] = ['TODO: explain the source', 'Request another invoice.']
    assert validate_finding_sections(f)
    f['rootCause'] = 'Same as above'
    assert any('rootCause' in p for p in validate_finding_sections(f))


def test_false_complete_claim_downgraded():
    d = inputs(); d['coverage']['completeness'] = 'complete'; d['coverage']['gaps'] = []
    bundle = finalize_bundle(**d)
    assert json.loads(bundle['scan-manifest.json'])['status'] == 'partial'
    assert json.loads(bundle['coverage.json'])['completeness'] == 'partial'
    assert 'This scan is incomplete' in bundle['report.md'].decode()


def test_complete_and_stopped_status():
    d = inputs(); d['coverage']['completeness'] = 'complete'; d['coverage']['gaps'] = []
    for c in d['coverage']['standards']['owaspTop10']['categories']:
        c['state'] = 'reviewed'; c['reason'] = 'Inspected the applicable routes.'
    assert json.loads(finalize_bundle(**d)['scan-manifest.json'])['status'] == 'completed'
    for status in ['canceled', 'failed']:
        d['manifest']['status'] = status
        bundle = finalize_bundle(**d)
        assert json.loads(bundle['scan-manifest.json'])['status'] == status
        assert 'This scan is incomplete' in bundle['report.md'].decode()


def test_nonreportable_retained_and_counts():
    d = inputs(); f = copy.deepcopy(d['findings_doc']['findings'][0])
    f.update(evidenceState='inconclusive', summary='Could not establish exposure.')
    for key in ('findingId', 'occurrenceId', 'fingerprints'):
        f.pop(key)
    d['findings_doc']['findings'].append(f)
    bundle = finalize_bundle(**d)
    assert json.loads(bundle['scan-manifest.json'])['counts']['retained'] == 1
    doc = json.loads(bundle['findings.json'])
    assert len(doc['findings']) == len(doc['retained']) == 1


def test_validator_lazy_import_and_fallback(monkeypatch):
    stub = ModuleType('hermes_security.domain.validate'); seen = []
    def validate(kind, doc):
        seen.append(kind)
        return ['schema rejected'] if kind.endswith('coverage') else []
    stub.validate_document = validate
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    with pytest.raises(ValueError, match='schema rejected'): finalize_bundle(**inputs())
    assert 'hermes-security.coverage' in seen
    monkeypatch.setitem(sys.modules, stub.__name__, None)
    assert finalize_bundle(**inputs())
    bad = inputs(); bad['coverage']['schemaVersion'] = '2.0'
    with pytest.raises(ValueError, match='schemaVersion'): finalize_bundle(**bad)


@pytest.mark.parametrize('level,expected', [('critical','error'), ('high','error'), ('medium','warning'), ('low','note'), ('informational','note')])
def test_all_sarif_levels(level, expected):
    d = inputs(); d['findings_doc']['findings'][0]['severity']['level'] = level
    sarif = render_sarif(d['manifest'], d['findings_doc'])
    assert sarif['runs'][0]['results'][0]['level'] == expected


def test_csv_formula_and_escaped_paths(tmp_path):
    d = inputs(); f = d['findings_doc']['findings'][0]
    f['title'] = '=HYPERLINK("https://invalid.example")'
    f['locations'][0]['path'] = 'src/my file.py'
    write_bundle(tmp_path, finalize_bundle(**d))
    import csv, io
    content, _, _ = export(tmp_path, 'csv')
    row = next(csv.DictReader(io.StringIO(content.decode())))
    assert row['title'].startswith("'=")
    sarif = json.loads(export(tmp_path, 'sarif')[0])
    assert sarif['runs'][0]['results'][0]['locations'][0]['physicalLocation']['artifactLocation']['uri'] == 'src/my%20file.py'


def test_manifest_seal_time_and_path_attack(tmp_path):
    bundle = finalize_bundle(**inputs()); manifest = json.loads(bundle['scan-manifest.json'])
    manifest['seal']['sealedAt'] = '2020-01-01T00:00:00Z'
    bundle['scan-manifest.json'] = canonical_json(manifest).encode(); write_bundle(tmp_path, bundle)
    assert not verify_bundle(tmp_path)['ok']
    manifest['artifacts']['../escape'] = 'sha256:' + '0'*64
    manifest['seal']['digest'] = 'sha256:' + sha256_hex(canonical_json({k: v for k,v in manifest.items() if k != 'seal'}))
    (tmp_path/'scan-manifest.json').write_text(canonical_json(manifest))
    assert any('Unsafe artifact' in p for p in verify_bundle(tmp_path)['problems'])


def test_fenced_code_is_exact_and_prose_is_escaped():
    d = inputs(); f = d['findings_doc']['findings'][0]
    code = '```\n<script>not prose</script>\n'
    f['codeEvidence'][0]['code'] = code
    f['rootCause'] = '<script>bad</script>'
    text = render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])
    assert '````\n' + code + '````' in text
    assert '\\<script\\>bad\\</script\\>' in text


def test_chain_edge_details():
    d = inputs()
    d['chains_doc']['chains'] = [{'title':'Session theft enables billing access', 'summary':'A stolen session reaches invoice data.', 'conditional':True, 'edges':[{'from':'a','to':'b','state':'speculative','assumptions':['The session remains valid.'],'counterEvidence':['Session revocation blocks this edge.']}]}]
    text = render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])
    assert 'a → b' in text and 'The session remains valid.' in text and 'Session revocation blocks this edge.' in text
