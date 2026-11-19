import json
from pathlib import Path
import pytest
from tests.support import make_link, outside_system_dir
from hermes_security.compat.codex_import import import_codex_bundle
from hermes_security.domain.validate import validate_document
from hermes_security.errors import ValidationError

# Vendored verbatim from the pinned upstream (tests/fixtures/upstream/.../SOURCE.txt); never skip.
UPSTREAM=Path(__file__).resolve().parents[1]/'fixtures'/'upstream'/'codex-security-completed-scan'

def test_vendored_upstream_fixture_present():
    assert UPSTREAM.is_dir(), 'vendored upstream fixture missing: ' + str(UPSTREAM)
    for name in ('findings.json','coverage.json','scan-manifest.json','report.md','LICENSE','SOURCE.txt'):
        assert (UPSTREAM/name).is_file(), name

def test_real_upstream_bundle_is_loss_preserving():
    result=import_codex_bundle(UPSTREAM)
    assert result == import_codex_bundle(UPSTREAM)
    for filename,doc in result.items():
        assert validate_document(doc['documentType'],doc)==[]
        if filename != 'chains.json':
            assert doc['provenance']['upstream'] == json.loads((UPSTREAM/filename).read_text(encoding='utf-8'))
    assert result['coverage.json']['completeness']=='partial'
    retained=result['findings.json']['retained'][0]
    assert retained['evidenceState']=='inconclusive'
    assert retained['provenance']['supersedes'].startswith('csf_')
    assert retained['provenance']['upstream']['severity']['score']==8.1

def test_missing_bundle_rejected(tmp_path):
    with pytest.raises(ValidationError): import_codex_bundle(tmp_path)


@pytest.mark.parametrize('mutation', ['cross-scan', 'non-finite', 'secret', 'duplicate', 'symlink'])
def test_untrusted_upstream_rejected(tmp_path, mutation):
    for name in ('findings.json','coverage.json','scan-manifest.json'):
        (tmp_path/name).write_bytes((UPSTREAM/name).read_bytes())
    path=tmp_path/'findings.json'
    doc=json.loads(path.read_text(encoding='utf-8'))
    if mutation=='cross-scan': doc['scanId']='another'
    if mutation=='non-finite': doc['findings'][0]['severity']['score']=float('nan')
    if mutation=='secret': doc['findings'][0]['summary']='AKIA'+'A'*16
    if mutation in ('cross-scan','non-finite','secret'): path.write_text(json.dumps(doc))
    if mutation=='duplicate': path.write_text(path.read_text(encoding='utf-8').replace('"schemaVersion": "1.0"','"schemaVersion": "1.0", "schemaVersion": "1.0"'))
    if mutation=='symlink':
        path.unlink(); make_link(path, UPSTREAM/'findings.json')
    with pytest.raises(ValidationError): import_codex_bundle(tmp_path)
