"""What a command in a real bwrap sandbox can read under HOME and HERMES_HOME.

The model's sandbox note says the credential paths of the home directory are
not readable. These tests run real commands against a fake HOME that holds
dummy credential files and check exactly that, plus the two things the HOME
rules must not break: the entries a command needs stay readable, and the
files other sessions of the profile staged under HERMES_HOME stay out of
view. Skipped where bwrap cannot run.
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

SECRET = "DUMMY-CREDENTIAL-MARKER"
VISIBLE = "VISIBLE-MARKER"

# Credential stores a sandbox must not read. The first two are the paths the
# sweep found readable; the rest are other common token stores, and one dot
# file that no list names.
CREDENTIAL_FILES = (
    ".pgpass",
    ".config/gh/hosts.yml",
    ".git-credentials",
    ".config/hub",
    ".config/rclone/rclone.conf",
    ".config/op/config",
    ".cargo/credentials.toml",
    ".terraform.d/credentials.tfrc.json",
    ".local/share/keyrings/login.keyring",
    ".cache/huggingface/token",
    ".cache/huggingface/stored_tokens",
    ".gem/credentials",
    ".m2/settings-security.xml",
    ".config/npm/npmrc",
    ".config/pnpm/rc",
    ".local/share/uv/credentials/credentials.toml",
    ".vault-token",
    ".my.cnf",
    ".unlisted-tool-token",
    ".ssh/id_ed25519",
    ".aws/credentials",
    ".netrc",
    ".hermes/.env",
    ".hermes/auth.json",
)

# What a command needs from HOME: shell and git settings, toolchains, plain entries.
READABLE_FILES = (".bashrc", ".gitconfig", ".cargo/config.toml", ".config/git/ignore", "notes.txt")


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
    home = host_dir / "home"
    for rel in CREDENTIAL_FILES:
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{SECRET} {rel}\n")
    for rel in READABLE_FILES:
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{VISIBLE} {rel}\n")
    (home / "project").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)
    return home


def _env(home: Path, settings: dict | None = None) -> BubblewrapEnvironment:
    config = load_bubblewrap_config(settings or {})
    return BubblewrapEnvironment(cwd=str(home / "project"), timeout=30, config=config)


def _read(env: BubblewrapEnvironment, path: Path) -> str:
    return env.execute(f"cat {path} 2>&1")["output"]


def test_credential_stores_in_home_are_not_readable(home):
    env = _env(home)
    try:
        leaked = [rel for rel in CREDENTIAL_FILES if SECRET in _read(env, home / rel)]
    finally:
        env.cleanup()
    assert leaked == []


def test_dot_entries_off_the_allowlist_do_not_exist(home):
    env = _env(home)
    try:
        listing = env.execute(f"ls -A {home}")["output"].split()
        config_listing = env.execute(f"ls -A {home / '.config'}")["output"].split()
    finally:
        env.cleanup()
    assert ".pgpass" not in listing
    assert ".unlisted-tool-token" not in listing
    assert "gh" not in config_listing
    assert "git" in config_listing


def test_entries_a_command_needs_stay_readable(home):
    env = _env(home)
    try:
        unreadable = [rel for rel in READABLE_FILES if VISIBLE not in _read(env, home / rel)]
    finally:
        env.cleanup()
    assert unreadable == []


def test_home_allow_shows_an_extra_dot_entry_but_never_a_credential_store(home):
    (home / ".mytool").mkdir()
    (home / ".mytool" / "settings").write_text(f"{VISIBLE} mytool\n")
    env = _env(home, {"home_allow": [".mytool", ".config/gh", ".pgpass"]})
    try:
        mytool = _read(env, home / ".mytool" / "settings")
        gh = _read(env, home / ".config/gh/hosts.yml")
        pgpass = _read(env, home / ".pgpass")
    finally:
        env.cleanup()
    assert VISIBLE in mytool
    assert SECRET not in gh
    assert SECRET not in pgpass


def test_files_another_session_staged_are_not_visible(home):
    """Session A stages documents, images and pastes under HERMES_HOME; session B's sandbox cannot read them."""
    from tools import credential_files

    roots = [Path(mount["host_path"]) for mount in credential_files.get_cache_directory_mounts()]
    assert roots, "the staged data registry lists no directories"
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
        (root / "session-a.txt").write_text(f"{SECRET} {root.name}\n")
    env = _env(home)
    try:
        visible = [str(root) for root in roots if SECRET in _read(env, root / "session-a.txt")]
    finally:
        env.cleanup()
    assert visible == []


def test_a_dotfiles_dir_named_in_home_allow_shows_read_only_and_keeps_its_credentials_hidden(home):
    """Allowed links into a hidden dotfiles directory resolve once that directory is allowed too."""
    dotfiles = home / ".dotfiles"
    (dotfiles / "git").mkdir(parents=True)
    (dotfiles / "git" / "gitconfig").write_text(f"{VISIBLE} gitconfig\n")
    (dotfiles / "git" / "credentials").write_text(f"{SECRET} git credentials\n")
    (home / ".gitconfig").unlink()
    (home / ".gitconfig").symlink_to(".dotfiles/git/gitconfig")
    shutil.rmtree(home / ".config" / "git")
    (home / ".config" / "git").symlink_to(dotfiles / "git")
    env = _env(home)
    try:
        dangling = _read(env, home / ".gitconfig")
    finally:
        env.cleanup()
    env = _env(home, {"home_allow": [".dotfiles"]})
    try:
        gitconfig = _read(env, home / ".gitconfig")
        credentials = _read(env, home / ".config" / "git" / "credentials")
        written = env.execute(f"echo x >> {home / '.gitconfig'}")["returncode"]
    finally:
        env.cleanup()
    assert VISIBLE not in dangling
    assert VISIBLE in gitconfig
    assert SECRET not in credentials
    assert written != 0
    assert "x" not in (dotfiles / "git" / "gitconfig").read_text()
