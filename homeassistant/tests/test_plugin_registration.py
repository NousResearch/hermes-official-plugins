"""register(ctx) wiring: the platform and the four tools go through the public plugin API with the
exact names, kwargs and schemas a migrated install depends on."""

import dataclasses
import importlib.util
import json

import pytest

import homeassistant_plugin
from homeassistant_plugin import adapter as ha_adapter
from homeassistant_plugin import tools as ha_tools

TOOL_NAMES = ["ha_list_entities", "ha_get_state", "ha_list_services", "ha_call_service", "ha_list_statistics", "ha_get_statistics"]
NEW_ENTRY_KWARGS = {
    "trusted_inbound", "display_tier", "shared_env_prefixes", "parse_target_ref_fn",
}


class RecordingCtx:
    """Minimal stand-in for ``PluginContext`` that records the public registration calls."""

    def __init__(self):
        self.platforms = []
        self.tools = []

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)


_REAL_CORE_SHIPS_HA = homeassistant_plugin._core_ships_home_assistant


@pytest.fixture(autouse=True)
def _core_without_bundled_ha(monkeypatch):
    """Exercise the wiring as on a core that dropped HA, whichever core the suite runs against."""
    monkeypatch.setattr(homeassistant_plugin, "_core_ships_home_assistant", lambda: False)


@pytest.fixture
def ctx():
    c = RecordingCtx()
    homeassistant_plugin.register(c)
    return c


class TestPlatformRegistration:
    def test_registers_homeassistant_platform_once(self, ctx):
        assert len(ctx.platforms) == 1
        kw = ctx.platforms[0]
        assert kw["name"] == "homeassistant"
        assert kw["label"] == "Home Assistant"
        assert kw["adapter_factory"] is ha_adapter.HomeAssistantAdapter
        assert kw["check_fn"] is ha_adapter.check_ha_requirements
        assert kw["validate_config"] is ha_adapter.validate_ha_config
        assert kw["standalone_sender_fn"] is ha_adapter._standalone_send
        assert kw["required_env"] == ["HASS_TOKEN"]
        assert kw["install_hint"] == "pip install aiohttp"
        assert kw["max_message_length"] == 4096
        assert kw["emoji"] == "🏠"
        assert kw["allow_update_command"] is True
        assert kw["cron_deliver_env_var"] == "HASS_HOME_CHANNEL"

    def test_env_enablement_fn_seeds_url_from_hass_url(self, ctx, monkeypatch):
        monkeypatch.setenv("HASS_URL", "http://192.168.1.50:8123")
        monkeypatch.delenv("HASS_HOME_CHANNEL", raising=False)
        assert ctx.platforms[0]["env_enablement_fn"]() == {"url": "http://192.168.1.50:8123"}

    def test_is_connected_requires_non_blank_hass_token(self, ctx, monkeypatch):
        from gateway.config import PlatformConfig

        is_connected = ctx.platforms[0]["is_connected"]
        monkeypatch.delenv("HASS_TOKEN", raising=False)
        assert is_connected(PlatformConfig(enabled=True)) is False
        monkeypatch.setenv("HASS_TOKEN", "tok")
        assert is_connected(PlatformConfig(enabled=True)) is True

    def test_kwargs_build_a_real_platform_entry(self, ctx):
        """Every kwarg passed must be one the running core's PlatformEntry accepts."""
        from gateway.platform_registry import PlatformEntry

        entry = PlatformEntry(source="plugin", **ctx.platforms[0])
        assert entry.name == "homeassistant"
        assert entry.adapter_factory is ha_adapter.HomeAssistantAdapter

    def test_target_parser_resolves_explicit_notify_target(self, ctx):
        parser = ctx.platforms[0]["parse_target_ref_fn"]
        assert parser("  mobile_app_my_phone  ") == ("mobile_app_my_phone", None)

    def test_target_parser_rejects_blank_target(self, ctx):
        assert ctx.platforms[0]["parse_target_ref_fn"]("  ") is None

    def test_new_entry_kwargs_passed_when_core_supports_them(self, monkeypatch):
        import gateway.platform_registry as reg

        @dataclasses.dataclass
        class NewerEntry(reg.PlatformEntry):
            trusted_inbound: bool = False
            display_tier: str = ""
            shared_env_prefixes: tuple = ()

        monkeypatch.setattr(reg, "PlatformEntry", NewerEntry)
        c = RecordingCtx()
        homeassistant_plugin.register(c)
        kw = c.platforms[0]
        assert kw["trusted_inbound"] is True
        assert kw["display_tier"] == "minimal"
        assert kw["shared_env_prefixes"] == ("HASS_",)
        NewerEntry(source="plugin", **kw)  # constructs cleanly

    def test_new_entry_kwargs_dropped_when_core_lacks_them(self, monkeypatch):
        import gateway.platform_registry as reg

        known = {f.name for f in dataclasses.fields(reg.PlatformEntry)} - NEW_ENTRY_KWARGS

        older = dataclasses.make_dataclass(
            "OlderEntry",
            [(f.name, f.type, f) for f in dataclasses.fields(reg.PlatformEntry) if f.name in known],
        )
        monkeypatch.setattr(reg, "PlatformEntry", older)
        c = RecordingCtx()
        homeassistant_plugin.register(c)
        kw = c.platforms[0]
        assert not (NEW_ENTRY_KWARGS & set(kw))
        older(source="plugin", **kw)  # an older core would not raise TypeError


