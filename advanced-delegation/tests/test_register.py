"""``register()`` hands core both backends; the bundled ``default`` stays in core."""

from __future__ import annotations

import pytest

pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

import advanced_delegation_plugin  # noqa: E402


def test_register_hands_core_both_backends():
    registered = []

    class Ctx:
        def register_delegation_provider(self, provider):
            registered.append(provider)

        def __getattr__(self, name):  # any other registrar the plugin uses is accepted and ignored here
            return lambda *a, **k: None

    advanced_delegation_plugin.register(Ctx())
    assert sorted(p.name for p in registered) == ["model_map", "profile_routes"]
