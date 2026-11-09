# Modified from Apache-2.0 source scripts/report_projection.py (see NOTICE).
"""Deterministic layered Markdown; quoted code remains byte-for-byte unchanged."""
from __future__ import annotations

import re
from collections import Counter
from .copy import SEVERITIES, _reportable, human_label
from ..orchestration.standard import SKIP_LABELS
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
            out.append(items[0])
        elif key[0] == 'packet':
            out.append(f'{len(items)} review packets have no accepted result.{_LISTED}')
        else:
            _, label, state, reason = key
            out.append(f'{len(items)} {label}s are {state}: {reason}{_LISTED}')
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


def render_markdown(manifest: dict, findings_doc: dict, coverage: dict, chains_doc: dict) -> str:
    """Project semantic documents without reading the target or generating times."""
    findings = _reportable(findings_doc)
    target = manifest.get('target', {})
    revision = target.get('revision') or target.get('snapshotDigest') or 'Snapshot not recorded'
    parts = [f"# Security review: {_text(target.get('root'))} — {_text(revision)}",
             f"This review identified {len(findings)} reportable finding{'s' if len(findings) != 1 else ''}. "
             'The sections below explain the affected behavior, supporting evidence, uncertainty, and recommended changes.']
    incomplete = coverage.get('completeness') != 'complete' or manifest.get('status') in {'partial', 'canceled', 'failed', 'interrupted'}
    if incomplete:
        gaps = coverage.get('gaps', [])
        if not gaps:
            gaps = [f"The scan ended with status {manifest.get('status', 'unknown')}; not all requested review work was established as complete."]
        # Keep the banner short; the full gap list is rendered under Limitations.
        grouped = _gap_lines(gaps)
        summary = _text(grouped[0]) if len(grouped) == 1 else 'The coverage gaps are listed under Limitations.'
        parts.append('> **This scan is incomplete.** ' + summary +
                     ' Absence of findings does not establish that the target is safe.')
    totals = Counter(f['severity']['level'] for f in findings)
    parts.extend(['## Severity totals', '| Severity | Findings |\n|---|---:|\n' + '\n'.join(f'| {human_label(s)} | {totals[s]} |' for s in SEVERITIES),
                  '## Coverage', f"Review coverage: {human_label(coverage.get('completeness', 'unknown'))}."])
    started, finished = manifest.get('startedAt'), manifest.get('finishedAt')
    if started and finished:
        parts.append(f'Scan time: {duration_text(elapsed_seconds(started, finished))} '
                     f'({_text(started)} to {_text(finished)}).')
    files = coverage.get('files', {})
    parts.append(f"Files: {files.get('reviewed', 0)} reviewed of {files.get('total', 0)}; "
                 f"{files.get('notApplicable', 0)} not applicable; "
                 f"{files.get('deferred', 0)} deferred; {files.get('unsupported', 0)} unsupported.")
    excluded = files.get('excluded', [])
    out_of_scope = sum(item.get('reason') == 'out_of_scope' for item in excluded)
    if out_of_scope:
        parts.append(f'Outside the requested scope: {out_of_scope} files (not reviewed).')
    # A focused standard scan skips some files by design; count them instead of listing thousands.
    by_design = Counter(item.get('reason') for item in excluded if str(item.get('reason', '')).startswith('standard_'))
    if by_design:
        kinds = '; '.join(f'{n} {SKIP_LABELS.get(reason, _text(reason))}' for reason, n in sorted(by_design.items()))
        parts.append(f'Not read by this standard scan, by design: {kinds}. A deep scan reads them; '
                     'coverage.json lists every file.')
    listed = [item for item in excluded
              if item.get('reason') != 'out_of_scope' and not str(item.get('reason', '')).startswith('standard_')]
    if coverage.get('workers'):
        parts.append(_text(_workers_line(coverage['workers'])))
    for item in listed[:200]:
        parts.append(f"Excluded: {_text(item.get('path'))} — {_text(item.get('reason'))}.")
    if len(listed) > 200:
        parts.append(f'{len(listed) - 200} more excluded files are listed in coverage.json.')
    parts.extend(_dependency_section(manifest.get('dependencyCheck')))
    categories = coverage.get('standards', {}).get('owaspTop10', {}).get('categories', [])
    by_id = {c['id']: c for c in categories}
    rows = []
    for i in range(1, 11):
        key = f'A{i:02}:2025'; c = by_id.get(key, {})
        rows.append(f"| {key} | {_text(c.get('name', ''))} | {human_label(c.get('state', 'unknown'))} | {_text(c.get('reason'))} |")
    parts.extend(['### OWASP Top 10:2025', 'Common web application risk categories and the review work completed for each.',
                  '| Category | Name | Review state | Reason |\n|---|---|---|---|\n' + '\n'.join(rows)])
    units = coverage.get('units', [])
    open_units = [u for u in units if u.get('state') not in {'reviewed', 'not_applicable'}]
    if len(units) > len(open_units):
        # Per-unit notes for closed units made reports hundreds of KB; coverage.json keeps them.
        parts.append(f'{len(units) - len(open_units)} coverage units are closed; their review notes are in coverage.json.')
    for unit in open_units:
        parts.append(f"- {_text(unit.get('unitId'))}: {human_label(unit.get('state', 'unknown'))} — {_text(unit.get('reason'))}")
    for f in findings:
        parts.extend([f"## {_text(f.get('title'))}", _text(f.get('summary'))])
        taxonomy = f.get('taxonomy', {})
        details = []
        for key, label in [('cwe', 'Weakness type (CWE)'), ('owasp', 'Web risk category (OWASP)'), ('asvs', 'Application verification control (ASVS)')]:
            details.append(label + ': ' + ', '.join(taxonomy.get(key, [])))
        parts.append('Details — ' + '; '.join(details) + '. Evidence: ' + human_label(f.get('evidenceState', 'unknown')) + '.')
        parts.extend(['### Root cause', _text(f.get('rootCause')), '### Attack path'])
        attack = f.get('attackPath', {})
        parts.append('\n'.join(f'{i}. {_text(step)}' for i, step in enumerate(attack.get('steps', []), 1)) or 'No steps recorded.')
        parts.append('Assumptions:\n' + _bullets(attack.get('assumptions', [])))
        parts.append('Existing controls:\n' + _bullets(attack.get('controls', [])))
        parts.append('### Evidence')
        for evidence in f.get('codeEvidence', []):
            code = evidence.get('code', '')
            # A source excerpt may itself contain backticks; use a longer fence.
            fence = '`' * max(3, 1 + max((len(m.group()) for m in re.finditer(r'`+', code)), default=0))
            parts.append(f"{_text(evidence.get('label'))} — {_text(evidence.get('path'))}:{evidence.get('startLine', '?')}\n\n"
                         + fence + '\n' + code + ('' if code.endswith('\n') else '\n') + fence + '\n\n' + _text(evidence.get('explanation')))
        if not f.get('codeEvidence'):
            parts.append('No source excerpts were recorded; the source evidence is a proof gap.')
        parts.extend(['### What could make this wrong', _bullets(f.get('counterEvidence', [])),
                      '### Proof gaps', _bullets(f.get('proofGaps', [])), '### Validation'])
        validation = f.get('validation', {})
        parts.append(f"{human_label(validation.get('status', 'NOT_RUN'))} — {_text(validation.get('summary'), 'No static checks recorded.')}")
        parts.extend(['### Severity', f"{human_label(f['severity']['level'])} — {_text(f['severity'].get('rationale'))}",
                      '### Fix', _text(f.get('remediation', {}).get('summary')), 'Regression tests:', _bullets(f.get('remediation', {}).get('regressionTests', []))])
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
