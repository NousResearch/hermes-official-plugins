"""Cross-platform test helpers for link-based hostile-filesystem cases.

Windows needs SeCreateSymbolicLinkPrivilege (or Developer Mode) for symbolic
links. Junctions redirect directories exactly like directory symlinks and need
no privilege, so directory cases always run. File symlinks cannot be created by
an unprivileged user at all, which also means an unprivileged checkout cannot
contain one; those cases are skipped with that reason rather than faked.
"""
import os
import sys
from pathlib import Path

import pytest

FILE_LINK_SKIP = 'file symlinks need Windows Developer Mode or SeCreateSymbolicLinkPrivilege'


def make_link(link, target, *, directory=False, required=True):
    """Create ``link`` -> ``target``. Returns the kind created, or None if unavailable.

    ``required=False`` lets a test keep its other assertions when only a file
    symlink is unavailable; otherwise the test is skipped.
    """
    link, target = Path(link), Path(target)
    try:
        link.symlink_to(target, target_is_directory=directory)
        return 'symlink'
    except OSError as exc:
        if sys.platform != 'win32' or getattr(exc, 'winerror', None) != 1314:
            raise
    if directory:
        import _winapi
        absolute = target if target.is_absolute() else link.parent / target
        _winapi.CreateJunction(os.path.abspath(absolute), str(link))
        return 'junction'
    if required:
        pytest.skip(FILE_LINK_SKIP)
    return None


def outside_system_dir():
    """A real directory outside any test tree (POSIX /etc analogue)."""
    return Path(os.environ.get('SystemRoot', r'C:\Windows')) if sys.platform == 'win32' else Path('/etc')


_FILE_LINKS = None


def require_file_links():
    """Skip before a test mutates anything if file symlinks cannot be created."""
    global _FILE_LINKS
    if _FILE_LINKS is None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            try:
                (Path(d) / 'l').symlink_to(Path(d) / 't')
                _FILE_LINKS = True
            except OSError:
                _FILE_LINKS = False
    if not _FILE_LINKS:
        pytest.skip(FILE_LINK_SKIP)
