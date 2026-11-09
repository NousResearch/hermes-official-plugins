# Modified from Apache-2.0 source scripts/report_projection.py (see NOTICE).
"""Deterministic layered Markdown. Quoted code lines are byte-for-byte unchanged; a long excerpt
shows its head and tail and names the lines it leaves out."""
from __future__ import annotations

import re
from collections import Counter
from .copy import SEVERITIES, _reportable, human_label
from ..orchestration.standard import SKIP_LABELS
from ..standards.ledger import asvs_summary
from ..timing import duration_text, elapsed_seconds


def _text(value, fallback='Not recorded'):
    """Escape untrusted prose so it cannot create headings, links, or HTML."""
    text = ' '.join(str(value).split()) if value is not None else ''
    text = text or fallback
    return re.sub(r'([\\`*\[\]<>_#|])', r'\\\1', text)


def _bullets(values, fallback='None recorded.'):
    return '\n'.join('- ' + _text(v) for v in values) if values else fallback


_LISTED = ' coverage.json lists each one.'
_STANDARD_GAP = re.compile(r'(OWASP lane|ASVS control) \S+ is (\w+): (.+)')
_PACKET_GAP = re.compile(r'Worker packet \S+ has no accepted result\.')
_FILE_GAP = re.compile(r'file:(\S+) is (\w+): (.+)', re.S)


def _workers_line(workers):
    """One count per worker result; listing every attempt ID tells a reader nothing."""
    counts = Counter(w.get('status') or 'unknown' for w in workers)
    order = ['accepted', 'rejected', 'late', 'missing']
    keys = [k for k in order if counts[k]] + sorted(k for k in counts if k not in order)
    text = 'Review workers: ' + ', '.join(f'{counts[k]} {_text(k)}' for k in keys) + '.'
    return text + (_LISTED if len(workers) > 1 else '')


def _gap_lines(gaps):
    """Gaps with repeats grouped: one line per kind of open standard item, one for unfinished packets."""
    groups = {}   # key -> gap texts, in first-seen order
    for index, gap in enumerate(gaps):
        text = str(gap)
        match = _STANDARD_GAP.fullmatch(text)
        key = (('standard',) + match.groups() if match else ('packet',) if _PACKET_GAP.fullmatch(text)
               else ('single', index))
        groups.setdefault(key, []).append(text)
    out = []
    for key, items in groups.items():
        if len(items) == 1:
            single = _FILE_GAP.fullmatch(items[0])
            out.append(f'{single[1]}: {human_label(single[2]).lower()} — {single[3]}' if single else items[0])
        elif key[0] == 'packet':
            out.append(f'{len(items)} review packets have no accepted result.{_LISTED}')
        else:
            _, label, state, reason = key
            out.append(f'{len(items)} {label}s: {human_label(state).lower()} — {reason}{_LISTED}')
    return out


def _runtimes(check):
    """Runtime lines. The runtime lookup runs on its own, so they appear whatever the package check did."""
    lines = []
    for r in check.get('runtimes', []):
        where = f"{_text(r.get('runtime'))} {_text(r.get('declared'))} in {_text(r.get('path'))}"
        ended = f"support for {_text(r.get('cycle'))} ended {_text(r.get('eolDate'), 'on a date not recorded')}"
        if r.get('status') == 'end_of_life':
            lines.append(f'Runtime past end of life: {where} ({ended}).')
        elif r.get('status') == 'allows_end_of_life':
            lines.append(f'Runtime minimum is past end of life: {where} still permits it ({ended}).')
    return lines


