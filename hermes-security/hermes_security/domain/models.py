"""JSON-native domain helpers; normalization never invents supporting evidence."""
from copy import deepcopy
from typing import TypedDict, NotRequired
from ..errors import ValidationError
from .identities import candidate_id, finding_id, occurrence_id, primary_fingerprint
from .validate import check_finding_sections, validate_document


class Location(TypedDict):
    path: str
    startLine: int
    endLine: NotRequired[int]
    role: NotRequired[str]


class Candidate(TypedDict, total=False):
    candidateId: str
    ruleId: str
    identity: dict
    locations: list[Location]
    evidenceState: str
    source: str


class Finding(Candidate, total=False):
    findingId: str
    occurrenceId: str
    fingerprints: dict


def normalize_candidate(raw: dict, *, scan_id) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError('candidate must be an object')
    result = deepcopy(raw)
    try:
        location = result['locations'][0]
        result['candidateId'] = candidate_id(scan_id, result['ruleId'], result['identity']['anchor'], location['path'], location['startLine'])
    except (KeyError, IndexError, TypeError) as exc:
        raise ValidationError('candidate requires ruleId, identity.anchor and a primary location') from exc
    defaults = {'proofGaps': [], 'counterEvidence': [], 'validation': {'status': 'NOT_RUN', 'level': 'static', 'receiptIds': [], 'summary': ''},
                'source': 'worker', 'evidenceState': 'candidate', 'codeEvidence': [],
                'capabilities': {'preconditions': [], 'effects': []}, 'secret': None, 'diffAttribution': None,
                'provenance': {'methodologyVersion': 'hermes-security/method-1', 'workerAttemptIds': [], 'sourceCandidateIds': [], 'supersedes': None}}
    for key, value in defaults.items():
        result.setdefault(key, value)
        if isinstance(value, dict) and isinstance(result[key], dict):
            for nested_key, nested_value in value.items():
                result[key].setdefault(nested_key, deepcopy(nested_value))
    return result


def promote_to_finding(candidate, *, scan_id, repo_key) -> dict:
    result = normalize_candidate(candidate, scan_id=scan_id)
    cid = result.pop('candidateId')
    result.pop('source', None)
    if result['evidenceState'] not in ('source_supported', 'runtime_confirmed'):
        raise ValidationError('only supported candidates can be promoted')
    if result['evidenceState'] == 'runtime_confirmed':
        validation = result.get('validation', {})
        if not isinstance(validation, dict) or validation.get('status') != 'passed' or validation.get('level') not in ('local-safe', 'active-authorized') or not validation.get('receiptIds'):
            raise ValidationError('runtime confirmation requires a passed runtime receipt')
    # Paired controls are verified by validation.next_evidence_state, not inferred here.
    result['findingId'] = finding_id(repo_key, result['ruleId'], result['identity']['anchor'])
    result['occurrenceId'] = occurrence_id(scan_id, result['findingId'], result['locations'][0])
    result['fingerprints'] = {'algorithm': 'hermes-security/v1', 'primary': primary_fingerprint(repo_key, result['ruleId'], result['identity']['anchor'])}
    problems = validate_document('hermes-security.findings', {'documentType': 'hermes-security.findings', 'schemaVersion': '1.0', 'scanId': scan_id, 'findings': [result]})
    problems.extend(check_finding_sections(result))
    if problems:
        raise ValidationError('; '.join(problems))
    if cid not in result['provenance']['sourceCandidateIds']:
        result['provenance']['sourceCandidateIds'].append(cid)
    return result
