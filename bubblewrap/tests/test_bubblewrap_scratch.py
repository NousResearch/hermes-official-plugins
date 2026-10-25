"""Each sandbox gets its own scratch directory, in a real bwrap sandbox.

Hermes points TMPDIR at its scratch directory (HERMES_HOME/cache/scratch),
which every Hermes process and session shares, and names that path in its
system prompt. Inside a sandbox the sandbox's own scratch directory is
mounted at that path: writable there, private, and without an environment
key gone with the environment. These tests run real commands against a fake HOME with
dummy files. Skipped where bwrap cannot run.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from bubblewrap_plugin.sandbox import BubblewrapEnvironment, load_bubblewrap_config


def _bwrap_usable() -> bool:
    if shutil.which("bwrap") is None:
        return False
    try:
        probe = subprocess.run(
            ["bwrap", "--unshare-user", "--ro-bind", "/", "/", "true"],
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _bwrap_usable(), reason="bwrap missing or its namespace probe failed")

SECRET = "DUMMY-OTHER-SESSION-MARKER"


@pytest.fixture
def host_dir(tmp_path):
    """A directory the sandbox sees at its host path (every sandbox mounts a fresh /tmp)."""
    if not str(tmp_path.resolve()).startswith("/tmp/"):
        yield tmp_path
        return
    base = Path(tempfile.mkdtemp(prefix="hermes-bwrap-", dir="/var/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(params=["under-home", "elsewhere"])
def scratch(request, host_dir, monkeypatch):
    """A fake HOME and Hermes home, with TMPDIR at the shared scratch dir as Hermes exports it.

    The Hermes home sits under HOME (the default ~/.hermes) or elsewhere, where the overlay
    over it is a writable tmpfs rather than the read-only HOME layout.
    """
    from hermes_constants import SCRATCH_DIR_MARKER_ENV, SCRATCH_TMP_ENV_VARS, get_scratch_dir

    home = host_dir / "home"
    (home / "project").mkdir(parents=True)
    hermes_home = home / ".hermes" if request.param == "under-home" else host_dir / "hermes"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)
    shared = get_scratch_dir(hermes_home, prune=False)
    for name in (*SCRATCH_TMP_ENV_VARS, SCRATCH_DIR_MARKER_ENV):
        monkeypatch.setenv(name, str(shared))
    return home, shared


def _env(home: Path, profile: str = "network") -> BubblewrapEnvironment:
    return BubblewrapEnvironment(cwd=str(home / "project"), timeout=30,
                                 config=load_bubblewrap_config({"profile": profile}))


def _tmpdir(env: BubblewrapEnvironment) -> Path:
    return Path(env.execute('printf %s "$TMPDIR"')["output"].strip())


def test_a_sandbox_writes_to_the_scratch_path_hermes_names(scratch):
    """The path in the system prompt and $TMPDIR are one place, writable, and the sandbox's own."""
    home, shared = scratch
    shared = shared.resolve()
    env = _env(home)
    try:
        own = Path(env._scratch_private)
        tmpdir = _tmpdir(env)
        wrote = env.execute(f"printf 'print(6 * 7)' > {shared}/probe.py && python3 {shared}/probe.py && mktemp")
        read = env.execute('cat "$TMPDIR/probe.py"')["output"]
        on_host = (own / "probe.py").read_text()
        in_shared = (shared / "probe.py").exists()
    finally:
        env.cleanup()
    assert tmpdir == shared
    assert wrote["returncode"] == 0, wrote
    lines = wrote["output"].strip().splitlines()
    assert lines[0] == "42"
    assert lines[1].startswith(str(shared) + "/")
    assert "print(6 * 7)" in read
    assert on_host == "print(6 * 7)"
    assert own.parent == shared
    assert not in_shared
    assert not own.exists()


def test_another_sessions_scratch_is_not_visible(scratch):
    home, shared = scratch
    (shared / "other-process.txt").write_text(f"{SECRET} host\n")
    a = _env(home)
    b = _env(home)
    try:
        assert a.execute(f'echo "{SECRET} a" > "$TMPDIR/a.txt"')["returncode"] == 0
        a_dir, b_dir = Path(a._scratch_private), Path(b._scratch_private)
        seen = b.execute(f"cat {shared}/a.txt {shared}/other-process.txt {a_dir}/a.txt 2>&1")["output"]
        listing = b.execute(f"ls -A {shared}")["output"].split()
        overwrite = b.execute(f"echo x > {shared}/other-process.txt")["returncode"]
    finally:
        a.cleanup()
        b.cleanup()
    assert SECRET not in seen
    assert listing == []
    assert b_dir != a_dir
    assert overwrite == 0
    assert (shared / "other-process.txt").read_text() == f"{SECRET} host\n"


def test_a_session_keeps_its_scratch_when_its_sandbox_is_made_again(scratch, bwrap_provider, monkeypatch):
    """Hermes drops an idle environment and makes a new one for the same session; another session's differs."""
    home, _shared = scratch
    provider, _settings = bwrap_provider
    monkeypatch.setenv("TERMINAL_CWD", str(home / "project"))

    def make(task_id):
        return provider.create_environment(cwd=str(home / "project"), timeout=30, task_id=task_id)

    first = make("session:a")
    try:
        a_dir = first._scratch_private
        assert first.execute('echo kept > "$TMPDIR/note.txt"')["returncode"] == 0
    finally:
        first.cleanup()
    again, other = make("session:a"), make("session:b")
    try:
        same = again.execute('cat "$TMPDIR/note.txt"')["output"]
        foreign = other.execute('cat "$TMPDIR/note.txt" 2>&1')["output"]
    finally:
        again.cleanup()
        other.cleanup()
    assert again._scratch_private == a_dir
    assert "kept" in same
    assert other._scratch_private != a_dir
    assert "kept" not in foreign


def test_restricted_profile_scratch_is_read_only(scratch):
    home, shared = scratch
    env = _env(home, "restricted")
    try:
        result = env.execute(f'touch "$TMPDIR/probe" {shared}/probe2')
    finally:
        env.cleanup()
    assert result["returncode"] != 0


def test_a_sandbox_sees_only_its_own_state_under_the_sandbox_dir(scratch, host_dir, monkeypatch):
    """A terminal.sandbox_dir outside the hidden set holds every environment's shell snapshot."""
    home, _shared = scratch
    sbx = host_dir / "sbx"
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(sbx))
    a = _env(home)
    b = _env(home)
    try:
        assert a.execute("export A_SESSION_SECRET=DUMMY-A-ENV")["returncode"] == 0
        a_state, b_state = Path(a.get_temp_dir()), Path(b.get_temp_dir())
        on_host = "".join(p.read_text(errors="replace") for p in a_state.iterdir() if p.is_file())
        listing = b.execute(f"ls -A {sbx}")["output"].split()
        seen = b.execute(f"cat {a_state}/* 2>&1; grep -rh A_SESSION_SECRET {sbx} 2>&1")["output"]
        own = b.execute(f"echo kept > {b_state}/probe && cat {b_state}/probe")["output"]
        made = b.execute(f"touch {sbx}/planted")["returncode"]
    finally:
        a.cleanup()
        b.cleanup()
    assert "DUMMY-A-ENV" in on_host
    assert listing == [b_state.name]
    assert "DUMMY-A-ENV" not in seen
    assert "kept" in own
    assert made != 0
    assert not (sbx / "planted").exists()
