"""Session settings come from the profile's own config (``hermes config set plugins.entries.<id>.settings.<key>``),
read on every call, with a bad value falling back to the default."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk_setup


def configure(home, **settings):
    rows = ''.join(f'        {key}: {value}\n' for key, value in settings.items())
    (home / 'config.yaml').write_text('plugins:\n  entries:\n    claude-subscription-directsdk-experimental:\n'
                                      '      settings:\n' + (rows or '        {}\n'), encoding='utf-8')


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    return tmp_path


def test_session_settings_are_read_from_the_profiles_config(home):
    configure(home)
    assert directsdk_setup.session_settings() == (True, 4)
    assert directsdk_setup.session_idle_seconds(3600.0) == 3600.0
    configure(home, session_mode='false', max_sessions=2, session_idle_seconds=600)
    assert directsdk_setup.session_settings() == (False, 2)
    assert directsdk_setup.session_idle_seconds(3600.0) == 600


@pytest.mark.parametrize('value', ['0', '-5', 'soon', 'true'])
def test_a_bad_idle_limit_keeps_the_cache_lifetime(home, value):
    configure(home, session_idle_seconds=value)
    assert directsdk_setup.session_idle_seconds(3600.0) == 3600.0
