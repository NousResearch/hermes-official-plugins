# Modified from Apache-2.0 source scripts/finalize_scan_contract.py (see NOTICE).
"""Read-only exports from verified sealed bundles."""
from __future__ import annotations

import csv
import io
from pathlib import Path
from .copy import _reportable
from .finalize import ReportError, _loads, _read_local, verify_bundle


def _csv_cell(value):
    # Prevent spreadsheet formula execution, including whitespace-prefixed values.
    if isinstance(value, str) and (value.startswith(('\t', '\r', '\n')) or value.lstrip().startswith(('=', '+', '-', '@', '＝', '＋', '－', '＠'))):
        return "'" + value
    return value


def export(scan_dir, fmt: str) -> tuple[bytes, str, str]:
    """Export md, SARIF, canonical findings JSON, or formula-safe CSV."""
    formats = {'md': ('report.md', 'text/markdown; charset=utf-8'),
               'sarif': ('exports/results.sarif', 'application/sarif+json'),
               'json': ('findings.json', 'application/json'),
               'csv': ('findings.csv', 'text/csv; charset=utf-8')}
    if fmt not in formats:
        raise ReportError(f'Unsupported export format: {fmt}')
    root = Path(scan_dir)
    receipt = verify_bundle(root)
    if not receipt['ok']:
        raise ReportError('Bundle verification failed: ' + '; '.join(receipt['problems']))
    rel, mime = formats[fmt]
    if fmt != 'csv':
        return _read_local(root, rel), mime, Path(rel).name
    findings = _loads(_read_local(root, 'findings.json'))
    out = io.StringIO(newline='')
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(['findingId', 'title', 'severity', 'evidenceState', 'path', 'startLine', 'cwe', 'owasp', 'validation'])
    for f in _reportable(findings):
        for location in f.get('locations', []) or [{}]:
            row = [f['findingId'], f['title'], f['severity']['level'], f['evidenceState'],
                   location.get('path', ''), location.get('startLine', ''),
                   ';'.join(f.get('taxonomy', {}).get('cwe', [])), ';'.join(f.get('taxonomy', {}).get('owasp', [])),
                   f.get('validation', {}).get('status', 'NOT_RUN')]
            writer.writerow([_csv_cell(v) for v in row])
    return out.getvalue().encode('utf-8'), mime, rel
