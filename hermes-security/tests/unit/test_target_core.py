import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import os
import subprocess
import pytest
from hermes_security.target import (resolve_target, build_inventory, snapshot_digest,
    diff_manifest, read_excerpt, verify_excerpt, is_snapshot_current, redact_secrets)
from hermes_security.errors import TargetError
from tests.support import make_link, outside_system_dir


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    git(tmp_path, 'config', 'user.name', 'Test')
    (tmp_path / 'a.py').write_text('print(1)\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'initial')
    return tmp_path


def test_directory(tmp_path):
    (tmp_path / 'a.py').write_text('print(1)\n')
    target = resolve_target(str(tmp_path))
    inv = build_inventory(target)
    assert target['kind'] == 'directory'
    assert inv['files'][0]['lines'] == 1
    digest = snapshot_digest(inv, target)
    assert digest == snapshot_digest(build_inventory(target), target)
    assert is_snapshot_current(target, digest)
    (tmp_path / 'a.py').write_text('print(2)\n')
    assert not is_snapshot_current(target, digest)


def test_rejected_roots(tmp_path):
    f = tmp_path / 'file'; f.touch()
    for root in ['/', str(Path.home()), str(f), str(tmp_path / 'missing')]:
        with pytest.raises(TargetError): resolve_target(root)


@pytest.mark.parametrize('change', ['unstaged', 'staged', 'untracked'])
def test_git_snapshot(repo, change):
    target = resolve_target(str(repo))
    assert target['revision'] == git(repo, 'rev-parse', 'HEAD')
    assert target['dirty'] is False
    before = snapshot_digest(build_inventory(target), target)
    (repo / ('new.py' if change == 'untracked' else 'a.py')).write_text('changed\n')
    if change == 'staged': git(repo, 'add', '.')
    assert resolve_target(str(repo))['dirty']
    assert not is_snapshot_current(target, before)


def test_plain_folder_scope_covers_the_folder(tmp_path):
    from hermes_security.target.policy import matches_scope
    assert matches_scope('tools/a.py', ['tools']) and matches_scope('tools/sub/b.py', ['tools/'])
    assert not matches_scope('toolsmith/a.py', ['tools']) and not matches_scope('a/tools/x.py', ['tools'])
    assert matches_scope('tools', ['tools']) and matches_scope('src/x.py', ['src/*.py'])


def test_scope_and_exclusions(tmp_path):
    (tmp_path / 'a.py').write_text('ok\n')
    (tmp_path / 'b.txt').write_text('ok\n')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules' / 'dep.py').write_text('ok')
    inv = build_inventory(resolve_target(str(tmp_path), scope=['**/*.py']))
    assert [f['path'] for f in inv['files']] == ['a.py']
    assert {'path': 'b.txt', 'reason': 'out_of_scope'} in inv['excluded']
    assert {'path': 'node_modules/dep.py', 'reason': 'vendored'} in inv['excluded']


def test_hostile_files(tmp_path):
    (tmp_path / 'a').write_text('a')
    if sys.platform == 'win32':
        # Unprivileged Windows: junctions are the link type; no FIFOs exist in the
        # filesystem; NTFS names are UTF-16, so the invalid name is a lone surrogate.
        (tmp_path / 'd').mkdir()
        make_link(tmp_path / 'inside', tmp_path / 'd', directory=True)
        make_link(tmp_path / 'outside', outside_system_dir(), directory=True)
        fd = os.open(str(tmp_path) + '\\bad\ud800', os.O_CREAT | os.O_WRONLY, 0o600); os.close(fd)
    else:
        (tmp_path / 'inside').symlink_to(tmp_path / 'a')
        (tmp_path / 'outside').symlink_to('/etc/passwd')
        os.mkfifo(tmp_path / 'fifo')
        fd = os.open(os.fsencode(tmp_path) + b'/bad\xff', os.O_CREAT | os.O_WRONLY, 0o600); os.close(fd)
    (tmp_path / 'big').write_bytes(b'x' * 100)
    inv = build_inventory(resolve_target(str(tmp_path)), max_file_bytes=20)
    reasons = {e['path']: e['reason'] for e in inv['excluded']}
    assert reasons['inside'] == reasons['outside'] == 'symlink'
    assert reasons['big'] == 'too_large'
    if sys.platform != 'win32':
        assert reasons['fifo'] == 'not_regular'
    assert 'invalid_encoding' in reasons.values()


def test_git_ignore_submodule_and_diff(repo):
    (repo / '.gitignore').write_text('ignored\n')
    (repo / 'ignored').write_text('secret')
    assert 'ignored' not in [f['path'] for f in build_inventory(resolve_target(str(repo)))['files']]
    base = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'update-index', '--add', '--cacheinfo', f'160000,{base},sub')
    inv = build_inventory(resolve_target(str(repo)))
    assert {'path': 'sub', 'reason': 'submodule'} in inv['excluded']
    (repo / 'a.py').unlink()
    (repo / 'new.py').write_text('new')
    diff = diff_manifest(resolve_target(str(repo), mode='diff'))
    assert 'a.py' in diff['deleted'] and 'new.py' in diff['added']
    git(repo, 'add', '-A'); git(repo, 'commit', '-qm', 'next')
    diff = diff_manifest(resolve_target(str(repo), base=base, head='HEAD', mode='diff'))
    assert diff['base'] == base and diff['head'] == git(repo, 'rev-parse', 'HEAD')
    assert 'a.py' in diff['deleted'] and 'new.py' in diff['added']


def test_excerpt(tmp_path):
    token = 'ghp_' + 'Ab12' * 9
    (tmp_path / 'a.py').write_text(f'x = "{token}"\nprint(x)\n')
    code = read_excerpt(tmp_path, 'a.py', 1, 1, inventory_paths={'a.py'})
    assert token not in code and '[REDACTED:' in code
    assert verify_excerpt(tmp_path, 'a.py', 1, 1, code, inventory_paths={'a.py'})
    assert not verify_excerpt(tmp_path, 'a.py', 1, 1, 'wrong', inventory_paths={'a.py'})
    for path in ['../a.py', '/etc/passwd', 'x/../a.py']:
        with pytest.raises(TargetError): read_excerpt(tmp_path, path, 1, 1, inventory_paths={path})


def test_redaction():
    text = 'AKIA' + 'A' * 16
    result, matches = redact_secrets(text)
    assert text not in result and matches[0]['type'] == 'aws-access-key'
    partial = 'ghp_' + 'A' * 20
    assert partial not in redact_secrets(partial)[0]


def test_redaction_skips_calls_but_not_literals():
    code = '    api_key = _resolve_explicit_xai_api_key()\n'
    assert redact_secrets(code) == (code, [])
    literal = 'api_key = Zq8rT2vLm9XkPw4N\n'
    assert 'Zq8rT2vLm9XkPw4N' not in redact_secrets(literal)[0]


def test_redaction_skips_variable_names_but_not_literals():
    for code in ['    "api_key": explicit_key,\n', 'token = self.session_token\n', 'secret = DEFAULT_SECRET_NAME\n']:
        assert redact_secrets(code) == (code, [])
    for literal in ['"api_key": "explicit_key_value"\n', 'token = Zq8rT2vLm9_XkPw4N\n', 'secret = abcdefghijkl_Mnop\n']:
        assert redact_secrets(literal)[1], literal
