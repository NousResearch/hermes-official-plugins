"""Host files named by configuration or the environment, bound into a real bwrap sandbox.

A CA bundle variable (SSL_CERT_FILE and the rest of CA_BUNDLE_VARS), an
operator bind and the profile home name a host path that bwrap mounts. Each
one is resolved once at construction and checked: a CA bundle must be a
regular file (a directory where a directory is expected) and must not
resolve into a path the sandbox hides, and none may lie in a directory a
sandbox can write to, where a command could swap it before the next spawn.
A refused CA bundle is skipped with one warning and the sandbox still runs;
a refused read-only bind or profile home stops the construction. These
tests run real commands against a fake HOME that holds dummy credential
files. Skipped where bwrap cannot run.
"""

import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from bubblewrap_plugin import sandbox
from bubblewrap_plugin.sandbox import CA_BUNDLE_VARS, BubblewrapEnvironment, load_bubblewrap_config


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

SECRET = "DUMMY-CREDENTIAL-MARKER"
ANCHORS = "DUMMY-TRUST-ANCHORS"


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


@pytest.fixture
def home(host_dir, monkeypatch):
    """A fake HOME with dummy credentials, a fake Hermes home and the Python env Hermes runs from."""
    home = host_dir / "home"
    secrets = (".ssh/id_ed25519", ".unlisted-tool/token", ".hermes/.env", ".hermes/auth.json")
    for rel in secrets:
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{SECRET} {rel}\n")
    pyenv = home / ".hermes" / "hermes-agent" / "venv"
    bundle = pyenv / "lib" / "certifi" / "cacert.pem"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(f"{ANCHORS}\n")
    (home / "project").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)
    for name in CA_BUNDLE_VARS:
        monkeypatch.delenv(name, raising=False)
    # Inside the hidden Hermes home only the Python environment Hermes runs from may be bound back;
    # here that is the fake venv above, not the interpreter running the tests.
    monkeypatch.setattr(sandbox, "python_env_roots", lambda: (str(pyenv),), raising=False)
    return home


def _bundle(home: Path) -> Path:
    return home / ".hermes" / "hermes-agent" / "venv" / "lib" / "certifi" / "cacert.pem"


def _env(home: Path, settings: dict | None = None) -> BubblewrapEnvironment:
    return BubblewrapEnvironment(cwd=str(home / "project"), timeout=30, config=load_bubblewrap_config(settings or {}))


def _refusals(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "not binding" in r.getMessage()]


def test_ca_bundle_in_the_hermes_python_env_is_bound_and_readable(home, monkeypatch, caplog):
    """The trust anchors Hermes runs with come back read-only; the rest of the Hermes home stays hidden."""
    monkeypatch.setenv("SSL_CERT_FILE", str(_bundle(home)))
    caplog.set_level(logging.WARNING)
    env = _env(home)
    try:
        out = env.execute('cat "$SSL_CERT_FILE"; cat "$HERMES_HOME/.env" 2>&1')["output"]
        written = env.execute('echo x >> "$SSL_CERT_FILE"')["returncode"]
    finally:
        env.cleanup()
    assert ANCHORS in out
    assert SECRET not in out
    assert written != 0
    assert _refusals(caplog) == []


@pytest.mark.parametrize("spelled, target", [
    ("certs/ca.pem", ".ssh/id_ed25519"),
    (".hermes/certs/ca.pem", ".unlisted-tool/token"),
    (".hermes/certs/ca.pem", ".hermes/.env"),
])
def test_ca_variable_resolving_into_a_hidden_path_is_skipped(home, monkeypatch, caplog, spelled, target):
    """A CA path that links into a credential store, a hidden dot entry or the Hermes home's secrets."""
    link = home.parent / spelled if not spelled.startswith(".hermes") else home / spelled
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(home / target)
    monkeypatch.setenv("SSL_CERT_FILE", str(link))
    caplog.set_level(logging.WARNING)
    env = _env(home)
    try:
        out = env.execute('cat "$SSL_CERT_FILE" 2>&1; echo ran')["output"]
    finally:
        env.cleanup()
    assert SECRET not in out
    assert "ran" in out
    assert any("SSL_CERT_FILE" in message for message in _refusals(caplog))


def test_ca_directory_variable_naming_the_hermes_home_is_skipped(home, monkeypatch, caplog):
    monkeypatch.setenv("SSL_CERT_DIR", str(home / ".hermes"))
    caplog.set_level(logging.WARNING)
    env = _env(home)
    try:
        out = env.execute('cat "$SSL_CERT_DIR/.env" "$SSL_CERT_DIR/auth.json" 2>&1')["output"]
    finally:
        env.cleanup()
    assert SECRET not in out
    assert any("SSL_CERT_DIR" in message for message in _refusals(caplog))


