import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import os
import re
import stat
import pytest
from hermes_security.store.artifacts import scan_dir, write_artifact, read_artifact
from hermes_security.errors import ValidationError, NotFound
from tests.support import make_link

WINDOWS = sys.platform == "win32"


def _rename_hook():
    """The primitive that publishes an artifact: os.replace, or the NT handle rename."""
    if WINDOWS:
        from hermes_security import _win32
        return _win32, "rename_handle"
    return os, "replace"


def _assert_private(path, mode):
    """0o700/0o600 on POSIX; on Windows a DACL granting only the owner and SYSTEM."""
    if not WINDOWS:
        assert stat.S_IMODE(path.stat().st_mode) == mode
        return
    from hermes_security import _win32
    sddl = _win32.dacl_sddl(path)
    aces = re.findall(r"\(([^)]*)\)", sddl)
    assert aces and all(a.split(";")[0] == "A" and a.split(";")[2] == "FA" for a in aces), sddl
    assert {a.split(";")[5] for a in aces} == {_win32.current_user_sid(), "SY"}, sddl


def test_artifact_roundtrip_permissions_and_atomic_failure(tmp_path, monkeypatch):
    root = scan_dir(tmp_path, "scan_a")
    write_artifact(root, "exports/report.txt", "first")
    assert read_artifact(root, "exports/report.txt") == b"first"
    _assert_private(root, 0o700)
    _assert_private(root / "exports", 0o700)
    _assert_private(root / "exports/report.txt", 0o600)

    def fail(*args, **kwargs):
        raise OSError("simulated failed rename")

    module, name = _rename_hook()
    with monkeypatch.context() as m:
        m.setattr(module, name, fail)
        with pytest.raises(ValidationError):
            write_artifact(root, "exports/report.txt", b"new")
    assert read_artifact(root, "exports/report.txt") == b"first"
    assert list((root / "exports").iterdir()) == [root / "exports/report.txt"]
    write_artifact(root, "exports/report.txt", b"new")
    assert read_artifact(root, "exports/report.txt") == b"new"
    with pytest.raises(NotFound):
        read_artifact(root, "missing")


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "a/../b",
        "a\x00b",
        "a//b",
        "C:\\outside",
        "a\\b",
        "./x",
    ],
)
def test_bad_paths(tmp_path, path):
    root = scan_dir(tmp_path, "scan_a")
    with pytest.raises(ValidationError):
        write_artifact(root, path, b"x")
    with pytest.raises(ValidationError):
        read_artifact(root, path)
    with pytest.raises(ValidationError):
        scan_dir(tmp_path, path)


def test_symlink_components(tmp_path):
    root = scan_dir(tmp_path / "data", "scan_a")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target").write_text("untouched")
    make_link(root / "link", outside, directory=True)
    rels = ["link/target"]
    if make_link(root / "file", outside / "target", required=False):
        rels.append("file")
    for rel in rels:
        with pytest.raises(ValidationError):
            write_artifact(root, rel, b"bad")
        with pytest.raises(ValidationError):
            read_artifact(root, rel)
    assert (outside / "target").read_text() == "untouched"
    make_link(root.parent / "bad", outside, directory=True)
    with pytest.raises(ValidationError):
        scan_dir(tmp_path / "data", "bad")
    make_link(tmp_path / "alias", tmp_path / "data", directory=True)
    with pytest.raises(ValidationError):
        scan_dir(tmp_path / "alias", "scan_new")


def test_symlink_parent_swap_cannot_escape(tmp_path, monkeypatch):
    root = scan_dir(tmp_path / "data", "scan_a")
    write_artifact(root, "exports/report", "old")
    outside = tmp_path / "outside"
    outside.mkdir()
    module, name = _rename_hook()
    replace = getattr(module, name)

    def swap(*args, **kwargs):
        try:
            (root / "exports").rename(root / "moved")
        except PermissionError:
            # Windows refuses to rename a directory while a file inside it is
            # open, so the swap itself is impossible during the write.
            assert WINDOWS
            swapped.append(False)
            return replace(*args, **kwargs)
        make_link(root / "exports", outside, directory=True)
        swapped.append(True)
        return replace(*args, **kwargs)

    swapped = []
    monkeypatch.setattr(module, name, swap)
    write_artifact(root, "exports/report", "new")
    assert not (outside / "report").exists()
    if swapped == [True]:
        assert (root / "moved/report").read_text() == "new"
    else:
        assert (root / "exports/report").read_text() == "new"
