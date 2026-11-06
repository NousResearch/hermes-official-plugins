"""Snapshot identities and no-filter Git diff manifests."""
import os
import stat
from pathlib import Path
from ..canonical import canonical_json, sha256_hex
from ..errors import TargetError
from .resolve import git_bytes, resolve_target
from .inventory import build_inventory, safe_read, validate_path
from .policy import matches_scope

# Per-scan file of [size, mtime_ns, file id, sha256] for each inventory file, kept in the work folder.
STAT_CACHE = 'snapshot-stat.json'


def changed_files(target, stored_files, cache):
    """Inventory files whose bytes changed or that disappeared, and the refreshed stat cache.

    Like git's index, a file whose size, mtime and file ID match the cache is trusted
    without reading it; any other file is re-read with the inventory's no-follow read
    and its hash compared. New files are ignored: they were never in scope.
    """
    root = Path(target['root'])
    changed, fresh = [], {}
    for item in stored_files:
        path = item['path']
        try:
            st = os.lstat(root / path)
        except (OSError, ValueError):
            changed.append(path)
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size != item['size']:
            changed.append(path)
            continue
        hit = cache.get(path)
        if isinstance(hit, list) and hit == [st.st_size, st.st_mtime_ns, st.st_ino, item['sha256']]:
            fresh[path] = hit
            continue
        try:
            data, after = safe_read(root, path, max(item['size'], 1))
        except TargetError:
            changed.append(path)
            continue
        if sha256_hex(data) != item['sha256']:
            changed.append(path)
            continue
        fresh[path] = [after.st_size, after.st_mtime_ns, after.st_ino, item['sha256']]
    return sorted(changed), fresh


def snapshot_digest(inventory, target):
    payload = {'files': sorted((f['path'], f['sha256']) for f in inventory['files']),
               'revision': target.get('revision'), 'base': target.get('base'), 'head': target.get('head'),
               'scope': sorted(target.get('scope', [])),
               'excluded': sorted(inventory.get('excluded', []), key=lambda e: (e['path'], e['reason'])),
               'truncated': inventory.get('truncated', False)}
    return 'sha256:' + sha256_hex(canonical_json(payload))


def is_snapshot_current(target, digest):
    try:
        current = resolve_target(target['root'], scope=target.get('scope'), base=target.get('base'), head=target.get('head'))
        return snapshot_digest(build_inventory(current, with_signals=False), current) == digest
    except (TargetError, OSError, ValueError): return False


def compare_inventory(target, stored_files):
    """Compare the target now with the scan's stored inventory.

    Returns ``{'changed': [...], 'added': [...]}``: ``changed`` lists inventory files whose
    bytes changed or that disappeared (their evidence no longer describes the tree);
    ``added`` lists new files, which were simply never in scope. ``None`` when the target
    can no longer be read.
    """
    try:
        current = resolve_target(target['root'], scope=target.get('scope'), base=target.get('base'), head=target.get('head'))
        now = {f['path']: f['sha256'] for f in build_inventory(current, with_signals=False)['files']}
    except (TargetError, OSError, ValueError):
        return None
    before = {f['path']: f['sha256'] for f in stored_files}
    return {'changed': sorted(p for p, h in before.items() if now.get(p) != h),
            'added': sorted(set(now) - set(before))}


def diff_manifest(target):
    if target['kind'] != 'git': raise TargetError('Diff requires Git')
    root = target['root']; base = target.get('base') or target.get('revision'); head = target.get('head')
    if not base: raise TargetError('Diff requires an existing base commit')
    # --no-ext-diff and --no-textconv prohibit repository-supplied executable helpers.
    args = ['diff', '--name-status', '-z', '--no-renames', '--no-ext-diff', '--no-textconv', base]
    if head: args.append(head)
    args.append('--')
    fields = git_bytes(root, *args).split(b'\x00')
    changed = set(); deleted = set(); added = set()
    for i in range(0, len(fields)-1, 2):
        status, raw = fields[i:i+2]
        path = os.fsdecode(raw)
        try: validate_path(path)
        except TargetError: continue
        if not matches_scope(path, target.get('scope', [])): continue
        if status == b'D': deleted.add(path)
        elif status == b'A': added.add(path)
        else: changed.add(path)
    if head is None:
        for raw in git_bytes(root, 'ls-files', '-z', '--others', '--exclude-standard').split(b'\x00'):
            if not raw: continue
            path = os.fsdecode(raw)
            try: validate_path(path)
            except TargetError: continue
            if matches_scope(path, target.get('scope', [])): added.add(path)
    return {'base': base, 'head': head, 'changed': sorted(changed), 'deleted': sorted(deleted), 'added': sorted(added)}
