"""Small, deliberately bounded JSON Schema 2020-12 subset (stdlib only).

No remote references, coercion, or target-code execution. Problems contain paths,
not user values, so rejected payloads do not leak secrets into error messages.
"""
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from ..canonical import canonical_json

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / 'schemas'
_DOCUMENTS = {f'hermes-security.{name}': name for name in ('findings', 'coverage', 'scan-manifest', 'chains')}
_DOCUMENTS.update({'worker-result': 'worker-result', 'validation-receipt': 'validation'})


def _json_problems(value, path='$', depth=0):
    if depth > 100:
        return [f'{path}: nesting limit exceeded']
    if value is None or type(value) in (str, bool, int):
        return []
    if type(value) is float:
        return [] if math.isfinite(value) else [f'{path}: non-finite number']
    if type(value) is list:
        return [p for i, v in enumerate(value) for p in _json_problems(v, f'{path}[{i}]', depth + 1)]
    if type(value) is dict:
        problems = []
        for k, v in value.items():
            if type(k) is not str:
                problems.append(f'{path}: object key must be a string')
            problems.extend(_json_problems(v, f'{path}.{k}' if type(k) is str else path, depth + 1))
        return problems
    return [f'{path}: not a JSON value']


def _is_type(value, kind):
    return {'object': type(value) is dict, 'array': type(value) is list,
            'string': type(value) is str, 'boolean': type(value) is bool,
            'integer': type(value) is int or (type(value) is float and math.isfinite(value) and value.is_integer()),
            'number': type(value) in (int, float) and (type(value) is int or math.isfinite(value)),
            'null': value is None}.get(kind, False)


def _equal(a, b):
    # JSON booleans must not compare equal to numeric 0/1.
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    return a == b


def _validate(value, schema, root, path='$'):
    if schema is True:
        return []
    if schema is False:
        return [f'{path}: forbidden value']
    problems = []
    if '$ref' in schema:
        reference = schema['$ref']
        if not reference.startswith('#/$defs/'):
            return [f'{path}: unsupported schema reference']
        target = root
        try:
            for segment in reference[2:].split('/'):
                target = target[segment.replace('~1', '/').replace('~0', '~')]
        except (KeyError, TypeError):
            return [f'{path}: unresolved schema reference']
        problems.extend(_validate(value, target, root, path))
    kinds = schema.get('type')
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(_is_type(value, k) for k in kinds):
            return problems + [f'{path}: expected {" or ".join(kinds)}']
    if 'const' in schema and not _equal(value, schema['const']):
        problems.append(f'{path}: incorrect constant')
    if 'enum' in schema and not any(_equal(value, v) for v in schema['enum']):
        problems.append(f'{path}: value outside enumeration')
    for keyword in ('anyOf', 'oneOf'):
        if keyword in schema:
            matches = sum(not _validate(value, branch, root, path) for branch in schema[keyword])
            if matches == 0 or (keyword == 'oneOf' and matches != 1):
                problems.append(f'{path}: does not satisfy {keyword}')
    if isinstance(value, dict):
        for key in schema.get('required', []):
            if key not in value:
                problems.append(f'{path}.{key}: required')
        properties = schema.get('properties', {})
        extra = schema.get('additionalProperties', True)
        for key, item in value.items():
            if key in properties:
                problems.extend(_validate(item, properties[key], root, f'{path}.{key}'))
            elif extra is False:
                problems.append(f'{path}.{key}: unknown field')
            elif isinstance(extra, dict):
                problems.extend(_validate(item, extra, root, f'{path}.{key}'))
    if isinstance(value, list):
        for keyword, fail in [('minItems', len(value) < schema.get('minItems', 0)), ('maxItems', len(value) > schema.get('maxItems', math.inf))]:
            if fail:
                problems.append(f'{path}: {keyword} violated')
        if 'items' in schema:
            for i, item in enumerate(value):
                problems.extend(_validate(item, schema['items'], root, f'{path}[{i}]'))
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            problems.append(f'{path}: minLength violated')
        if len(value) > schema.get('maxLength', math.inf):
            problems.append(f'{path}: maxLength violated')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            problems.append(f'{path}: pattern mismatch')
    if type(value) in (int, float):
        if value < schema.get('minimum', -math.inf):
            problems.append(f'{path}: minimum violated')
        if value > schema.get('maximum', math.inf):
            problems.append(f'{path}: maximum violated')
    return problems


def validate_schema(value, schema):
    """Validate an in-memory schema using only the documented subset."""
    problems = _json_problems(value)
    return problems or _validate(value, schema, schema)


