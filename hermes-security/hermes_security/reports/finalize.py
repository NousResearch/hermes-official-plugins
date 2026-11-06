# Modified from Apache-2.0 source scripts/finalize_scan_contract.py (see NOTICE).
"""Validate canonical semantics, project artifacts, and seal their exact bytes.

The seal is an integrity checksum, not an authenticated signature. An attacker
able to replace both content and all checksums needs an external trusted digest.
"""
from __future__ import annotations

import copy
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

from ..canonical import canonical_json, sha256_hex
from ..errors import ValidationError
from .copy import REPORTABLE, SEVERITIES, _sections, distinct_sections
from .markdown import render_markdown
from .sarif import render_sarif
from ..fsutil import is_link

ARTIFACTS = frozenset({'findings.json', 'coverage.json', 'chains.json', 'report.md', 'exports/results.sarif'})


class ReportError(ValidationError, ValueError):
    """Invalid report semantics or bundle integrity failure."""


def validate_finding_sections(finding: dict) -> list[str]:
    """Check required narrative sections without mistaking tests for evidence."""
    if not isinstance(finding, dict):
        return ['Finding must be an object.']
    problems = distinct_sections(finding)
    for key, value in _sections(finding).items():
        if not isinstance(value, str) or not value.strip():
            problems.append(f'{key} is missing or empty.')
        elif (re.fullmatch(r'\s*(?:todo|tbd|placeholder|n/?a|none|unknown|not available|pending|same as above|not applicable|\.\.\.|<[^>]+>)[.!]?\s*', value, re.I)
              or re.search(r'\b(?:TODO|TBD|same as above)\b', value)):
            problems.append(f'{key} contains placeholder text.')
    return problems


def _bytes(doc):
    return canonical_json(doc).encode('utf-8')


def _structural(kind, doc):
    required = {
        'findings': {'findings'}, 'coverage': {'completeness', 'files', 'units', 'standards', 'gaps'},
        'chains': {'chains', 'broken', 'vocabularyVersion'},
        'scan-manifest': {'producer', 'target', 'mode', 'safetyLevel', 'status', 'startedAt', 'finishedAt', 'counts', 'artifacts', 'seal'},
    }
    issues = []
    if not isinstance(doc, dict):
        return [f'{kind} must be an object']
    if doc.get('documentType') != 'hermes-security.' + kind:
        issues.append(f'{kind}.documentType is invalid')
    if doc.get('schemaVersion') != '1.0':
        issues.append(f'{kind}.schemaVersion is invalid')
    for key in sorted(required[kind] | {'scanId'}):
        if key not in doc:
            issues.append(f'{kind}.{key} is required')
    for key in ('findings', 'retained', 'chains', 'broken', 'units', 'gaps'):
        if key in doc and not isinstance(doc[key], list):
            issues.append(f'{kind}.{key} must be an array')
    return issues


def _coverage_gaps(coverage):
    """Fail closed on a claimed complete review with unfinished ledger rows."""
    gaps = list(coverage.get('gaps', []))
    done = {'reviewed', 'not_applicable'}
    files = coverage.get('files', {})
    if (files.get('deferred', 0) or files.get('unsupported', 0)
            or files.get('reviewed', 0) + len(files.get('excluded', [])) < files.get('total', 0)):
        gaps.append('Some inventory files have not been reviewed.')
    categories = coverage.get('standards', {}).get('owaspTop10', {}).get('categories', [])
    states = {c['id']: c['state'] for c in categories}
    for i in range(1, 11):
        key = f'A{i:02}:2025'
        if states.get(key) not in done:
            gaps.append(f'{key} review is incomplete.')
    for unit in coverage.get('units', []):
        if unit.get('state') not in done:
            gaps.append(f"Review unit {unit.get('unitId', 'unknown')} is incomplete.")
    if any(w.get('status') == 'missing' for w in coverage.get('workers', [])):
        gaps.append('A review worker result is missing.')
    return list(dict.fromkeys(gaps))


