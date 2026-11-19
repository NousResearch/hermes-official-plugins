"""Cross-platform link detection and owner-only permissions.

``is_link`` is the plugin's single definition of "symlink": a POSIX symlink, or
on Windows any name-surrogate reparse point (symbolic link, junction, mount
point). ``Path.is_symlink`` misses junctions, which redirect just like symlinks.

``set_private`` is ``chmod(0o700/0o600)``; on Windows it installs a protected
DACL granting only the current user and SYSTEM (inherited by new children).
"""
import os
import stat
from pathlib import Path

_NAME_SURROGATE = 0x20000000


def is_link(path):
    # Path.is_symlink first: identical POSIX behaviour (and the same seam tests hook).
    try:
        if Path(path).is_symlink():
            return True
    except (OSError, ValueError):
        return False
    if os.name != 'nt':
        return False
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_reparse_tag', 0) & _NAME_SURROGATE)


def set_private(path, mode):
    if os.name != 'nt':
        os.chmod(path, mode)
        return
    from . import _win32
    directory = os.path.isdir(path) and not is_link(path)
    with _win32.open_path_nofollow(os.path.abspath(path)) as handle:
        _win32.make_private(handle, directory=directory)
