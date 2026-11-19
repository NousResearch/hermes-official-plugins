"""``require_profile``: a task that names no profile is refused instead of silently routed (#429)."""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

from agent.delegation_provider import DelegationRouteError  # noqa: E402
from advanced_delegation_plugin.profile_routes import (  # noqa: E402
    ProfilesDelegationProvider, _warned,
)

PROFILES = [{"name": "coder", "description": "Writes code."},
            {"name": "researcher", "description": "Research."}]


def _config(**settings):
    return {"backend": "profile_routes", "backends": {"profile_routes": settings}}


@pytest.fixture(autouse=True)
def _clear_warned():
    _warned.clear()
    yield
    _warned.clear()


def test_strict_refuses_a_task_without_profile():
    p = ProfilesDelegationProvider()
    with pytest.raises(DelegationRouteError) as exc:
        p.route_task({"goal": "do it"}, _config(profiles=PROFILES, require_profile=True))
    msg = str(exc.value)
    assert "require_profile" in msg and "coder" in msg and "researcher" in msg


def test_strict_still_routes_a_named_profile():
    p = ProfilesDelegationProvider()
    assert p.route_task({"profile": "coder"}, _config(profiles=PROFILES, require_profile=True)) == {
        "profile": "coder", "label": "coder"}


def test_strict_ignores_default_and_warns_once(caplog):
    p = ProfilesDelegationProvider()
    cfg = _config(profiles=PROFILES, default="coder", require_profile=True)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with pytest.raises(DelegationRouteError):
                p.route_task({}, cfg)
    assert sum("ignored when require_profile" in r.message for r in caplog.records) == 1


def test_strict_with_empty_profiles_says_so():
    p = ProfilesDelegationProvider()
    with pytest.raises(DelegationRouteError) as exc:
        p.route_task({}, _config(require_profile=True))
    assert "empty" in str(exc.value)


def test_default_off_keeps_todays_behaviour():
    p = ProfilesDelegationProvider()
    assert p.route_task({}, _config(profiles=PROFILES)) == {}
    assert p.route_task({}, _config(profiles=PROFILES, default="coder")) == {
        "profile": "coder", "label": "coder"}


def test_explicit_false_keeps_todays_behaviour():
    p = ProfilesDelegationProvider()
    assert p.route_task({}, _config(profiles=PROFILES, require_profile=False)) == {}