@lru_cache(maxsize=6)
def _load_schema(name):
    return json.loads((_SCHEMA_DIR / f'{name}.schema.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def _schema_field_names():
    names = set()
    def walk(node):
        if isinstance(node, dict):
            names.update(k for k in node.get('properties', {}) if isinstance(k, str))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    for path in _SCHEMA_DIR.glob('*.schema.json'):
        walk(json.loads(path.read_text(encoding='utf-8')))
    return frozenset(names)


def public_problems(problems, limit=5):
    """Problem paths safe to echo: only field names our own schemas declare.

    Payload-supplied keys (e.g. an unknown field named like a credential) become <field>.
    Messages after the colon are validator constants, never payload values.
    """
    known = _schema_field_names()
    out = []
    for problem in problems[:limit]:
        path, _, message = problem.partition(': ')
        parts = re.findall(r'\.([^.\[]+)|(\[\d+\])', path[1:] if path.startswith('$') else path)
        safe = '$' + ''.join(index or ('.' + name if name in known else '.<field>') for name, index in parts)
        out.append(safe + ': ' + message if message else safe)
    if len(problems) > limit:
        out.append(f'{len(problems) - limit} more')
    return out


def validate_document(document_type: str, doc: dict) -> list[str]:
    if document_type not in _DOCUMENTS:
        return ['$: unknown document type']
    if document_type == 'worker-result':
        return validate_worker_result(doc)
    return validate_schema(doc, _load_schema(_DOCUMENTS[document_type]))


def validate_worker_result(payload) -> list[str]:
    problems = _json_problems(payload)
    if problems:
        return problems
    try:
        size = len(canonical_json(payload).encode('utf-8'))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return ['$: invalid JSON payload']
    if size > 2 * 1024 * 1024:
        problems.append('$: payload exceeds 2 MiB')
    def walk(value, path='$', key=None):
        if isinstance(value, str) and len(value) > (4000 if key == 'code' else 20000):
            problems.append(f'{path}: string limit exceeded')
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, f'{path}.{k}', k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f'{path}[{i}]', key)
    walk(payload)
    return problems + _validate(payload, _load_schema('worker-result'), _load_schema('worker-result'))


def check_finding_sections(finding) -> list[str]:
    """Check independent substantive sections, without inventing missing prose."""
    if not isinstance(finding, dict):
        return ['$: expected finding object']
    attack = finding.get('attackPath') or {}
    steps = attack.get('steps', []) if isinstance(attack, dict) else []
    severity = finding.get('severity') or {}
    remediation = finding.get('remediation') or {}
    sections = {'summary': finding.get('summary'), 'rootCause': finding.get('rootCause'),
                'attackPath': ' '.join(steps) if isinstance(steps, list) and all(isinstance(s, str) for s in steps) else None,
                'severity.rationale': severity.get('rationale') if isinstance(severity, dict) else None,
                'remediation.summary': remediation.get('summary') if isinstance(remediation, dict) else None}
    normalized = {}
    problems = []
    for name, value in sections.items():
        text = ' '.join(value.casefold().split()) if isinstance(value, str) else ''
        if not text:
            problems.append(f'{name}: empty section')
        elif re.search(r'\b(?:todo|tbd|n/a|same as above)\b', text) or text in ('na', 'none', 'unknown', 'not applicable'):
            problems.append(f'{name}: placeholder section')
        else:
            normalized[name] = text
    names = list(normalized)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            short, long = sorted((normalized[a], normalized[b]), key=len)
            if short == long or (long.startswith(short) and len(short) / len(long) > .9):
                problems.append(f'{a} and {b}: duplicated sections')
    return problems


def check_cross_refs(scan_id, findings_doc, chains_doc) -> list[str]:
    problems = []
    for name, doc in [('findings', findings_doc), ('chains', chains_doc)]:
        if doc.get('scanId') != scan_id:
            problems.append(f'{name}.scanId: cross-scan reference')
    findings = findings_doc.get('findings', [])
    from .identities import occurrence_id
    for finding in findings:
        locations = finding.get('locations', [])
        if locations and finding.get('occurrenceId') != occurrence_id(scan_id, finding.get('findingId'), locations[0]):
            problems.append('finding.occurrenceId: cross-scan or invalid occurrence')
    by_id = {f.get('findingId'): f for f in findings}
    if len(by_id) != len(findings):
        problems.append('findings: duplicate finding IDs')
    evidence = {f"{f.get('findingId')}#{e.get('id')}" for f in findings for e in f.get('codeEvidence', [])}
    for chain in chains_doc.get('chains', []) + chains_doc.get('broken', []):
        ids = chain.get('findingIds', [])
        for fid in ids:
            if fid not in by_id:
                problems.append('chain.findingIds: unresolved or cross-scan finding')
        for edge in chain.get('edges', []) + ([chain['edge']] if 'edge' in chain else []):
            for side in ('from', 'to'):
                if edge.get(side) not in by_id or edge.get(side) not in ids:
                    problems.append(f'edge.{side}: unresolved or cross-scan finding')
            for reference in edge.get('evidenceRefs', []):
                if reference not in evidence:
                    problems.append('edge.evidenceRefs: unresolved evidence')
    return problems
