# Modified from Apache-2.0 source schemas/findings.schema.json; schemas/coverage.schema.json; schemas/scan-manifest.schema.json (see NOTICE).
"""Import v1 bundles as new documents, never as byte-identical sealed artifacts.

Return keys are filenames. Original documents/records live in provenance.upstream.
Incomplete upstream findings are retained as inconclusive, never filled with invented
proof. Imported coverage is partial because upstream has no Hermes standards ledger.
"""
from copy import deepcopy
import json
from pathlib import Path
from ..canonical import canonical_json, sha256_hex
from ..errors import ValidationError
from ..domain.identities import scan_id as make_scan_id
from ..domain.models import normalize_candidate, promote_to_finding
from ..domain.validate import validate_document
from ..target.excerpt import redact_secrets
from ..fsutil import is_link


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get('summary') or value.get('rationale') or value.get('why') or ''
    return ''


def _read(path, kind):
    try:
        if is_link(path) or path.stat().st_size > 16 * 1024 * 1024:
            raise ValidationError('upstream artifact is a symlink or exceeds 16 MiB')
        def reject_constant(_):
            raise ValueError('non-finite JSON')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate JSON key')
                result[key] = value
            return result
        doc = json.loads(path.read_text(encoding='utf-8'), parse_constant=reject_constant, object_pairs_hook=unique)
        serialized = canonical_json(doc)
        # Do not silently alter archived evidence or carry literal secrets into
        # provenance. The producer must supply a redacted upstream artifact.
        if redact_secrets(serialized)[1]:
            raise ValidationError('upstream artifact contains credentials; redact before importing')
    except (OSError, ValueError, RecursionError) as exc:
        raise ValidationError(f'cannot read upstream {kind} document') from exc
    if not isinstance(doc, dict) or doc.get('documentType') != 'codex-security.' + kind or doc.get('schemaVersion') != '1.0':
        raise ValidationError(f'not an upstream v1 {kind} document')
    return doc


