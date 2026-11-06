"""Dependency check run before review: known advisories (OSV) and runtime end of life.

Results are leads for reviewers, not findings: a vulnerable package matters
only if this code reaches the vulnerable behaviour, which workers judge from
source. A check that could not run is reported as not run, never as clean.
"""
import json
import os
import tomllib
from pathlib import PurePosixPath, Path

from ..canonical import utcnow
from ..errors import SecurityError
from . import eol, osv

MAX_LEADS = 5000
LOCKFILE_BYTES = 64 * 1024 * 1024   # real lockfiles exceed the default read cap (uv.lock is often > 1 MB)
REPORT_LEADS = 50   # most severe leads carried into the sealed manifest and report
_SEVERITY_RANK = {'critical': 4, 'high': 3, 'moderate': 2, 'medium': 2, 'low': 1}
_DISABLED = {'0', 'off', 'false', 'no', 'disabled'}


def disabled_by_environment():
    return os.environ.get('HERMES_SECURITY_DEPENDENCY_CHECK', '').strip().lower() in _DISABLED


def _relative(source, root, paths):
    """Map an osv-scanner source path to an inventory path, or None."""
    try:
        rel = Path(source).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError, TypeError):
        rel = PurePosixPath(str(source).replace('\\', '/')).as_posix()
    return rel if rel in paths else None


def _severity(vuln, groups):
    for group in groups:
        if vuln.get('id') in group.get('ids', []) and group.get('max_severity'):
            try:
                score = float(group['max_severity'])
            except (TypeError, ValueError):
                break
            return 'critical' if score >= 9 else 'high' if score >= 7 else 'medium' if score >= 4 else 'low'
    label = str((vuln.get('database_specific') or {}).get('severity', '')).lower()
    return {'moderate': 'medium'}.get(label, label) if label in _SEVERITY_RANK else 'unknown'


def _fixed(vuln, name):
    versions = []
    for affected in vuln.get('affected', []):
        if (affected.get('package') or {}).get('name') != name:
            continue
        for rng in affected.get('ranges', []):
            versions.extend(e['fixed'] for e in rng.get('events', []) if isinstance(e, dict) and e.get('fixed'))
    return sorted(set(map(str, versions)))[:5]


def local_packages(path, read):
    """Names a lockfile marks as the project's own (workspace, editable or linked) packages."""
    try:
        text = read(path)
        if not text:
            return set()
        name = PurePosixPath(path).name
        if name == 'uv.lock':
            rows = tomllib.loads(text).get('package', [])
            return {str(p.get('name', '')).lower() for p in rows
                    if isinstance(p.get('source'), dict) and p['source'].keys() & {'editable', 'virtual', 'directory', 'path'}}
        if name == 'package-lock.json':
            data = json.loads(text)
            names = {str(data.get('name', '')).lower()}
            for key, entry in (data.get('packages') or {}).items():
                # '' is the root project; keys outside node_modules/ and links are workspace members.
                if isinstance(entry, dict) and (key == '' or 'node_modules/' not in key or entry.get('link')):
                    names.add(str(entry.get('name') or key.rsplit('node_modules/', 1)[-1]).lower())
            return names - {''}
    except (ValueError, TypeError, AttributeError, tomllib.TOMLDecodeError):
        return set()
    return set()


def summarize(report, root, inventory_paths, local=None):
    """Compact per-package leads, most severe first, for files in the scan inventory.

    *local* maps a lockfile path to the project's own package names. Those are
    skipped: OSV matches by name only, so an unpublished workspace package would
    pick up advisories meant for an unrelated package of the same name.
    """
    leads, packages, outside, sources, skipped = [], 0, 0, set(), 0
    for result in report.get('results', []):
        source = (result.get('source') or {}).get('path')
        path = _relative(source, root, inventory_paths)
        if path is None:
            outside += 1
            continue
        sources.add(path)
        own = local(path) if local else set()
        for package in result.get('packages', []):
            info = package.get('package') or {}
            if str(info.get('name', '')).lower() in own:
                skipped += 1
                continue
            packages += 1
            vulns = package.get('vulnerabilities') or []
            if not vulns:
                continue
            groups = package.get('groups') or []
            advisories = [{'id': str(v.get('id'))[:100], 'aliases': [str(a)[:100] for a in v.get('aliases', [])][:5],
                           'summary': str(v.get('summary') or '')[:300], 'severity': _severity(v, groups),
                           'fixed': _fixed(v, info.get('name'))} for v in vulns]
            worst = max((_SEVERITY_RANK.get(a['severity'], 0) for a in advisories), default=0)
            leads.append({'path': path, 'ecosystem': str(info.get('ecosystem', ''))[:40],
                          'name': str(info.get('name', ''))[:200], 'version': str(info.get('version', ''))[:100],
                          'severity': next((k for k, v in _SEVERITY_RANK.items() if v == worst and k != 'moderate'), 'unknown'),
                          'advisories': advisories})
    leads.sort(key=lambda l: (-_SEVERITY_RANK.get(l['severity'], 0), l['path'], l['name'], l['version']))
    return {'packages': packages, 'vulnerablePackages': len(leads), 'advisories': sum(len(l['advisories']) for l in leads),
            'sources': sorted(sources), 'outsideInventory': outside, 'localPackages': skipped, 'leads': leads[:MAX_LEADS],
            'truncated': len(leads) > MAX_LEADS}


