"""Secure, static-only target snapshots and evidence APIs."""
from .resolve import resolve_target
from .inventory import build_inventory
from .snapshot import snapshot_digest, diff_manifest, is_snapshot_current, compare_inventory, changed_files, STAT_CACHE
from .excerpt import read_excerpt, verify_excerpt
from .redact import redact_secrets
from . import signals

__all__ = ['resolve_target', 'build_inventory', 'snapshot_digest', 'diff_manifest',
           'read_excerpt', 'verify_excerpt', 'is_snapshot_current', 'compare_inventory', 'redact_secrets']
