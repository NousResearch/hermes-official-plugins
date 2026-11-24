"""`GET /sessions/silent` reads the store of the profile Hermes binds the request to.

Runs against a real Hermes checkout on PYTHONPATH; skips without one.
"""
import pytest
from test_push_silent_sessions import load_api, make_store

hermes_constants = pytest.importorskip("hermes_constants")
pytest.importorskip("hermes_cli.profiles")

DEFAULT_RUNS = {"cron_default": ("cron", [("user", "Run"), ("assistant", "[SILENT]")], 2)}
WORK_RUNS = {"cron_work": ("cron", [("user", "Run"), ("assistant", "[SILENT]")], 1)}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root, work = tmp_path / "hermes", tmp_path / "hermes/profiles/work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("{}\n")
    make_store(root / "state.db", DEFAULT_RUNS)
    make_store(work / "state.db", WORK_RUNS)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root, work


def test_a_secondary_profiles_plugin_host_reads_its_own_store(homes):
    # Hermes strips `?profile=` before forwarding to the work profile's plugin host,
    # which runs with that profile's home bound.
    api = load_api()
    _root, work = homes
    token = hermes_constants.set_hermes_home_override(work)
    try:
        assert api.silent() == {"silent": ["cron_work"]}
        assert api.silent(profile="work") == {"silent": ["cron_work"]}
    finally:
        hermes_constants.reset_hermes_home_override(token)


def test_the_launch_profile_reads_the_default_store(homes):
    api = load_api()
    assert api.silent() == {"silent": ["cron_default"]}
    assert api.silent(profile="default") == {"silent": ["cron_default"]}


def test_an_unbound_request_for_another_profile_reads_that_profile(homes):
    # Older Hermes leaves every plugin request in the launch profile's home.
    api = load_api()
    assert api.silent(profile="work") == {"silent": ["cron_work"]}
