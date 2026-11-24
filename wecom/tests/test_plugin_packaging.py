"""Standalone-plugin contracts: two platforms from one plugin, optional entry fields filtered against the
running core, the env bridge for both modes, the local strings, and the inert guard on cores that still
bundle WeCom."""

import dataclasses
import importlib
import logging
import types

import pytest


class _Ctx:
    def __init__(self):
        self.platforms = []

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)


def _register_mod():
    return importlib.import_module("wecom_plugin.register")


def _entry_fields():
    from gateway.platform_registry import PlatformEntry
    return {f.name for f in dataclasses.fields(PlatformEntry)}


def test_registers_both_platforms(monkeypatch):
    register_mod = _register_mod()
    monkeypatch.setattr(register_mod, "core_ships_wecom", lambda: False)
    ctx = _Ctx()
    register_mod.register(ctx)
    bot, callback = ctx.platforms
    assert (bot["name"], callback["name"]) == ("wecom", "wecom_callback")
    assert bot["cron_deliver_env_var"] == "WECOM_HOME_CHANNEL" and "cron_deliver_env_var" not in callback
    assert bot["display_overrides"] == {"streaming": True}
    assert bot["display_tier"] == callback["display_tier"] == "low"
    assert bot["platform_hint"].startswith("You are on WeCom (企业微信).")
    assert bot["own_policy_env"] == ("WECOM_DM_POLICY", "WECOM_GROUP_POLICY")
    assert callback["allowed_users_env"] == "WECOM_CALLBACK_ALLOWED_USERS"
    assert "env_overrides" not in bot and "env_overrides" not in callback


def test_kwargs_build_real_entries_on_the_running_core():
    """Every kwarg is a field this core's PlatformEntry has (newer optional fields are filtered out)."""
    from gateway.platform_registry import PlatformEntry

    register_mod = _register_mod()
    for kwargs in (register_mod.platform_kwargs(), register_mod.callback_platform_kwargs()):
        assert set(kwargs) <= _entry_fields()
        PlatformEntry(source="plugin", **kwargs)


def test_listen_port_only_on_a_core_with_the_field(monkeypatch):
    register_mod = _register_mod()
    has_field = "listen_port" in _entry_fields()
    assert ("listen_port" in register_mod.callback_platform_kwargs()) is has_field
    monkeypatch.setattr(register_mod, "_supported_entry_fields", lambda: _entry_fields() | {"listen_port"})
    assert register_mod.callback_platform_kwargs()["listen_port"] == ("port", 8645)
    monkeypatch.setattr(register_mod, "_supported_entry_fields", lambda: _entry_fields() - {"listen_port"})
    assert "listen_port" not in register_mod.callback_platform_kwargs()


def test_standalone_send_policy_routes_media_through_the_adapter_when_supported():
    from gateway.platforms.send_targets import StandaloneSendPolicy

    policy = _register_mod().platform_kwargs()["standalone_send"]
    assert policy.text_direct is True
    if "media_via_adapter" in {f.name for f in dataclasses.fields(StandaloneSendPolicy)}:
        assert policy.media_via_adapter is True


@pytest.mark.plugin_registry
def test_registry_entries_use_the_plugin_adapters():
    from gateway.config import Platform, PlatformConfig
    from gateway.platform_registry import platform_registry
    from wecom_plugin.adapter import WeComAdapter
    from wecom_plugin.callback_adapter import WecomCallbackAdapter

    assert platform_registry.get("wecom").adapter_factory is WeComAdapter
    adapter = platform_registry.get("wecom_callback").adapter_factory(PlatformConfig(enabled=True, extra={"corp_id": "c"}))
    assert isinstance(adapter, WecomCallbackAdapter) and adapter.platform == Platform("wecom_callback")
    assert WeComAdapter(PlatformConfig(enabled=True, extra={})).platform == Platform("wecom")


def test_inert_when_core_still_ships_wecom(monkeypatch, caplog):
    register_mod = _register_mod()
    monkeypatch.setattr(register_mod, "core_ships_wecom", lambda: True)
    ctx = _Ctx()
    with caplog.at_level(logging.INFO, logger=register_mod.logger.name):
        register_mod.register(ctx)
    assert ctx.platforms == []
    assert [r.getMessage() for r in caplog.records if "stays inactive" in r.getMessage()] == [
        "wecom: this Hermes core still ships WeCom built in; the plugin stays inactive until core drops it"]


def _fake_core(monkeypatch, root):
    register_mod = _register_mod()
    (root / "plugins" / "platforms").mkdir(parents=True, exist_ok=True)
    package = types.SimpleNamespace(__path__=[str(root / "plugins" / "platforms")])
    monkeypatch.setattr(register_mod.importlib, "import_module", {"plugins.platforms": package}.__getitem__)
    return register_mod


def test_core_ships_wecom_detects_the_bundled_dir(tmp_path, monkeypatch):
    (tmp_path / "plugins" / "platforms" / "wecom").mkdir(parents=True)
    (tmp_path / "plugins" / "platforms" / "wecom" / "__init__.py").write_text("")
    assert _fake_core(monkeypatch, tmp_path).core_ships_wecom() is True


def test_core_ships_wecom_ignores_a_leftover_cache_dir(tmp_path, monkeypatch):
    """An update that deleted the bundled files can leave ``plugins/platforms/wecom/__pycache__``."""
    (tmp_path / "plugins" / "platforms" / "wecom" / "__pycache__").mkdir(parents=True)
    assert _fake_core(monkeypatch, tmp_path).core_ships_wecom() is False


