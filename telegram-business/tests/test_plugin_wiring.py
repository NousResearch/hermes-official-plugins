"""Wiring tests: register(ctx) queues a factory; the factory adds handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import telegram_business_plugin as plugin


class _FakeCtx:
    def __init__(self):
        self.factories = []
        self.llm = MagicMock()

    def register_telegram_handler(self, factory):
        assert callable(factory)
        self.factories.append(factory)


def test_register_queues_one_factory():
    ctx = _FakeCtx()
    plugin.register(ctx)
    assert len(ctx.factories) == 1


def test_factory_wires_handlers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    databases = []
    state_db_factory = plugin.BusinessStateDB

    def _tracked_state_db(*args, **kwargs):
        database = state_db_factory(*args, **kwargs)
        databases.append(database)
        return database

    monkeypatch.setattr(plugin, "BusinessStateDB", _tracked_state_db)
    ctx = _FakeCtx()
    plugin.register(ctx)
    factory = ctx.factories[0]

    application = MagicMock()
    adapter = SimpleNamespace(_bot=MagicMock(), name="telegram")

    try:
        factory(application, adapter)

        # 6 handlers: BusinessConnection, 2x business message, bd: callback,
        # /biz command, owner-DM edit capture.
        assert application.add_handler.call_count == 6

        # The edit-capture handler must be registered in group -1 so it runs
        # before (and can yield to) the core adapter's text handler.
        groups = [
            kwargs.get("group")
            for args, kwargs in application.add_handler.call_args_list
        ]
        assert -1 in groups

        # Plugin state DB created under HERMES_HOME, not the core state.db.
        assert (tmp_path / "telegram-business" / "state.db").exists()
    finally:
        for database in databases:
            database.close()


def test_manager_construction_failure_closes_db_and_retry_is_fresh(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    databases = []
    manager_calls = []
    successful_manager = object()

    class _TrackedDB:
        def __init__(self, path):
            self.path = path
            self.close_calls = 0
            databases.append(self)

        def close(self):
            self.close_calls += 1

    def _manager_factory(**kwargs):
        manager_calls.append(kwargs)
        if len(manager_calls) == 1:
            raise RuntimeError("startup invalidation failed")
        return successful_manager

    monkeypatch.setattr(plugin, "BusinessStateDB", _TrackedDB)
    monkeypatch.setattr(plugin, "BusinessModeManager", _manager_factory)
    ctx = _FakeCtx()
    plugin.register(ctx)
    factory = ctx.factories[0]
    application = MagicMock()
    adapter = SimpleNamespace(_bot=MagicMock(), name="telegram")

    with pytest.raises(RuntimeError, match="startup invalidation failed"):
        factory(application, adapter)

    assert len(databases) == 1
    assert databases[0].close_calls == 1

    factory(application, adapter)
    factory(application, adapter)

    assert len(databases) == 2
    assert databases[1] is manager_calls[1]["session_db"]
    assert databases[1].close_calls == 0
    assert len(manager_calls) == 2