def finalize_bundle(*, manifest: dict, findings_doc: dict, coverage: dict, chains_doc: dict) -> dict[str, bytes]:
    """Produce a sealed bundle without I/O or changing input documents.

    Timestamps must be supplied by the caller: identical inputs remain identical
    even when projected later. Domain validation is loaded lazily during assembly.
    """
    manifest, findings_doc, coverage, chains_doc = copy.deepcopy((manifest, findings_doc, coverage, chains_doc))
    try:
        from ..domain.validate import validate_document
    except ImportError:
        validate_document = None
    def validate(kind, doc):
        problems = _structural(kind, doc)
        if validate_document is not None:
            problems.extend(validate_document('hermes-security.' + kind, doc))
        if problems:
            raise ReportError('; '.join(problems))
    # Non-reportable candidates survive in retained rather than disappearing.
    if not isinstance(findings_doc.get('findings'), list) or not isinstance(findings_doc.get('retained', []), list):
        raise ReportError('findings and retained must be arrays')
    reported, retained = [], list(findings_doc.get('retained', []))
    for f in findings_doc['findings']:
        if not isinstance(f, dict):
            raise ReportError('Every finding must be an object')
        if f.get('evidenceState') in REPORTABLE:
            problems = validate_finding_sections(f)
            if problems:
                raise ReportError(f"{f.get('findingId', 'Finding')}: " + '; '.join(problems))
            if f.get('severity', {}).get('level') not in SEVERITIES:
                raise ReportError('Invalid finding severity')
            reported.append(f)
        else:
            retained.append(f)
    findings_doc['findings'], findings_doc['retained'] = reported, retained
    for kind, doc in [('findings', findings_doc), ('coverage', coverage), ('chains', chains_doc)]:
        validate(kind, doc)
        if doc.get('scanId') != manifest.get('scanId'):
            raise ReportError(f'{kind}.scanId does not match manifest')
    # Bind excerpts to their recorded bytes; snapshot membership is the submit
    # boundary's responsibility because finalization deliberately has no target I/O.
    for finding in reported:
        for evidence in finding.get('codeEvidence', []):
            if evidence.get('sha256') != sha256_hex(evidence.get('code', '')):
                raise ReportError('Source excerpt digest mismatch')
    if coverage.get('completeness') == 'complete':
        gaps = _coverage_gaps(coverage)
        if gaps:
            coverage['completeness'] = 'partial'
            coverage['gaps'] = gaps
    if coverage.get('completeness') == 'partial':
        manifest['status'] = 'partial'
    if manifest.get('status') not in {'completed', 'partial', 'canceled', 'failed'}:
        raise ReportError('A terminal scan status is required')
    if not manifest.get('finishedAt'):
        raise ReportError('finishedAt is required for deterministic sealing')
    counts = {level: sum(f['severity']['level'] == level for f in reported) for level in SEVERITIES}
    counts.update(retained=len(retained), chains=len(chains_doc['chains']))
    manifest['counts'] = counts
    bundle = {'findings.json': _bytes(findings_doc), 'coverage.json': _bytes(coverage), 'chains.json': _bytes(chains_doc),
              'report.md': render_markdown(manifest, findings_doc, coverage, chains_doc).encode('utf-8'),
              'exports/results.sarif': _bytes(render_sarif(manifest, findings_doc))}
    manifest.pop('seal', None)
    manifest['artifacts'] = {name: 'sha256:' + sha256_hex(data) for name, data in sorted(bundle.items())}
    manifest['seal'] = {'algorithm': 'sha256-canonical', 'digest': 'sha256:' + sha256_hex(_bytes(manifest)), 'sealedAt': manifest['finishedAt']}
    validate('scan-manifest', manifest)
    bundle['scan-manifest.json'] = _bytes(manifest)
    return bundle


def _read_local(root: Path, rel: str) -> bytes:
    """Refuse absolute/traversing paths, symlinks and non-regular files."""
    path = PurePosixPath(rel)
    if not rel or path.is_absolute() or any(p in {'.', '..', ''} for p in rel.split('/')) or '\\' in rel:
        raise ReportError(f'Unsafe artifact path: {rel}')
    current = root
    if is_link(root) or not root.is_dir():
        raise ReportError('Scan directory is not a regular directory')
    for part in path.parts:
        current = current / part
        if is_link(current):
            raise ReportError(f'Symlink artifact path: {rel}')
    if not stat.S_ISREG(current.stat().st_mode):
        raise ReportError(f'Not a regular artifact: {rel}')
    return current.read_bytes()


def _loads(data):
    def reject(value):
        raise ValueError('Non-finite JSON value: ' + value)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(data, parse_constant=reject, object_pairs_hook=unique)


def verify_bundle(scan_dir: Path) -> dict:
    """Verify manifest seal and artifact bytes, including unexpected exports.

    Unrelated working files outside exports/ are not considered sealed artifacts.
    Verification never follows artifact symlinks or writes to the scan directory.
    """
    root = Path(scan_dir)
    problems = []
    try:
        raw = _read_local(root, 'scan-manifest.json')
        manifest = _loads(raw)
        if not isinstance(manifest, dict):
            raise ReportError('Manifest must be an object')
        if raw != _bytes(manifest):
            problems.append('Manifest bytes are not the canonical sealed representation')
        seal = manifest.get('seal', {})
        unsigned = {k: v for k, v in manifest.items() if k != 'seal'}
        if not isinstance(seal, dict) or seal.get('algorithm') != 'sha256-canonical' or seal.get('digest') != 'sha256:' + sha256_hex(_bytes(unsigned)):
            problems.append('Manifest seal digest mismatch')
        if not isinstance(seal, dict) or seal.get('sealedAt') != manifest.get('finishedAt'):
            problems.append('Manifest seal time mismatch')
        artifacts = manifest.get('artifacts')
        if not isinstance(artifacts, dict):
            raise ReportError('Manifest artifacts must be an object')
        if set(artifacts) != ARTIFACTS:
            problems.append('Manifest artifact set does not match the required bundle')
        for rel, digest in sorted(artifacts.items()):
            try:
                data = _read_local(root, rel)
                if digest != 'sha256:' + sha256_hex(data):
                    problems.append(f'Artifact hash mismatch: {rel}')
            except (OSError, ValueError) as exc:
                problems.append(f'Missing or unsafe artifact {rel}: {exc}')
        exports = root / 'exports'
        if is_link(exports):
            problems.append('Exports directory is a symlink')
        elif exports.exists():
            for directory, dirs, files in os.walk(exports, followlinks=False):
                for name in dirs + files:
                    p = Path(directory) / name
                    rel = p.relative_to(root).as_posix()
                    if is_link(p) or (name in files and rel not in artifacts):
                        problems.append(f'Unexpected export: {rel}')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        problems.append(f'Cannot verify bundle: {exc}')
    return {'ok': not problems, 'problems': problems}