def _dependency_section(check):
    """Known advisories are leads: they say a package version is affected, not that this code reaches it."""
    if not check:
        return []
    parts = ['## Dependency check']
    status = check.get('status')
    if status == 'disabled':
        return parts + [f"Not run: {_text(check.get('reason'), 'turned off')}. Known-vulnerable dependencies were not checked."]
    if status == 'no_sources':
        return parts + [f"Nothing to check: {_text(check.get('reason'))}. No package versions in scope were "
                        'checked for published advisories.'] + _runtimes(check)
    if status != 'ok':
        return parts + [f"**Not run:** {_text(check.get('reason'), 'unknown error')}. Known-vulnerable dependencies "
                        'were not checked, so this report says nothing about them.'] + _runtimes(check)
    tool = check.get('tool') or {}
    parts.append(f"{check.get('vulnerablePackages', 0)} of {check.get('packages', 0)} packages in "
                 f"{len(check.get('sources', []))} manifest or lockfile(s) have published advisories "
                 f"({check.get('advisories', 0)} in total), checked with {_text(tool.get('name'))} "
                 f"{_text(tool.get('version'), 'version not recorded')}. Package names and versions were sent to "
                 + _text(', '.join(check.get('network', [])), 'no network service') + '. '
                 'An advisory means the version is affected, not that this code reaches the vulnerable behaviour; '
                 'reviewed reachability appears in the findings above.')
    leads = check.get('leads', [])
    if leads:
        rows = [f"| {_text(l.get('name'))} | {_text(l.get('version'))} | {_text(l.get('path'))} | {human_label(l.get('severity', 'unknown'))} | "
                + _text(', '.join(a.get('id', '') for a in l.get('advisories', [])[:4])
                        + (f" +{len(l['advisories']) - 4} more" if len(l.get('advisories', [])) > 4 else '')) + ' |'
                for l in leads]
        more = check.get('vulnerablePackages', 0) - len(leads)
        parts.append('| Package | Version | Declared in | Worst severity | Advisories |\n|---|---|---|---|---|\n' + '\n'.join(rows))
        if more > 0:
            parts.append(f'{more} more vulnerable packages are listed in the scan data (section dependencies).')
    return parts + _runtimes(check)


_DEPENDENCY_RULE = 'dependency.known-advisories'   # dependencies/finding.py RULE_ID
_CLONE_ROOT = re.compile(r'/repos/[^/]+/([^/]+/[^/]+)/?$')
_LANES_CLOSED = re.compile(r'Closed by (\d+) reviews \([^)]*\); latest note: ', re.S)
_STAMP = re.compile(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})')
_LANGUAGES = {
    'swift': 'swift', 'kt': 'kotlin', 'kts': 'kotlin', 'java': 'java', 'py': 'python', 'js': 'javascript',
    'mjs': 'javascript', 'cjs': 'javascript', 'jsx': 'jsx', 'ts': 'typescript', 'tsx': 'tsx', 'go': 'go',
    'rs': 'rust', 'rb': 'ruby', 'php': 'php', 'cs': 'csharp', 'c': 'c', 'h': 'c', 'cc': 'cpp', 'cpp': 'cpp',
    'hpp': 'cpp', 'm': 'objectivec', 'mm': 'objectivec', 'sh': 'bash', 'bash': 'bash', 'zsh': 'bash',
    'yml': 'yaml', 'yaml': 'yaml', 'json': 'json', 'toml': 'toml', 'xml': 'xml', 'plist': 'xml',
    'html': 'html', 'css': 'css', 'sql': 'sql', 'gradle': 'groovy', 'dart': 'dart', 'lua': 'lua',
    'j2': 'jinja', 'jinja': 'jinja', 'vue': 'vue', 'svelte': 'svelte', 'tf': 'hcl', 'proto': 'protobuf',
}
# Grouping by weakness type, so related fixes sit together. The first CWE that maps decides.
_THEMES = (
    ('Memory and resource exhaustion', {400, 401, 409, 674, 770, 772, 789, 834, 1325}),
    ('Sign-in, sessions and credentials', {287, 288, 290, 294, 306, 384, 522, 523, 613, 620, 640, 798, 1390}),
    ('Access control', {284, 285, 639, 862, 863}),
    ('Sensitive data exposure', {200, 201, 209, 312, 313, 315, 316, 319, 359, 524, 532, 538, 922}),
    ('Links, URLs and outbound requests', {451, 601, 610, 918, 939, 1021}),
    ('Injection', {74, 75, 77, 78, 79, 89, 91, 94, 95, 113, 116, 917, 1336}),
    ('Code and dependency integrity', {345, 347, 353, 494, 829, 1104, 1357, 1395}),
)
_OTHER_THEME = 'Other issues'
MAX_EXCERPT_LINES, EXCERPT_HEAD, EXCERPT_TAIL = 30, 12, 12   # median excerpt is ~10 lines


def _code(value):
    """An inline code span for a path; text that could break out of one is escaped prose instead."""
    text = str(value)
    return f'`{text}`' if text and '`' not in text and '\n' not in text else _text(text)


def _num(n):
    return f'{n:,}' if isinstance(n, int) else _text(n)