def test_ca_file_in_a_directory_the_sandbox_writes_is_skipped(home, monkeypatch, caplog):
    """The bundle resolves into the writable working directory, where a command could swap it."""
    in_cwd = home / "project" / "ca.pem"
    in_cwd.write_text(f"{ANCHORS}\n")
    link = home / ".hermes" / "certs" / "ca.pem"
    link.parent.mkdir(parents=True)
    link.symlink_to(in_cwd)
    monkeypatch.setenv("SSL_CERT_FILE", str(link))
    caplog.set_level(logging.WARNING)
    env = _env(home)
    try:
        env.execute(f"rm -f {in_cwd} && ln -s {home / '.ssh' / 'id_ed25519'} {in_cwd}")
        out = env.execute('cat "$SSL_CERT_FILE" 2>&1; echo ran')["output"]
    finally:
        env.cleanup()
    assert SECRET not in out
    assert "ran" in out
    assert any("SSL_CERT_FILE" in message for message in _refusals(caplog))


def test_a_ca_link_the_sandbox_rewrites_cannot_steer_the_next_mount(home, monkeypatch):
    """The bundle is bound from the path it resolved to at construction, never through the link."""
    link = home / "project" / "ca.pem"
    link.symlink_to(_bundle(home))
    monkeypatch.setenv("SSL_CERT_FILE", str(link))
    env = _env(home)
    try:
        first = env.execute('cat "$SSL_CERT_FILE"')["output"]
        env.execute(f"rm -f {link} && ln -s {home / '.ssh' / 'id_ed25519'} {link}")
        second = env.execute(f'cat "$SSL_CERT_FILE" {_bundle(home)} 2>&1')["output"]
    finally:
        env.cleanup()
    assert ANCHORS in first
    assert SECRET not in second


def test_unset_and_missing_ca_variables_bind_nothing_and_warn_nothing(home, monkeypatch, caplog):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(home / ".hermes" / "missing.pem"))
    caplog.set_level(logging.WARNING)
    env = _env(home)
    try:
        result = env.execute("echo ok")
    finally:
        env.cleanup()
    assert result["returncode"] == 0 and "ok" in result["output"]
    assert _refusals(caplog) == []


def test_read_only_bind_from_inside_the_writable_directory_stops_the_build(home, host_dir):
    """A source a command can replace would choose what the next spawn mounts; the entry is refused."""
    data = home / "project" / "data"
    data.mkdir()
    mnt = host_dir / "mnt"
    mnt.mkdir()
    with pytest.raises(ValueError, match="binds entry") as exc:
        _env(home, {"binds": [{"src": str(data), "dest": str(mnt)}]})
    assert str(data) in str(exc.value)
    assert str(home / "project") in str(exc.value)


def test_read_only_bind_deep_in_the_writable_directory_is_not_left_writable(home):
    """Bound at its own path two levels down, the file is a "protect this" bind a command could swap."""
    protected = home / "project" / "sub" / "protected.txt"
    protected.parent.mkdir()
    protected.write_text("ORIGINAL\n")
    with pytest.raises(ValueError, match="is read-only") as exc:
        env = _env(home, {"binds": [{"src": str(protected)}]})
        try:
            env.execute(f"echo OVERWRITTEN > {protected}")
        finally:
            env.cleanup()
    assert str(protected) in str(exc.value)
    assert protected.read_text() == "ORIGINAL\n"


def test_a_bind_another_sandbox_of_the_profile_can_swap_is_refused(home, host_dir):
    """Sandbox A writes ~/work; B, in ~/other, must not mount a source A can replace with a link."""
    data = home / "work" / "data"
    data.mkdir(parents=True)
    (data / "readme").write_text("readme\n")
    (home / "other").mkdir()
    mnt = host_dir / "mnt"
    mnt.mkdir()
    a = BubblewrapEnvironment(cwd=str(home / "work"), timeout=30, config=load_bubblewrap_config({}))
    try:
        with pytest.raises(ValueError, match="binds entry") as exc:
            b = BubblewrapEnvironment(cwd=str(home / "other"), timeout=30,
                                      config=load_bubblewrap_config({"binds": [{"src": str(data), "dest": str(mnt)}]}))
            try:
                a.execute(f"mv {data} {data}.old && ln -s {home / '.unlisted-tool'} {data}")
                leaked = b.execute(f"cat {mnt}/token 2>&1")["output"]
            finally:
                b.cleanup()
            assert SECRET not in leaked
    finally:
        a.cleanup()
    assert str(home / "work") in str(exc.value)


