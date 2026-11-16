"""What the session gate keeps of the version a Claude Code process reports in its init event (no Claude Code needed)."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk_setup


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(directsdk_setup, '_versions', {})


@pytest.mark.parametrize('reported, version', [
    ('2.1.322', '2.1.322'), ('2.1.323-beta.1', None), ('2.1.3220', '2.1.3220'), ('', None), (None, None), (2, None)])
def test_only_a_release_number_counts(tmp_path, reported, version):
    claude = tmp_path / 'claude'
    claude.write_text('x')
    assert directsdk_setup.claude_version([str(claude)]) == (False, None)
    directsdk_setup.note_version([str(claude)], reported)
    assert directsdk_setup.claude_version([str(claude)]) == (True, version)


def test_a_version_belongs_to_the_command_and_the_files_it_names_as_they_are(tmp_path):
    """Two wrapper commands on one interpreter are two binaries; a file updated in place is a new one (not seen)."""
    a, b = tmp_path / 'a.js', tmp_path / 'b.js'
    a.write_text('one')
    b.write_text('two')
    directsdk_setup.note_version([sys.executable, str(a)], '2.1.292')
    assert directsdk_setup.claude_version([sys.executable, str(a)]) == (True, '2.1.292')
    assert directsdk_setup.claude_version([sys.executable, str(b)]) == (False, None)
    a.write_text('one, updated')
    assert directsdk_setup.claude_version([sys.executable, str(a)]) == (False, None)


@pytest.mark.parametrize('how', ['replaced', 'rewritten'])
def test_a_same_size_update_is_a_new_binary(tmp_path, how):
    """npm gives package files a fixed mtime: a release of the same size is told apart by its new file (inode), and an
    in-place rewrite by its mtime."""
    import os
    import time
    claude = tmp_path / 'cli.js'
    claude.write_text('2.1.322')
    directsdk_setup.note_version([str(claude)], '2.1.322')
    time.sleep(.01)
    if how == 'replaced':
        (tmp_path / 'new.js').write_text('2.1.330')
        os.replace(tmp_path / 'new.js', claude)
    else:
        claude.write_text('2.1.330')
    assert directsdk_setup.claude_version([str(claude)]) == (False, None)


def test_a_version_is_kept_for_the_files_as_they_were_when_the_process_started(tmp_path):
    """The binary is updated after a process starts and before its init event arrives: the version that process
    reports belongs to the old files, so the new ones stay unseen (the next step runs per-call and learns theirs)."""
    import time
    claude = tmp_path / 'cli.js'
    claude.write_text('2.1.322')
    started = directsdk_setup.version_key([str(claude)])
    time.sleep(.01)
    claude.write_text('2.1.330, updated')
    directsdk_setup.note_version([str(claude)], '2.1.322', started)
    assert directsdk_setup.claude_version([str(claude)]) == (False, None)