def test_bot_env_enablement_needs_both_credentials(monkeypatch):
    from wecom_plugin.gateway_env import env_enablement

    monkeypatch.setenv("WECOM_BOT_ID", "bot")
    assert env_enablement() == {}
    monkeypatch.setenv("WECOM_SECRET", "s3cret")
    assert env_enablement() == {"bot_id": "bot", "secret": "s3cret"}
    monkeypatch.setenv("WECOM_WEBSOCKET_URL", "wss://127.0.0.1:9/ws")
    monkeypatch.setenv("WECOM_HOME_CHANNEL", "group-1")
    monkeypatch.setenv("WECOM_HOME_CHANNEL_NAME", "Ops")
    seed = env_enablement()
    assert seed["websocket_url"] == "wss://127.0.0.1:9/ws"
    assert seed["home_channel"] == {"chat_id": "group-1", "name": "Ops"}


def test_callback_env_enablement_needs_both_credentials(monkeypatch):
    from wecom_plugin.gateway_env import callback_env_enablement

    monkeypatch.setenv("WECOM_CALLBACK_CORP_ID", "corp")
    assert callback_env_enablement() == {}
    monkeypatch.setenv("WECOM_CALLBACK_CORP_SECRET", "cs")
    assert callback_env_enablement() == {
        "corp_id": "corp", "corp_secret": "cs", "agent_id": "", "token": "", "encoding_aes_key": "",
        "host": "", "port": 8645}
    monkeypatch.setenv("WECOM_CALLBACK_PORT", " 9001 ")
    monkeypatch.setenv("WECOM_CALLBACK_HOST", "127.0.0.1")
    seed = callback_env_enablement()
    assert seed["port"] == 9001 and seed["host"] == "127.0.0.1"
    monkeypatch.setenv("WECOM_CALLBACK_PORT", "not-a-port")
    assert callback_env_enablement()["port"] == 8645


def test_is_connected_answers_for_env_only_credentials(monkeypatch):
    register_mod = _register_mod()
    empty = types.SimpleNamespace(extra={})
    assert not register_mod._is_connected(empty) and not register_mod._callback_is_connected(empty)
    monkeypatch.setenv("WECOM_BOT_ID", "bot")
    monkeypatch.setenv("WECOM_SECRET", "s")
    monkeypatch.setenv("WECOM_CALLBACK_CORP_ID", "corp")
    assert register_mod._is_connected(empty) and not register_mod._callback_is_connected(empty)
    monkeypatch.setenv("WECOM_CALLBACK_CORP_SECRET", "cs")
    assert register_mod._callback_is_connected(empty)
    assert register_mod._callback_is_connected(types.SimpleNamespace(extra={"apps": [{"corp_id": "x"}]}))


@pytest.mark.plugin_registry
def test_home_without_wecom_seeds_and_enables_nothing(caplog):
    """No WeCom env, no config: neither platform enables (wecom_callback would bind port 8645) and the
    config load warns about nothing WeCom."""
    from gateway.config import Platform, load_gateway_config
    from wecom_plugin.gateway_env import callback_env_enablement, env_enablement

    assert env_enablement() == {} and callback_env_enablement() == {}
    with caplog.at_level(logging.DEBUG):
        platforms = load_gateway_config().platforms
    for name in ("wecom", "wecom_callback"):
        pconf = platforms.get(Platform(name))
        assert pconf is None or not pconf.enabled
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "wecom" in r.getMessage().lower()]


@pytest.mark.plugin_registry
def test_gateway_config_enables_both_modes_from_env(monkeypatch):
    from gateway.config import Platform, load_gateway_config

    monkeypatch.setenv("WECOM_BOT_ID", "bot")
    monkeypatch.setenv("WECOM_SECRET", "s")
    monkeypatch.setenv("WECOM_HOME_CHANNEL", "chat-1")
    monkeypatch.setenv("WECOM_CALLBACK_CORP_ID", "corp")
    monkeypatch.setenv("WECOM_CALLBACK_CORP_SECRET", "cs")
    monkeypatch.setenv("WECOM_CALLBACK_PORT", "9002")
    platforms = load_gateway_config().platforms
    bot, callback = platforms.get(Platform("wecom")), platforms.get(Platform("wecom_callback"))
    assert bot is not None and bot.enabled and bot.extra["bot_id"] == "bot"
    assert bot.home_channel is not None and bot.home_channel.chat_id == "chat-1"
    assert callback is not None and callback.enabled and callback.extra["port"] == 9002


@pytest.mark.parametrize("lang, expected", [
    ("en", "Voice message"),
    ("zh-CN", "语音"),
    ("zh-TW", "語音訊息"),
    ("xx", "Voice message"),
])
def test_strings_follow_the_profile_language(monkeypatch, lang, expected):
    from wecom_plugin._i18n import t

    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    assert t("platform.wecom.media.kind_voice") == expected
    assert "21.00MB" in t("platform.wecom.media.file_too_large", size_mb="21.00")


def test_every_language_carries_every_key():
    from wecom_plugin._i18n import _MESSAGES

    keys = set(_MESSAGES["en"])
    assert len(keys) == 6 and all(set(table) == keys for table in _MESSAGES.values())


def test_media_notice_uses_the_local_strings(monkeypatch):
    from wecom_plugin.media import WeComMediaMixin

    monkeypatch.setenv("HERMES_LANGUAGE", "zh")
    verdict = WeComMediaMixin._apply_file_size_limits(3 * 1024 * 1024, "voice", "audio/amr")
    assert verdict["downgraded"] and verdict["downgrade_note"].startswith("语音大小 3.00MB 超过 2MB")
