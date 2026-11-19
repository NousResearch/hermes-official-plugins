"""Plain-language labels and non-destructive report copy checks.

Checks report author prose, never rewrite quoted source evidence.
"""
from __future__ import annotations

import re
from itertools import combinations

BANNED_WORDS = ('comprehensive', 'robust', 'seamless', 'delve', 'critical insight', 'security posture')
SEVERITIES = ('critical', 'high', 'medium', 'low', 'informational')
REPORTABLE = frozenset({'source_supported', 'runtime_confirmed'})
_LABELS = {
    'candidate': 'Needs review', 'source_supported': 'Supported by code',
    'runtime_confirmed': 'Confirmed by test', 'rejected': 'Not an issue',
    'inconclusive': "Couldn't confirm", 'reviewed': 'Reviewed',
    'not_applicable': 'Not applicable', 'deferred': 'Deferred',
    'unsupported': 'Not supported', 'unknown': 'Not yet reviewed',
    'failed': 'Failed', 'NOT_RUN': 'NOT RUN', 'passed': 'Passed',
    'candidate_chain': 'Possible chain', 'source_supported_chain': 'Chain supported by code',
    'runtime_confirmed_chain': 'Chain confirmed by test', 'broken_chain': 'Chain blocked',
    'partial': 'Incomplete', 'complete': 'Complete', 'canceled': 'Canceled',
    'completed': 'Completed', 'informational': 'Informational',
}


def human_label(state: str) -> str:
    """Display known states without leaking machine-style underscores."""
    return _LABELS.get(state, str(state).replace('_', ' ').capitalize())


def banned_words(text: str) -> list[str]:
    """Return banned phrases present in text, in policy order."""
    return [word for word in BANNED_WORDS if re.search(r'\b' + re.escape(word).replace(r'\ ', r'\s+') + r'\b', text, re.I)]


check_banned_words = banned_words


def title_quality(title: str) -> list[str]:
    """Return actionable issues; this heuristic is not a reportability gate."""
    if not isinstance(title, str) or not title.strip():
        return ['Title is missing.']
    problems = []
    if re.search(r'^(potential|possible|suspected)\b|^issue\s+in\b|\b(weakness|security issue|vulnerability)\s*$', title, re.I):
        problems.append('Title is vague; name the actor, action, and affected asset.')
    if len(title.split()) < 5:
        problems.append('Title needs a concrete effect and affected asset.')
    if len(title) > 180:
        problems.append('Title is too long; move conditions into the summary.')
    if banned_words(title):
        problems.append('Title contains banned copy: ' + ', '.join(banned_words(title)))
    return problems


def _sections(finding: dict) -> dict[str, str]:
    attack = finding.get('attackPath', {})
    if isinstance(attack, dict):
        steps = attack.get('steps', [])
        attack = ' '.join(str(s) for s in steps) if isinstance(steps, list) else ''
    severity = finding.get('severity', {})
    fix = finding.get('remediation', {})
    return {'summary': finding.get('summary', ''), 'rootCause': finding.get('rootCause', ''),
            'attackPath': attack, 'severity.rationale': severity.get('rationale', '') if isinstance(severity, dict) else '',
            'remediation.summary': fix.get('summary', '') if isinstance(fix, dict) else ''}


def distinct_sections(finding: dict) -> list[str]:
    """Return duplicate section diagnostics after case/whitespace normalization."""
    texts = {k: ' '.join(v.casefold().split()) for k, v in _sections(finding).items() if isinstance(v, str) and v.strip()}
    problems = []
    for (a, left), (b, right) in combinations(texts.items(), 2):
        short, long = sorted((left, right), key=len)
        if long.startswith(short) and len(short) / len(long) > .9:
            problems.append(f'{a} and {b} have duplicated text.')
    return problems


def _reportable(findings_doc: dict) -> list[dict]:
    return sorted((f for f in findings_doc.get('findings', []) if f.get('evidenceState') in REPORTABLE),
                  key=lambda f: (SEVERITIES.index(f['severity']['level']), f.get('findingId', ''), f.get('occurrenceId', '')))
