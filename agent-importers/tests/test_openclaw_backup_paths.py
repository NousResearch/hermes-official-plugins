"""A backup of an absolute path must stay shallow under ``backups/``.

``backup_existing`` computed ``Path(*path.parts[1:])``, which drops only the
drive/anchor, so an absolute source was mirrored whole:
``backups/Users/<name>/AppData/Local/.../config.yaml``. Every extra level of the
source tree became an extra level of the backup path, which is what pushed real
migrations past Windows' 260-character limit (``WinError 206``) and aborted them
before anything was written.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "openclaw-migration" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import openclaw_to_hermes as oth  # noqa: E402


def test_an_absolute_source_is_backed_up_under_a_shallow_name(tmp_path):
    source = tmp_path / "home" / "user" / ".openclaw" / "workspace" / "config.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("name: demo\n", encoding="utf-8")

    dest = oth.backup_existing(source, tmp_path / "backups")

    assert dest is not None and dest.exists()
    relative = dest.relative_to(tmp_path / "backups")
    assert len(relative.parts) <= 2, relative
    assert dest.read_text(encoding="utf-8") == "name: demo\n"


def test_a_deeper_source_tree_does_not_deepen_the_backup(tmp_path):
    shallow = tmp_path / "a" / "config.yaml"
    shallow.parent.mkdir(parents=True)
    shallow.write_text("x\n", encoding="utf-8")
    deep = tmp_path / "b" / "c" / "d" / "e" / "config.yaml"
    deep.parent.mkdir(parents=True)
    deep.write_text("y\n", encoding="utf-8")

    a = oth.backup_existing(shallow, tmp_path / "backups")
    b = oth.backup_existing(deep, tmp_path / "backups")

    assert len(a.relative_to(tmp_path / "backups").parts) == len(b.relative_to(tmp_path / "backups").parts)


def test_two_sources_with_the_same_tail_do_not_collide(tmp_path):
    first = tmp_path / "one" / "config.yaml"
    second = tmp_path / "two" / "config.yaml"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")

    a = oth.backup_existing(first, tmp_path / "backups")
    b = oth.backup_existing(second, tmp_path / "backups")

    assert a != b
    assert a.read_text(encoding="utf-8") == "one\n"
    assert b.read_text(encoding="utf-8") == "two\n"