def import_codex_bundle(bundle_dir: Path) -> dict:
    bundle_dir = Path(bundle_dir)
    originals = {kind: _read(bundle_dir / (kind + '.json'), kind) for kind in ('findings', 'coverage', 'scan-manifest')}
    scan = originals['scan-manifest'].get('scan', {})
    old_id = scan.get('id')
    if not old_id or any(originals[k].get('scanId') != old_id for k in ('findings', 'coverage')):
        raise ValidationError('upstream cross-scan bundle')
    sid = make_scan_id('upstream-v1', originals)
    target = scan.get('target', {})
    repo_key = sha256_hex(canonical_json([target.get('targetId'), target.get('remote'), target.get('displayName')]))[:24]
    def wrap(kind, **fields):
        return {'documentType': 'hermes-security.' + kind, 'schemaVersion': '1.0', 'scanId': sid, **fields}
    findings = wrap('findings', findings=[], retained=[], provenance={'upstream': deepcopy(originals['findings'])})
    for raw in originals['findings'].get('findings', []):
        try:
            attack = raw.get('attackPath') or {}
            flow = attack.get('dataFlow') or attack.get('dataflow') or attack.get('data_flow') or {}
            if not isinstance(flow, dict):
                flow = {'summary': flow}
            severity = raw.get('severity', {})
            rem = raw.get('remediation')
            candidate = {
                'ruleId': raw['ruleId'], 'identity': deepcopy(raw['identity']), 'title': raw['title'], 'summary': raw['summary'],
                'rootCause': _text(raw.get('rootCause') or raw.get('root_cause')),
                'attackPath': {'source': _text(flow.get('source')), 'sink': _text(flow.get('sink')), 'steps': attack.get('steps') or ([_text(attack.get('summary') or flow.get('summary'))] if _text(attack.get('summary') or flow.get('summary')) else []), 'controls': attack.get('controls', []), 'assumptions': attack.get('assumptions', [])},
                'severity': {'level': severity['level'], 'rationale': severity.get('rationale', ''), 'reachability': _text(attack.get('reachability')), 'impact': _text(attack.get('impact')), 'likelihood': _text(attack.get('likelihood')), 'prerequisites': attack.get('preconditions', [])},
                'confidence': deepcopy(raw['confidence']),
                'taxonomy': {**deepcopy(raw['taxonomy']), 'owasp': [], 'asvs': []},
                'locations': [{k: v for k, v in loc.items() if k in ('path', 'startLine', 'endLine', 'role')} for loc in raw['locations']],
                'remediation': {'summary': _text(rem), 'regressionTests': raw.get('remediationTests', [])},
                'evidenceState': 'source_supported', 'source': 'worker',
                'provenance': {'methodologyVersion': 'hermes-security/method-1', 'workerAttemptIds': [], 'sourceCandidateIds': [], 'supersedes': raw.get('findingId'), 'upstream': deepcopy(raw)},
            }
            candidate['codeEvidence'] = []
            for entry in raw.get('codeEvidence', []):
                evidence = {k: v for k, v in entry.items() if k in ('id', 'label', 'path', 'startLine', 'endLine', 'code', 'explanation')}
                evidence.setdefault('endLine', evidence['startLine'])
                evidence['sha256'] = sha256_hex(evidence['code'])
                candidate['codeEvidence'].append(evidence)
            candidate = normalize_candidate(candidate, scan_id=sid)
            try:
                findings['findings'].append(promote_to_finding(candidate, scan_id=sid, repo_key=repo_key))
            except ValidationError:
                candidate['evidenceState'] = 'inconclusive'
                candidate['proofGaps'].append('Upstream record lacks complete Hermes finding sections; review before promotion.')
                # Retained partial candidates omit missing sections rather than fabricate text.
                for key in ('rootCause',):
                    if not candidate.get(key):
                        candidate.pop(key, None)
                if not candidate['attackPath']['steps']:
                    candidate['attackPath'].pop('steps')
                if not candidate['severity']['rationale']:
                    candidate['severity'].pop('rationale')
                findings['retained'].append(candidate)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValidationError('upstream finding has malformed required fields') from exc
    coverage = wrap('coverage', completeness='partial', files={'total': 0, 'reviewed': 0, 'deferred': 0, 'unsupported': 0, 'excluded': []}, units=[], standards={'owaspTop10': {'version': '2025', 'categories': []}, 'asvs': {'version': '5.0.0', 'controls': []}}, workers=[], gaps=['Imported bundle does not establish Hermes inventory or standards coverage.'], provenance={'upstream': deepcopy(originals['coverage'])})
    chains = wrap('chains', vocabularyVersion='hermes-security/capabilities-1', chains=[], broken=[])
    counts = {k: sum(f['severity']['level'] == k for f in findings['findings']) for k in ('critical', 'high', 'medium', 'low', 'informational')}
    counts.update(retained=len(findings['retained']), chains=0)
    snapshot = target.get('snapshotDigest', '').split('sha256:')[-1]
    if len(snapshot) != 64 or any(c not in '0123456789abcdef' for c in snapshot):
        snapshot = sha256_hex(canonical_json(target))
    manifest = wrap('scan-manifest', producer={'name': 'hermes-security', 'version': '0.1.0'}, methodologyVersion='hermes-security/method-1', mode='standard', safetyLevel='static', status='partial', target={'root': '', 'repoKey': repo_key, 'kind': 'git' if target.get('kind') == 'git_worktree' else 'directory', 'revision': target.get('revision'), 'dirty': False, 'snapshotDigest': 'sha256:' + snapshot, 'scope': scan.get('scope', {}).get('includePaths', []), 'base': None, 'head': None, 'fileCount': 0}, runtime={'provider': None, 'model': None, 'profile': 'import', 'fallbacks': []}, startedAt=scan.get('startedAt'), finishedAt=scan.get('completedAt'), counts=counts, artifacts={}, supersedes=old_id, provenance={'upstream': deepcopy(originals['scan-manifest'])})
    bundle = {'findings.json': findings, 'coverage.json': coverage, 'chains.json': chains}
    manifest['artifacts'] = {name: 'sha256:' + sha256_hex(canonical_json(doc)) for name, doc in bundle.items()}
    manifest['seal'] = {'algorithm': 'sha256-canonical', 'digest': 'sha256:' + sha256_hex(canonical_json(manifest)), 'sealedAt': scan.get('sealedAt') or scan.get('completedAt')}
    bundle['scan-manifest.json'] = manifest
    for doc in bundle.values():
        problems = validate_document(doc['documentType'], doc)
        if problems:
            raise ValidationError('imported document is invalid: ' + '; '.join(problems))
    return bundle
