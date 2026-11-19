"""Diff review accounts for deleted paths without inventing current citations."""
from ..canonical import stable_id
from ..target import diff_manifest
from .standard import lanes


def build_packets(scan_id, target, profiles, *, manifest=None):
    # Resume uses the recorded manifest; never re-plan against a moving worktree.
    manifest = diff_manifest(target) if manifest is None else manifest
    paths = sorted(set(manifest['changed'] + manifest['added'] + manifest['deleted']))
    packets = [{'packetId': stable_id('pkt', scan_id, 'diff', i), 'role': 'diff',
                'lanes': [r['id'] for r in lanes()], 'files': paths[i:i + 200], 'profiles': list(profiles),
                'brief': 'Review each changed, added and deleted path; inspect sibling operations and removed controls. Deleted paths are review units, not current-snapshot source citations.'}
               for i in range(0, len(paths), 200)]
    return packets, manifest


def attribution(candidate, manifest):
    changed = set(manifest['changed'] + manifest['added'] + manifest['deleted'])
    return 'introduced' if any(loc['path'] in changed for loc in candidate.get('locations', []) + candidate.get('codeEvidence', [])) else 'inherited'
