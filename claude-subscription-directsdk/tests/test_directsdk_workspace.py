"""Every client of one OS user must reuse one workspace directory across requests.

The native CLI embeds its working directory in the request (environment-context
block, native >= ~2.1.276), so a fresh random tempdir per request breaks the
server-side prompt cache on every round of a tool loop (see issue #14): the
shared prefix collapses to static system+tools and the conversation tail is
re-written as cache each round. On Opus the block is a system message right after
the first user turn, so a per-client tempdir broke the cache whenever the host
built a new client.
"""
import importlib.util
import os
import shutil
import stat
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("directsdk_workspace", ROOT / "directsdk.py")
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)

FAKE = '''import json, os, pathlib, sys
pathlib.Path(os.path.join(os.environ["HOME"], "cwds.log")).open("a").write(str(pathlib.Path.cwd()) + "\\n")
for line in sys.stdin:
    row = json.loads(line)
    if row["type"] == "user" and row.get("shouldQuery", True):
        break
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}], "id": "m", "stop_reason": "end_turn"}}), flush=True)
print(json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
'''


REQUEST = dict(
    model="sonnet",
    messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "go"}],
    tools=[{"type": "function", "function": {"name": "probe", "description": "d"}}],
    stream=True,
    timeout=SimpleNamespace(read=5),
)


posix_only = pytest.mark.skipif(not hasattr(os, "getuid"), reason="the shared workspace is keyed by the POSIX uid")


def shared_path(tmp_path):
    return tmp_path / f"claude-directsdk-cwd-{os.getuid()}"


def fake_client(tmp_path):
    script = tmp_path / "native.py"
    script.write_text(FAKE)
    return native.Client(command=[sys.executable, str(script)], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})


def run_clients(tmp_path, count):
    clients = [fake_client(tmp_path) for _ in range(count)]
    try:
        for client in clients:
            list(client.create(**REQUEST))
            list(client.create(**REQUEST))
    finally:
        for client in clients:
            client.close()
    return (tmp_path / "cwds.log").read_text().splitlines()


@posix_only
def test_clients_of_one_user_share_a_workspace_that_survives_close_and_prune(tmp_path, monkeypatch):
    monkeypatch.setattr(native.tempfile, "tempdir", str(tmp_path))
    shared = shared_path(tmp_path)
    assert set(run_clients(tmp_path, 2)) == {str(shared)}
    assert shared.is_dir(), "the shared workspace must survive client close"
    shutil.rmtree(shared)
    client = fake_client(tmp_path)
    try:
        list(client.create(**REQUEST))
        os.utime(shared, (0, 0))
        list(client.create(**REQUEST))
    finally:
        client.close()
    assert (tmp_path / "cwds.log").read_text().splitlines()[-2:] == [str(shared)] * 2
    assert shared.stat().st_mtime > 0, "every request must refresh the workspace mtime"


def fresh_process(monkeypatch):
    """A new process: no private workspace yet, and exit cleanup recorded instead of registered."""
    monkeypatch.setattr(native, "_process_cwd", None)
    at_exit = []
    monkeypatch.setattr(native, "atexit", SimpleNamespace(register=lambda f, path, **_: at_exit.append(path)))
    return at_exit


def unsafe(kind, tmp_path, monkeypatch):
    root = tmp_path / "tmp"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(native.tempfile, "tempdir", str(root))
    if kind == "no_uid":
        # No uid and, on Windows, no user name to key the shared path by: only the private fallback is left.
        monkeypatch.delattr(native.os, "getuid", raising=False)
        monkeypatch.delenv("USERNAME", raising=False)
        return
    shared = shared_path(root)
    if kind == "file":
        shared.write_text("not a directory")
    elif kind == "symlink":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        shared.symlink_to(tmp_path / "elsewhere")
    elif kind == "open_mode":
        shared.mkdir()
        shared.chmod(0o777)
    elif kind == "open_parent":
        root.chmod(0o777)


@pytest.mark.parametrize("kind", ["no_uid", *(pytest.param(k, marks=posix_only) for k in ("file", "symlink", "open_mode", "open_parent"))])
def test_unsafe_shared_workspace_falls_back_to_one_private_workspace_per_process(kind, tmp_path, monkeypatch):
    at_exit = fresh_process(monkeypatch)
    unsafe(kind, tmp_path, monkeypatch)
    first, second = fake_client(tmp_path), fake_client(tmp_path)
    try:
        list(first.create(**REQUEST))
        private = (tmp_path / "cwds.log").read_text().splitlines()[0]
        first.close()
        # Every client of the process, including one built after another closed, keeps one cwd.
        list(second.create(**REQUEST))
        shutil.rmtree(private)
        list(second.create(**REQUEST))
        if hasattr(native.os, "getuid"):
            shutil.rmtree(private)
            if kind == "file":
                Path(private).write_text("planted")
            else:
                Path(private).mkdir()
                Path(private).chmod(0o777)
            list(second.create(**REQUEST))
    finally:
        first.close()
        second.close()
    cwds = (tmp_path / "cwds.log").read_text().splitlines()
    assert cwds[:3] == [private] * 3 and "claude-directsdk-cwd-" in private, cwds
    assert all(c != private for c in cwds[3:]), "a private path someone else recreated must not be adopted"
    if hasattr(os, "getuid"):
        assert Path(private).name != f"claude-directsdk-cwd-{os.getuid()}", "an unsafe shared path must never be used"
    assert Path(cwds[-1]).is_dir(), "the process workspace outlives its clients"
    assert at_exit[-1] == cwds[-1], "the process workspace is removed at exit"


