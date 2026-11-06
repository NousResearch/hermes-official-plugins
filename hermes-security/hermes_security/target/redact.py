"""Secret redaction shared by evidence and built-in detection (no raw values returned)."""
import math
import re
from collections import Counter
from ..canonical import secret_fingerprint

PATTERNS = [
    ('private-key', re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)')),
    ('aws-access-key', re.compile(r'(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])')),
    ('github-token', re.compile(r'(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})')),
    ('slack-token', re.compile(r'xox[baprs]-[A-Za-z0-9-]{16,}')),
    ('stripe-key', re.compile(r'(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}')),
    ('jwt', re.compile(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+')),
]
ASSIGNMENT = re.compile(r'''(?ix)\b(?:password|passwd|secret|token|api[_-]?key)\b["']?\s*[:=]\s*(?:["'](?P<quoted>[^"'\r\n]{8,})["']|(?P<bare>[A-Za-z0-9_./+=!@#$%^&*-]{12,}))''')
CALL = re.compile(r'\s*\(')
# snake_case or CONSTANT_CASE names, optionally dotted: one case, no digits, so never a generated key.
NAME = re.compile(r'(?=.*[_.])(?:[a-z]+(?:_[a-z]+)*|[A-Z]+(?:_[A-Z]+)*)(?:\.(?:[a-z]+(?:_[a-z]+)*|[A-Z]+(?:_[A-Z]+)*))*')
BOUNDARY = re.compile(r'[A-Za-z0-9_./+=-]{20,}')
PREFIX = re.compile(r'(?:AKIA|ASIA|gh[pousr]_|github_pat_|xox[baprs]-|[sr]k_(?:live|test)_|eyJ)')


def _entropy(value):
    n = len(value)
    return -sum((count / n) * math.log2(count / n) for count in Counter(value).values())


def redact_secrets(text):
    if not isinstance(text, str): raise TypeError('text must be str')
    spans = []
    for kind, pattern in PATTERNS:
        spans.extend((m.start(), m.end(), kind) for m in pattern.finditer(text))
    for match in ASSIGNMENT.finditer(text):
        group = 'quoted' if match.group('quoted') is not None else 'bare'
        value = match.group(group)
        if re.fullmatch(r'\[REDACTED:[a-z-]+:sha256:[0-9a-f]{16}\]', value):
            continue
        # `api_key = resolve_key()` is code, not a value; a literal is never followed by a call.
        if group == 'bare' and CALL.match(text, match.end(group)):
            continue
        # `"api_key": explicit_key` names a variable; a bare name is not a value.
        if group == 'bare' and NAME.fullmatch(value):
            continue
        if _entropy(value) >= 3.0:
            spans.append((*match.span(group), 'generic-secret'))
    for match in BOUNDARY.finditer(text):
        # Conservatively redact partial known-prefix runs anywhere: a caller may
        # subsequently select lines/chars, turning an interior run into an edge.
        if PREFIX.match(match.group()):
            spans.append((match.start(), match.end(), 'partial-token'))
    # Merge overlapping detections: never expose the tail of a longer match.
    selected = []
    for start, end, kind in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]), s[2] == 'partial-token')):
        if selected and start < selected[-1][1]:
            old = selected[-1]
            selected[-1] = (old[0], max(end, old[1]), old[2])
        else: selected.append((start, end, kind))
    result = []; records = []; last = 0
    for start, end, kind in selected:
        fingerprint = secret_fingerprint(text[start:end])
        result.extend([text[last:start], f'[REDACTED:{kind}:{fingerprint}]'])
        # Preserve line indices when redacting PEM blocks for excerpt selection.
        result.append('\n' * text[start:end].count('\n'))
        records.append({'type': kind, 'fingerprint': fingerprint, 'line': text.count('\n', 0, start) + 1})
        last = end
    result.append(text[last:])
    return ''.join(result), records
