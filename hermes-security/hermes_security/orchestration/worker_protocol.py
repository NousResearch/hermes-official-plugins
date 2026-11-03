"""Untrusted worker boundary. Candidate failures expose reasons, not input prose."""
from copy import deepcopy
from .. import target
from ..canonical import sha256_hex
from ..domain.models import normalize_candidate
from ..errors import TargetError, ValidationError


def _excerpt_problem(scan, evidence, paths):
    """Say when a mismatch comes from snapshot redaction, so workers cite the redacted text."""
    try:
        actual = target.read_excerpt(scan['root'], evidence['path'], evidence['startLine'],
                                     evidence.get('endLine', evidence['startLine']), inventory_paths=set(paths),
                                     expected_sha256=paths[evidence['path']]['sha256'])
    except (TargetError, OSError, ValueError):
        return 'code excerpt does not match snapshot'
    if '[REDACTED:' in actual:
        return ('code excerpt does not match snapshot: these lines are secret-redacted in the snapshot; '
                'cite them with the [REDACTED:...] markers as security_scan_get returns them')
    return 'code excerpt does not match snapshot'


def check_candidate(raw, *, scan, inventory, attempt_id):
    candidate = normalize_candidate(raw, scan_id=scan['scan_id'])
    if raw.get('candidateId') is not None and raw['candidateId'] != candidate['candidateId']:
        raise ValidationError('candidateId does not match this scan and source identity')
    paths = {entry['path']: entry for entry in inventory}
    for loc in candidate['locations'] + candidate['codeEvidence']:
        path, start = loc.get('path'), loc.get('startLine')
        end = loc.get('endLine', start)
        if path not in paths:
            raise ValidationError('citation path is not in inventory')
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= paths[path]['lines']:
            raise ValidationError('citation line range is outside inventory file')
    for evidence in candidate['codeEvidence']:
        if not target.verify_excerpt(scan['root'], evidence['path'], evidence['startLine'], evidence.get('endLine', evidence['startLine']), evidence.get('code'), inventory_paths=set(paths), expected_sha256=paths[evidence['path']]['sha256']):
            raise ValidationError(_excerpt_problem(scan, evidence, paths))
        evidence['sha256'] = sha256_hex(evidence['code'])
    # A worker may describe source support but cannot claim runtime receipt authority.
    validation = candidate.get('validation', {})
    if validation.get('status', 'NOT_RUN') != 'NOT_RUN' or validation.get('level', 'static') != 'static' or validation.get('receiptIds'):
        raise ValidationError('worker cannot supply runtime validation or receipt authority')
    candidate['source'] = 'worker'
    p = candidate['provenance']
    p['workerAttemptIds'] = [attempt_id]
    p['sourceCandidateIds'] = [candidate['candidateId']]
    p['supersedes'] = None
    return deepcopy(candidate)
