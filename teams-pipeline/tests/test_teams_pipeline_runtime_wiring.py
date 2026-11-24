"""Tests for binding the Teams pipeline runtime to the msgraph webhook adapter."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from teams_pipeline_plugin.runtime import build_pipeline_runtime


class _Ctx:
    def __init__(self):
        self.cli_commands, self.platform_handlers = [], []

    def register_cli_command(self, **kwargs):
        self.cli_commands.append(kwargs["name"])

    def register_platform_handler(self, platform, factory):
        self.platform_handlers.append((platform, factory))


class _Adapter:
    def __init__(self):
        self.scheduler = None
        self._background_tasks = set()

    def set_notification_scheduler(self, scheduler):
        self.scheduler = scheduler


def test_register_binds_the_runtime_through_a_msgraph_webhook_platform_handler(monkeypatch):
    plugin = importlib.import_module("teams_pipeline_plugin")
    from teams_pipeline_plugin import runtime as runtime_module

    monkeypatch.setattr(plugin, "_core_ships_teams_pipeline", lambda: False)
    monkeypatch.setattr(runtime_module, "load_gateway_config", lambda: GatewayConfig(platforms={}))
    pipeline = object()
    monkeypatch.setattr(runtime_module, "build_pipeline_runtime", lambda gateway_config: pipeline)
    ctx = _Ctx()
    plugin.register(ctx)
    assert ctx.cli_commands == ["teams-pipeline"]
    [(platform, factory)] = ctx.platform_handlers
    assert platform == "msgraph_webhook"

    adapter = _Adapter()
    factory(object(), adapter)  # (native aiohttp app, adapter), as the adapter's connect() calls it
    assert callable(adapter.scheduler)
    assert adapter._teams_pipeline_runtime is pipeline


def test_register_stays_inert_on_a_core_that_still_bundles_teams_pipeline(monkeypatch, tmp_path):
    """The bundled copy registers the same CLI command and core wires its runtime itself."""
    import plugins as core_plugins

    plugin = importlib.import_module("teams_pipeline_plugin")
    bundled = tmp_path / "core_plugins"
    (bundled / "teams_pipeline").mkdir(parents=True)
    (bundled / "teams_pipeline" / "plugin.yaml").write_text("name: teams_pipeline\n")
    monkeypatch.setattr(core_plugins, "__path__", [str(bundled)])
    assert plugin._core_ships_teams_pipeline() is True
    ctx = _Ctx()
    plugin.register(ctx)
    assert ctx.cli_commands == [] and ctx.platform_handlers == []


def test_core_without_the_bundled_copy_is_detected(monkeypatch, tmp_path):
    import plugins as core_plugins

    plugin = importlib.import_module("teams_pipeline_plugin")
    monkeypatch.setattr(core_plugins, "__path__", [str(tmp_path)])
    assert plugin._core_ships_teams_pipeline() is False


def test_rebinding_on_reconnect_reuses_the_runtime(monkeypatch):
    """A reconnect builds a new aiohttp app and re-runs the factory: same runtime, fresh scheduler."""
    from teams_pipeline_plugin import runtime as runtime_module

    builds = []
    monkeypatch.setattr(runtime_module, "load_gateway_config", lambda: GatewayConfig(platforms={}))
    monkeypatch.setattr(runtime_module, "build_pipeline_runtime",
                        lambda gateway_config: builds.append(gateway_config) or object())
    adapter = _Adapter()
    assert runtime_module.bind_adapter(adapter) is True
    first_runtime, first_scheduler = adapter._teams_pipeline_runtime, adapter.scheduler
    assert runtime_module.bind_adapter(adapter) is True
    assert len(builds) == 1
    assert adapter._teams_pipeline_runtime is first_runtime
    assert callable(adapter.scheduler) and adapter.scheduler is not first_scheduler


@pytest.mark.anyio
async def test_build_failure_installs_a_drop_scheduler(monkeypatch):
    from teams_pipeline_plugin import runtime as runtime_module

    def _boom(gateway_config):
        raise RuntimeError("no graph creds")
    monkeypatch.setattr(runtime_module, "load_gateway_config", lambda: GatewayConfig(platforms={}))
    monkeypatch.setattr(runtime_module, "build_pipeline_runtime", _boom)
    adapter = _Adapter()
    assert runtime_module.bind_adapter(adapter) is False
    assert adapter._teams_pipeline_runtime_error == "no graph creds"
    await adapter.scheduler({"id": "n1"}, object())  # acks without raising


def test_build_pipeline_runtime_skips_sender_when_adapter_layer_is_unavailable(monkeypatch):
    gateway_config = SimpleNamespace(
        platforms={
            Platform("teams"): PlatformConfig(
                enabled=True,
                extra={
                    "delivery_mode": "graph",
                    "team_id": "team-1",
                    "channel_id": "channel-1",
                },
            ),
        }
    )

    monkeypatch.setattr(
        "teams_pipeline_plugin.runtime.build_graph_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "teams_pipeline_plugin.runtime.resolve_teams_pipeline_store_path",
        lambda: "/tmp/teams-pipeline-store.json",
    )
    monkeypatch.setattr(
        "teams_pipeline_plugin.runtime.TeamsPipelineStore",
        lambda path: {"path": path},
    )
    monkeypatch.setitem(
        sys.modules,
        "plugins.platforms.teams.adapter",
        ModuleType("plugins.platforms.teams.adapter"),
    )

    runtime = build_pipeline_runtime(gateway_config)

    assert runtime.teams_sender is None