def _plural(n, word, many=None):
    return f'{_num(n)} {word if n == 1 else many or word + "s"}'


def _when(start, end):
    """``2026-11-09 00:20 to 01:07 UTC``; falls back to the raw values when they don't parse."""
    a, b = _STAMP.match(str(start or '')), _STAMP.match(str(end or ''))
    if not a or not b:
        return f'{_text(start)} to {_text(end)}'
    return f"{a[1]} {a[2]} to {b[2] if a[1] == b[1] else b[1] + ' ' + b[2]} UTC"


def _repo_name(root):
    match = _CLONE_ROOT.search(str(root or ''))
    if match:
        return match[1], True
    name = str(root or '').rstrip('/').rsplit('/', 1)[-1]
    return name or 'Target not recorded', False


def _theme(finding):
    for cwe in (finding.get('taxonomy') or {}).get('cwe', []):
        match = re.fullmatch(r'CWE-(\d+)', str(cwe))
        number = int(match[1]) if match else None
        for name, ids in _THEMES:
            if number in ids:
                return name
    return _OTHER_THEME


def _where(finding):
    """The first cited location as ``path:line``, else the first recorded location."""
    for item in list(finding.get('codeEvidence') or []) + list(finding.get('locations') or []):
        if item.get('path'):
            line = item.get('startLine') if item.get('role') != 'manifest' else None   # a manifest has no cited line
            return f"{item['path']}:{line}" if line else str(item['path'])
    return ''


def _first_sentence(value, limit=150):
    text = ' '.join(str(value or '').split())
    match = re.match(r'(.+?[.!?])(\s|$)', text)
    text = match[1] if match else text
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _fenced(code, language):
    fence = '`' * max(3, 1 + max((len(m.group()) for m in re.finditer(r'`+', code)), default=0))
    return fence + language + '\n' + code + ('' if code.endswith('\n') else '\n') + fence


def _excerpt(evidence):
    """The quoted lines, unchanged; a long excerpt shows its head and tail and names the lines left out."""
    code = str(evidence.get('code', ''))
    path = str(evidence.get('path') or '')
    language = _LANGUAGES.get(path.rsplit('.', 1)[-1].lower(), '') if '.' in path.rsplit('/', 1)[-1] else ''
    lines = code.splitlines(keepends=True)
    if len(lines) <= MAX_EXCERPT_LINES:
        return _fenced(code, language)
    start = evidence.get('startLine') if isinstance(evidence.get('startLine'), int) else None
    hidden = len(lines) - EXCERPT_HEAD - EXCERPT_TAIL
    span = f' (lines {start + EXCERPT_HEAD}–{start + EXCERPT_HEAD + hidden - 1})' if start else ''
    return (_fenced(''.join(lines[:EXCERPT_HEAD]), language)
            + f'\n\n*{hidden} lines left out{span}; findings.json has the full excerpt.*\n\n'
            + _fenced(''.join(lines[-EXCERPT_TAIL:]), language))


def _caveats(finding):
    """Assumptions, existing controls, counter-evidence and proof gaps as one de-duplicated list."""
    attack = finding.get('attackPath') or {}
    items = ([f'Assumes: {a}' for a in attack.get('assumptions', [])]
             + [f'Existing control: {c}' for c in attack.get('controls', [])]
             + list(finding.get('counterEvidence', []))
             + [f'Not verified: {g}' for g in finding.get('proofGaps', [])])
    return list(dict.fromkeys(' '.join(str(i).split()) for i in items if str(i).strip()))


def _classification(finding):
    taxonomy = finding.get('taxonomy') or {}
    parts = [str(v) for v in taxonomy.get('cwe', [])]   # already 'CWE-n'
    parts += [f'{name} {v}' for name, key in (('OWASP', 'owasp'), ('ASVS', 'asvs')) for v in taxonomy.get(key, [])]
    return 'Classification: ' + _text(' · '.join(parts)) + '.' if parts else ''