def check_dependencies(*, root, inventory, data_dir, work_dir, read, enabled=True, opener=None):
    """Run the whole dependency check and return its receipt; never raises for tool failures."""
    receipt = {'status': 'disabled', 'reason': None, 'tool': None, 'startedAt': utcnow(), 'network': [],
               'packages': 0, 'vulnerablePackages': 0, 'advisories': 0, 'sources': [], 'outsideInventory': 0,
               'leads': [], 'truncated': False, 'runtimes': [], 'runtimeStatus': 'disabled'}
    if not enabled:
        receipt['reason'] = 'turned off for this scan'
        return receipt
    if disabled_by_environment():
        receipt['reason'] = 'turned off by HERMES_SECURITY_DEPENDENCY_CHECK'
        return receipt
    paths = {f['path'] for f in inventory}
    try:
        executable, receipt['tool'] = osv.ensure_scanner(data_dir, opener=opener)
        receipt['network'] = list(osv.NETWORK if receipt['tool']['source'] == 'downloaded' else osv.NETWORK[1:])
        report, run = osv.run_scanner(executable, root, work_dir)
        whole = lambda path: read(path, max_bytes=LOCKFILE_BYTES)
        summary = summarize(report, root, paths, local=lambda path: local_packages(path, whole))
        receipt.update(summary, status='ok', run=run)
        if not summary['sources']:
            # The scanner ran, but nothing in scope declares packages: say so, never 'ok'.
            outside = summary['outsideInventory']
            receipt.update(status='no_sources', reason='no manifest or lockfile in the scanned scope'
                           + (f' ({outside} outside it not counted)' if outside else ''))
    except SecurityError as exc:
        receipt.update(status='not_run', reason=str(exc)[:500])
    try:
        receipt['runtimes'] = eol.check_runtimes(paths, read, opener=opener)
        receipt['runtimeStatus'] = 'ok'
        if receipt['runtimes']:
            receipt['network'] = receipt['network'] + eol.NETWORK
    except Exception as exc:  # a broken lookup must not abort the scan
        receipt.update(runtimeStatus='not_run', runtimeReason=str(exc)[:300])
    receipt['finishedAt'] = utcnow()
    return receipt


def compact(receipt):
    """The receipt without per-package leads: what ran, what it contacted, what it found."""
    if not receipt:
        return None
    keep = ('status', 'reason', 'tool', 'startedAt', 'finishedAt', 'network', 'packages', 'vulnerablePackages',
            'advisories', 'outsideInventory', 'localPackages', 'truncated', 'runtimeStatus', 'run')
    result = {k: receipt[k] for k in keep if k in receipt}
    result['sources'] = receipt.get('sources', [])[:200]
    result['runtimes'] = [{k: r.get(k) for k in ('runtime', 'declared', 'path', 'cycle', 'status', 'eolDate', 'latest')}
                          for r in receipt.get('runtimes', [])][:100]
    return result


def describe(receipt):
    """One plain sentence for the activity log and report."""
    status = (receipt or {}).get('status')
    if status == 'ok':
        eol_count = sum(r.get('status') == 'end_of_life' for r in receipt.get('runtimes', []))
        text = (f"Dependency check: {receipt['vulnerablePackages']} of {receipt['packages']} packages have known "
                f"advisories ({receipt['advisories']} advisories).")
        return text + (f' {eol_count} declared runtime version(s) are past end of life.' if eol_count else '')
    if status == 'no_sources':
        return 'Dependency check found nothing to check: ' + str(receipt.get('reason')) + '.'
    if status == 'disabled':
        return 'Dependency check turned off: ' + str(receipt.get('reason') or 'by setting') + '.'
    return 'Dependency check did not run: ' + str((receipt or {}).get('reason') or 'unknown error') + '.'


# Leads a packet embeds, in JSON characters: one packet must fit a security_scan_get page
# (tools.MAX_RESULT_CHARS) with room to spare; the rest are paged from section dependencies.
PACKET_LEAD_CHARS = 12_000


def leads_for(receipt, files, limit=100):
    """Leads whose manifest is one of *files*, for a reviewer's packet, and how many there are."""
    wanted = set(files)
    rows = [l for l in (receipt or {}).get('leads', []) if l['path'] in wanted]
    kept, size = [], 0
    for row in rows[:limit]:
        size += len(json.dumps(row))
        if size > PACKET_LEAD_CHARS:
            break
        kept.append(row)
    return kept, len(rows)