@pytest.fixture
def short_dir():
    """A host directory with a short path: a unix socket path is limited to about 100 bytes."""
    base = Path(tempfile.mkdtemp(prefix="hb-", dir="/var/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_binds_may_name_a_unix_socket_and_a_fifo(home, short_dir, caplog):
    """An agent or service socket, a FIFO: neither a regular file nor a directory, both bindable."""
    run = short_dir / "run"
    run.mkdir()
    sock_path = run / "svc.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    server.settimeout(20)

    def answer():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.sendall(b"pong")

    responder = threading.Thread(target=answer, daemon=True)
    responder.start()
    fifo = run / "pipe"
    os.mkfifo(fifo)
    mnt = short_dir / "mnt"
    mnt.mkdir()
    (mnt / "svc.sock").touch()
    (mnt / "pipe").touch()
    caplog.set_level(logging.WARNING)
    env = _env(home, {"binds": [
        {"src": str(sock_path), "dest": str(mnt / "svc.sock"), "readonly": False},
        {"src": str(fifo), "dest": str(mnt / "pipe")},
    ]})
    try:
        out = env.execute(
            "python3 -c \"import socket, sys; s = socket.socket(socket.AF_UNIX); "
            f"s.connect(sys.argv[1]); print(s.recv(16).decode())\" {mnt / 'svc.sock'}; "
            f"test -p {mnt / 'pipe'} && echo fifo-ok"
        )["output"]
    finally:
        env.cleanup()
        server.close()
        responder.join(5)
    assert "pong" in out
    assert "fifo-ok" in out
    assert _refusals(caplog) == []


def test_profile_home_linked_into_the_writable_directory_is_refused(home):
    """A command could rename the link target and plant a link to ~/.ssh for the next spawn's HOME bind."""
    target = home / "project" / "phome"
    target.mkdir()
    (home / ".hermes" / "home").symlink_to(target)
    with pytest.raises(ValueError, match="terminal.home_mode=profile") as exc:
        env = BubblewrapEnvironment(cwd=str(home / "project"), timeout=30, config=load_bubblewrap_config({}, "profile"))
        try:
            env.execute(f"mv {target} {target}.old && ln -s {home / '.ssh'} {target}")
            leaked = env.execute(f"cat {home / '.hermes' / 'home'}/id_ed25519 2>&1")["output"]
        finally:
            env.cleanup()
        assert SECRET not in leaked
    assert str(home / "project") in str(exc.value)


def test_profile_home_is_bound_from_the_directory_it_resolved_to(home, host_dir):
    """Each spawn binds the directory the link named at construction, never the link."""
    first, second = host_dir / "phome", host_dir / "elsewhere"
    first.mkdir()
    second.mkdir()
    link = home / ".hermes" / "home"
    link.symlink_to(first)
    env = BubblewrapEnvironment(cwd=str(home / "project"), timeout=30, config=load_bubblewrap_config({}, "profile"))
    try:
        assert env.execute(f"echo one > {link}/a.txt")["returncode"] == 0
        link.unlink()
        link.symlink_to(second)
        assert env.execute(f"echo two > {link}/b.txt")["returncode"] == 0
    finally:
        env.cleanup()
    assert (first / "a.txt").read_text() == "one\n"
    assert (first / "b.txt").read_text() == "two\n"
    assert not (second / "b.txt").exists()


def test_read_only_binds_from_a_fixed_place_or_at_their_own_path_still_work(home, host_dir, caplog):
    shared = host_dir / "shared"
    shared.mkdir()
    (shared / "readme").write_text("shared data\n")
    data = home / "project" / "data"
    data.mkdir()
    (data / "readme").write_text("project data\n")
    mnt = host_dir / "mnt"
    mnt.mkdir()
    caplog.set_level(logging.WARNING)
    env = _env(home, {"binds": [{"src": str(shared), "dest": str(mnt)}, {"src": str(data)}]})
    try:
        out = env.execute(f"cat {mnt}/readme {data}/readme")["output"]
        written = env.execute(f"echo x >> {data}/readme")["returncode"]
    finally:
        env.cleanup()
    assert "shared data" in out and "project data" in out
    assert written != 0
    assert _refusals(caplog) == []