def widened_mkdir(monkeypatch):
    """A default ACL on the tempdir turns every fresh 0700 mkdir into 0770 (seen on a Hermes deployment)."""
    real = os.mkdir

    def mkdir(path, mode=0o777, *args, **kwargs):
        real(path, mode, *args, **kwargs)
        os.chmod(path, 0o770)
    monkeypatch.setattr(native.os, "mkdir", mkdir)


@posix_only
@pytest.mark.parametrize("parent_mode", [0o700, 0o770])
def test_a_default_acl_on_the_tempdir_does_not_move_the_workspace_between_requests(parent_mode, tmp_path, monkeypatch):
    fresh_process(monkeypatch)
    root = tmp_path / "tmp"
    root.mkdir(mode=0o700)
    root.chmod(parent_mode)
    monkeypatch.setattr(native.tempfile, "tempdir", str(root))
    widened_mkdir(monkeypatch)
    client = fake_client(tmp_path)
    try:
        for _ in range(3):
            list(client.create(**REQUEST))
    finally:
        client.close()
    cwds = (tmp_path / "cwds.log").read_text().splitlines()
    assert len(set(cwds)) == 1, cwds
    # A safe parent keeps the one shared workspace; the group-writable one (no sticky bit) still refuses it.
    assert (Path(cwds[0]) == shared_path(root)) == (parent_mode == 0o700), cwds


@posix_only
def test_tightening_never_follows_a_symlink_swapped_in_after_the_mkdir(tmp_path, monkeypatch):
    """Someone who can write the tempdir renames the fresh workspace away and leaves a symlink to a file of ours."""
    fresh_process(monkeypatch)
    root = tmp_path / "tmp"
    root.mkdir(mode=0o700)
    root.chmod(0o770)
    monkeypatch.setattr(native.tempfile, "tempdir", str(root))
    victim = tmp_path / "victim"
    victim.write_text("ours")
    victim.chmod(0o644)
    before = stat.S_IMODE(victim.stat().st_mode)  # an inheriting ACL (ZFS nfs4acl) may not give back 0644
    real = os.mkdir

    def mkdir(path, mode=0o777, *args, **kwargs):
        real(path, mode, *args, **kwargs)
        if "claude-directsdk-cwd-" in os.fspath(path):
            os.rename(path, os.fspath(path) + ".moved")
            os.symlink(victim, path)
    monkeypatch.setattr(native.os, "mkdir", mkdir)
    client = fake_client(tmp_path)
    try:
        with pytest.raises(OSError):
            list(client.create(**REQUEST))
    finally:
        client.close()
    assert stat.S_IMODE(victim.stat().st_mode) == before, "chmod followed the swapped-in symlink"


@pytest.mark.skipif(os.name != "nt", reason="Windows has no uid; the path is keyed by USERNAME")
def test_windows_clients_share_one_workspace_in_the_users_own_temp(tmp_path, monkeypatch):
    # Without a uid every Windows process used a fresh mkdtemp, so message 1 moved on every gateway restart.
    assert native.shared_workdir() == native.shared_workdir()
    assert Path(native.shared_workdir()).name == "claude-directsdk-cwd-" + os.environ["USERNAME"]


def test_the_windows_workspace_is_refused_outside_the_users_profile(tmp_path, monkeypatch):
    profile, shared = tmp_path / "profile", tmp_path / "shared-temp"
    for path in (profile, shared):
        path.mkdir()
    monkeypatch.setenv("USERNAME", "fixture user")
    monkeypatch.setenv("LOCALAPPDATA", str(profile))
    monkeypatch.setattr(native.tempfile, "tempdir", str(profile / "Temp"))
    (profile / "Temp").mkdir()
    assert native._windows_shared_workdir() == str(profile / "Temp" / "claude-directsdk-cwd-fixture_user")
    monkeypatch.setattr(native.tempfile, "tempdir", str(shared))
    assert native._windows_shared_workdir() is None, "a temp dir other users may write is never shared"


def test_launch_files_a_killed_process_left_are_swept_and_a_live_ones_kept(tmp_path, monkeypatch):
    # The launch files hold the whole system prompt and tool schemas; a hard kill must not leave them behind.
    monkeypatch.setattr(native.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(native, "_swept", [])
    dead = native.subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    stale = tmp_path / f"claude-directsdk-launch-{dead.pid}-x"
    live = tmp_path / f"claude-directsdk-launch-{os.getppid()}-x"
    for path in (stale, live):
        path.mkdir()
        (path / "system.md").write_text("system prompt")
    ours = native._launch_dir()
    assert not stale.exists() and live.exists() and ours.is_dir()
    client = fake_client(tmp_path)
    try:
        list(client.create(**REQUEST))
    finally:
        client.close()
    assert not [p for p in tmp_path.glob(f"claude-directsdk-launch-{os.getpid()}-*") if p != ours], \
        "a finished request leaves no launch files"