class TestCoreStillBundlesHomeAssistant:
    def test_registers_nothing_when_core_ships_home_assistant(self, monkeypatch, tmp_path, caplog):
        """On a core that still bundles HA (``tools/homeassistant_tool.py``), loading the plugin too would
        shadow core's ha_* tools and replace core's platform adapter; it must stay inert instead."""
        import tools as core_tools

        monkeypatch.setattr(homeassistant_plugin, "_core_ships_home_assistant", _REAL_CORE_SHIPS_HA)
        (tmp_path / "homeassistant_tool.py").write_text("")
        monkeypatch.setattr(core_tools, "__path__", [*core_tools.__path__, str(tmp_path)])
        c = RecordingCtx()
        with caplog.at_level("INFO"):
            homeassistant_plugin.register(c)
        assert (c.platforms, c.tools) == ([], [])
        assert any("still ships Home Assistant" in r.getMessage() for r in caplog.records)


class TestToolRegistration:
    def test_six_tools_in_homeassistant_toolset(self, ctx):
        assert [t["name"] for t in ctx.tools] == TOOL_NAMES
        for t in ctx.tools:
            assert t["toolset"] == "homeassistant"
            assert t["schema"]["name"] == t["name"]
            assert t["emoji"] == "🏠"
            assert t["check_fn"] is ha_tools._check_ha_available

    def test_check_fn_follows_hass_token(self, ctx, monkeypatch):
        check = ctx.tools[0]["check_fn"]
        monkeypatch.delenv("HASS_TOKEN", raising=False)
        assert check() is False
        monkeypatch.setenv("HASS_TOKEN", "tok")
        assert check() is True

    def test_handlers_take_args_positionally_and_return_json(self, ctx):
        handlers = {t["name"]: t["handler"] for t in ctx.tools}
        result = json.loads(handlers["ha_get_state"]({}, task_id="t1"))
        assert "entity_id" in result["error"]
        result = json.loads(handlers["ha_call_service"]({"domain": "shell_command", "service": "x"}))
        assert "blocked" in result["error"]

    @pytest.mark.skipif(
        importlib.util.find_spec("tools.homeassistant_tool") is None,
        reason="core no longer ships tools/homeassistant_tool.py",
    )
    def test_schemas_identical_to_core_tools(self, ctx):
        """While core still ships the old module, the moved schemas must match it byte-for-byte."""
        import tools.homeassistant_tool as core

        legacy_names = {"ha_list_entities", "ha_get_state", "ha_list_services", "ha_call_service"}
        ours = {t["name"]: t["schema"] for t in ctx.tools if t["name"] in legacy_names}
        assert ours == {
            s["name"]: s for s in (
                core.HA_LIST_ENTITIES_SCHEMA, core.HA_GET_STATE_SCHEMA,
                core.HA_LIST_SERVICES_SCHEMA, core.HA_CALL_SERVICE_SCHEMA)
        }
        assert ha_tools._BLOCKED_DOMAINS == core._BLOCKED_DOMAINS
        assert ha_tools._ENTITY_ID_RE.pattern == core._ENTITY_ID_RE.pattern
        assert ha_tools._SERVICE_NAME_RE.pattern == core._SERVICE_NAME_RE.pattern