def _finding_section(number, f):
    severity = human_label(f['severity']['level'])
    validation = f.get('validation', {})
    where = _where(f)
    header = ' · '.join(x for x in [f'**{severity}**', _code(where) if where else '',
                                     human_label(f.get('evidenceState', 'unknown')),
                                     'Validation: ' + human_label(validation.get('status', 'NOT_RUN'))] if x)
    remediation = f.get('remediation', {})
    parts = [f'<a id="finding-{number}"></a>\n\n### {number}. {_text(f.get("title"))}', header,
             _text(f.get('summary')), '**Fix:** ' + _text(remediation.get('summary'))]
    if remediation.get('regressionTests'):
        parts.append('Regression tests:\n' + _bullets(remediation['regressionTests']))
    if f.get('ruleId') == _DEPENDENCY_RULE:
        return parts + ['Packages, versions and advisories are listed under [Dependency check](#dependency-check).']
    attack = f.get('attackPath', {})
    parts.extend(['#### Root cause', _text(f.get('rootCause')), '#### Attack path',
                  '\n'.join(f'{i}. {_text(step)}' for i, step in enumerate(attack.get('steps', []), 1)) or 'No steps recorded.',
                  '#### Evidence'])
    for evidence in f.get('codeEvidence', []):
        location = f"{evidence.get('path')}:{evidence.get('startLine', '?')}"
        parts.append(f"{_text(evidence.get('label'))} — {_code(location)}\n\n{_excerpt(evidence)}\n\n"
                     + _text(evidence.get('explanation')))
    if not f.get('codeEvidence'):
        parts.append('No source excerpts were recorded; the source evidence is a proof gap.')
    parts.extend(['#### What could make this wrong', _bullets(_caveats(f)), '#### Severity',
                  f"{severity} — {_text(f['severity'].get('rationale'))}",
                  f"Validation: {human_label(validation.get('status', 'NOT_RUN'))} — "
                  f"{_text(validation.get('summary'), 'No static checks recorded.')}"])
    classification = _classification(f)
    return parts + ([classification] if classification else [])


def _banner(manifest, coverage):
    """Why the scan is incomplete, in counts a reader can act on; Limitations has every item."""
    gaps = [str(g) for g in coverage.get('gaps', [])]
    if not gaps:
        return (f"> **This scan is incomplete.** The scan ended with status {_text(manifest.get('status', 'unknown'))}; "
                'not all requested review work was established as complete. '
                'Absence of findings does not establish that the target is safe.')
    files = coverage.get('files', {})
    categories = coverage.get('standards', {}).get('owaspTop10', {}).get('categories', [])
    done = []
    if files.get('total'):
        done.append(f"{_num(files.get('reviewed', 0))} of {_plural(files['total'], 'file')}")
    if categories:
        closed = sum(c.get('state') in {'reviewed', 'not_applicable'} for c in categories)
        done.append(f'{closed} of {len(categories)} OWASP Top 10 categories')
    kinds = Counter()
    others = []
    for gap in gaps:
        standard = _STANDARD_GAP.fullmatch(gap)
        if standard:
            kinds[standard[1]] += 1
        elif _PACKET_GAP.fullmatch(gap):
            kinds['packet'] += 1
        elif _FILE_GAP.fullmatch(gap):
            kinds['file'] += 1
        else:
            others.append(gap)
    still = []
    if kinds['ASVS control']:
        still.append(_plural(kinds['ASVS control'], 'ASVS checklist control') + ' with no recorded review')
    if kinds['OWASP lane']:
        still.append(_plural(kinds['OWASP lane'], 'OWASP category', 'OWASP categories') + ' not closed')
    if kinds['file']:
        still.append(_plural(kinds['file'], 'file') + ' not fully reviewed')
    if kinds['packet']:
        still.append(_plural(kinds['packet'], 'review packet') + ' without a result')
    if len(others) == 1:
        still.append(_text(others[0]).rstrip('.'))
    elif others:
        still.append(f'{len(others)} other gaps')
    text = '> **This scan is incomplete.**'
    if done:
        text += ' Reviewed ' + ' and '.join(done) + '.'
    return (text + ' Still open: ' + '; '.join(still) + '. Details are under Limitations. '
            'Absence of findings does not establish that the target is safe.')


