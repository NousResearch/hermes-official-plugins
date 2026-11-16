"""How the plugin reads a Claude Code's version for the session gate, with fake binaries (no Claude Code needed)."""
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk_setup


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(directsdk_setup, '_versions', {})
    count = tmp_path / 'count'

    def make(name, printed):
        script = tmp_path / name
        script.write_text(f'import sys\nopen({str(count)!r}, "a").write("x")\nprint({printed!r})\n')
        return [sys.executable, str(script)]
    make.calls = lambda: len(count.read_text()) if count.exists() else 0
    return make


@pytest.mark.parametrize('printed, version', [
    ('2.1.322 (Claude Code)', '2.1.322'), ('2.1.322', '2.1.322'),
    ('2.1.323-beta.1 (Claude Code)', None), ('2.1.3220 (Claude Code)', '2.1.3220'), ('Claude Code', None), ('', None)])
def test_only_a_release_number_counts(fake, printed, version):
    assert directsdk_setup.claude_version(fake('claude.py', printed), dict(os.environ)) == version


def test_each_command_is_asked_once_until_its_files_change(fake):
    """Two wrapper commands on one interpreter are two versions; an update of a script is read again; an unreadable
    version is not asked again on every call."""
    a, b = fake('a.py', '2.1.292 (Claude Code)'), fake('b.py', '2.1.322 (Claude Code)')
    env = dict(os.environ)
    assert [directsdk_setup.claude_version(c, env) for c in (a, b, a, b)] == ['2.1.292', '2.1.322', '2.1.292', '2.1.322']
    assert fake.calls() == 2
    time.sleep(.01)
    Path(a[1]).write_text(Path(a[1]).read_text().replace('2.1.292', '2.1.323'))
    assert directsdk_setup.claude_version(a, env) == '2.1.323' and fake.calls() == 3
    broken = fake('broken.py', 'not a version')
    assert [directsdk_setup.claude_version(broken, env) for _ in range(3)] == [None] * 3 and fake.calls() == 4

