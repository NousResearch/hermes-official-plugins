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