def render_markdown(manifest: dict, findings_doc: dict, coverage: dict, chains_doc: dict) -> str:
    """Project semantic documents without reading the target or generating times."""
    findings = _reportable(findings_doc)
    target = manifest.get('target', {})
    name, cloned = _repo_name(target.get('root'))
    if target.get('revision'):
        version = f"Commit {_code(str(target['revision'])[:7])}" + (' with uncommitted changes' if target.get('dirty') else '')
    elif target.get('snapshotDigest'):
        version = f"Snapshot {_code(str(target['snapshotDigest']).replace('sha256:', '')[:12])}"
    else:
        version = 'Snapshot not recorded'
    about = [version]
    if manifest.get('mode'):
        about.append(f"{human_label(manifest['mode'])} scan")
    stamp = _STAMP.match(str(manifest.get('startedAt') or ''))
    if stamp:
        about.append(stamp[1])
    parts = [f'# Security review: {_text(name)}', ' · '.join(about)]
    if target.get('root') and not cloned:
        parts.append(f"Path: {_code(target['root'])}")
    incomplete = coverage.get('completeness') != 'complete' or manifest.get('status') in {'partial', 'canceled', 'failed', 'interrupted'}
    if incomplete:
        parts.append(_banner(manifest, coverage))

    # Findings, grouped by weakness type: worst theme first, each theme worst finding first.
    themes = {}
    for f in findings:
        themes.setdefault(_theme(f), []).append(f)
    order = sorted(themes, key=lambda t: (min(SEVERITIES.index(f['severity']['level']) for f in themes[t]),
                                          t == _OTHER_THEME, -len(themes[t]), t))
    numbered = [(theme, f) for theme in order for f in themes[theme]]
    totals = Counter(f['severity']['level'] for f in findings)
    parts.append('## Summary')
    if findings:
        counts = ', '.join(f'{totals[s]} {human_label(s).lower()}' for s in SEVERITIES if totals[s])
        parts.append(f"**{_plural(len(findings), 'finding')}:** {counts}.")
        rows = []
        for number, (theme, f) in enumerate(numbered, 1):
            where = _where(f)
            rows.append(f"| {number} | {human_label(f['severity']['level'])} | [{_text(f.get('title'))}](#finding-{number}) "
                        f"| {_code(where.rsplit('/', 1)[-1]) if where else ''} "
                        f"| {_text(_first_sentence(f.get('remediation', {}).get('summary')))} |")
        parts.append('| # | Severity | Finding | Where | Fix |\n|---:|---|---|---|---|\n' + '\n'.join(rows))
    else:
        parts.append('No reportable findings.')
    number = 0
    for theme in order:
        parts.append(f"## {theme} ({len(themes[theme])})")
        for f in themes[theme]:
            number += 1
            parts.extend(_finding_section(number, f))

    dependency = _dependency_section(manifest.get('dependencyCheck'))
    if dependency:
        parts.append('<a id="dependency-check"></a>')
        parts.extend(dependency)

    parts.extend(['## Coverage', f"Review coverage: {human_label(coverage.get('completeness', 'unknown'))}."])
    started, finished = manifest.get('startedAt'), manifest.get('finishedAt')
    if started and finished:
        parts.append(f'Scan time: {duration_text(elapsed_seconds(started, finished))} ({_when(started, finished)}).')
    files = coverage.get('files', {})
    parts.append(f"Files: {_num(files.get('reviewed', 0))} reviewed of {_num(files.get('total', 0))}; "
                 f"{_num(files.get('notApplicable', 0))} not applicable; "
                 f"{_num(files.get('deferred', 0))} deferred; {_num(files.get('unsupported', 0))} unsupported.")
    excluded = files.get('excluded', [])
    out_of_scope = sum(item.get('reason') == 'out_of_scope' for item in excluded)
    if out_of_scope:
        parts.append(f'Outside the requested scope: {_num(out_of_scope)} files (not reviewed).')
    # A focused standard scan skips some files by design; count them instead of listing thousands.
    by_design = Counter(item.get('reason') for item in excluded if str(item.get('reason', '')).startswith('standard_'))
    if by_design:
        kinds = '; '.join(f'{_num(n)} {SKIP_LABELS.get(reason, _text(reason))}' for reason, n in sorted(by_design.items()))
        parts.append(f'Not read by this standard scan, by design: {kinds}. A deep scan reads them; '
                     'coverage.json lists every file.')
    if coverage.get('workers'):
        parts.append(_text(_workers_line(coverage['workers'])))
    listed = [item for item in excluded
              if item.get('reason') != 'out_of_scope' and not str(item.get('reason', '')).startswith('standard_')]
    if listed:
        lines = [f"- {_code(item.get('path'))} — {human_label(str(item.get('reason') or 'unknown'))}" for item in listed[:200]]
        parts.append('Excluded:\n' + '\n'.join(lines))
    if len(listed) > 200:
        parts.append(f'{_num(len(listed) - 200)} more excluded files are listed in coverage.json.')
    categories = coverage.get('standards', {}).get('owaspTop10', {}).get('categories', [])
    by_id = {c['id']: c for c in categories}
    rows = []
    for i in range(1, 11):
        key = f'A{i:02}:2025'; c = by_id.get(key, {})
        # The per-worker IDs in a closed lane's reason are kept in coverage.json, not repeated here.
        reason = _LANES_CLOSED.sub(lambda m: f'Reviewed by {m[1]} workers. Latest note: ', str(c.get('reason') or ''))
        rows.append(f"| {key} | {_text(c.get('name', ''))} | {human_label(c.get('state', 'unknown'))} | {_text(reason or None)} |")
    parts.extend(['### OWASP Top 10:2025', 'Common web application risk categories and the review work completed for each.',
                  '| Category | Name | Review state | Reason |\n|---|---|---|---|\n' + '\n'.join(rows)])
    asvs = asvs_summary(coverage.get('standards', {}))
    if asvs['applicable']:
        states = asvs['states']
        checked = states.get('reviewed', 0)
        rest = ', '.join(f'{n} {human_label(s).lower()}' for s, n in sorted(states.items()) if s != 'reviewed')
        mapped = (f" {_plural(asvs['findingMapped'], 'control')} "
                  f"{'relates' if asvs['findingMapped'] == 1 else 'relate'} to reported findings."
                  if asvs['findingMapped'] else '')
        parts.append(f"ASVS 5.0.0 checklist (informational; it does not decide completeness): {_num(checked)} of "
                     f"{_plural(asvs['applicable'], 'applicable control')} recorded as reviewed"
                     + (f' ({rest})' if rest else '') + f'.{mapped} coverage.json lists each control.')
    units = coverage.get('units', [])
    closed = sum(u.get('state') in {'reviewed', 'not_applicable'} for u in units)
    if closed:
        # Per-unit notes made reports hundreds of KB; open units are listed under Limitations.
        parts.append(f'coverage.json has the review notes for the {_num(closed)} files, categories and controls '
                     'that were closed.')

    parts.append('## Chains')
    for chain in chains_doc.get('chains', []):
        parts.extend([f"### {_text(chain.get('title', chain.get('chainId')))}", _text(chain.get('summary')),
                      human_label(chain.get('disposition', 'candidate_chain')) + ('; conditional.' if chain.get('conditional') else '.')])
        for edge in chain.get('edges', []):
            parts.append(f"{_text(edge.get('from'))} → {_text(edge.get('to'))} ({_text(edge.get('state'))})\n\nAssumptions:\n" +
                         _bullets(edge.get('assumptions', [])) + '\n\nDefeating controls:\n' + _bullets(edge.get('counterEvidence', [])))
    for broken in chains_doc.get('broken', []):
        parts.append('Blocked chain: ' + _text(broken.get('defeatedBy')))
        edge = broken.get('edge', {})
        if edge:
            parts.append(f"{_text(edge.get('from'))} → {_text(edge.get('to'))}\n\nAssumptions:\n" +
                         _bullets(edge.get('assumptions', [])) + '\n\nDefeating controls:\n' +
                         _bullets(edge.get('counterEvidence', [])))
    if not chains_doc.get('chains') and not chains_doc.get('broken'):
        parts.append('No chains recorded.')
    retained = list(findings_doc.get('retained', [])) + [f for f in findings_doc.get('findings', []) if f.get('evidenceState') not in {'source_supported', 'runtime_confirmed'}]
    parts.extend(['## Retained and rejected candidates', f'{len(retained)} candidates retained outside the reported findings.'])
    for f in retained:
        reason = f.get('reason') or f.get('rejectionReason') or f.get('summary') or 'No reason recorded.'
        parts.append(f"- {_text(f.get('title', f.get('candidateId')))}: {human_label(f.get('evidenceState', 'candidate'))} — {_text(reason)}")
    gaps = coverage.get('gaps') or ([f"The scan ended with status {manifest.get('status', 'unknown')}; not all requested review work was established as complete."] if incomplete else [])
    parts.extend(['## Limitations', _bullets(_gap_lines(gaps), 'No coverage gaps were recorded.'),
                  'Source review alone does not establish runtime exploitability. Validation that was not performed is marked NOT RUN.'])
    return '\n\n'.join(parts) + '\n'
