"""Standalone-plugin contracts: the feishu_doc / feishu_drive toolsets, the PlatformEntry fields, the cached
platform hint, the env bridge, the local strings, and the inert guard on cores that still bundle Feishu."""

import importlib
import logging
import types

import pytest

DOC_TOOLS = {"feishu_doc_read"}
DRIVE_TOOLS = {"feishu_drive_list_comments", "feishu_drive_list_comment_replies",
               "feishu_drive_reply_comment", "feishu_drive_add_comment"}


class _Ctx:
    def __init__(self):
        self.kwargs = None
        self.tools = []

    def register_platform(self, **kwargs):
        self.kwargs = kwargs

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)


def _register(monkeypatch):
    register_mod = importlib.import_module("feishu_plugin.register")
    monkeypatch.setattr(register_mod, "core_ships_feishu", lambda: False)
    ctx = _Ctx()
    register_mod.register(ctx)
    return ctx


def _fake_core(monkeypatch, root):
    register_mod = importlib.import_module("feishu_plugin.register")

    (root / "plugins" / "platforms").mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(exist_ok=True)
    packages = {
        "plugins.platforms": types.SimpleNamespace(__path__=[str(root / "plugins" / "platforms")]),
        "tools": types.SimpleNamespace(__path__=[str(root / "tools")]),
    }
    monkeypatch.setattr(register_mod.importlib, "import_module", packages.__getitem__)
    return register_mod


def test_registers_both_toolsets_through_the_plugin_api(monkeypatch):
    ctx = _register(monkeypatch)
    by_toolset = {}
    for tool in ctx.tools:
        by_toolset.setdefault(tool["toolset"], set()).add(tool["name"])
    assert by_toolset == {"feishu_doc": DOC_TOOLS, "feishu_drive": DRIVE_TOOLS}
    assert all(not t["is_async"] and callable(t["check_fn"]) and t["schema"]["name"] == t["name"] for t in ctx.tools)


def test_entry_keeps_core_fields(monkeypatch):
    from gateway.platform_registry import PlatformEntry

    kwargs = _register(monkeypatch).kwargs
    PlatformEntry(source="plugin", **kwargs)  # every passed field exists on the running core
    assert kwargs["name"] == "feishu" and kwargs["allow_update_command"] is True
    assert kwargs["max_message_length"] == 8000 and kwargs["cron_deliver_env_var"] == "FEISHU_HOME_CHANNEL"
    assert kwargs["required_env"] == ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]
    assert kwargs["allowed_users_env"] == "FEISHU_ALLOWED_USERS" and kwargs["allow_bots_env"] == "FEISHU_ALLOW_BOTS"
    assert kwargs["allowlist_suppresses_pairing"] is True
    assert callable(kwargs["env_enablement_fn"]) and "env_overrides" not in kwargs


def test_new_entry_fields_only_when_core_has_them(monkeypatch):
    register_mod = importlib.import_module("feishu_plugin.register")
    new = {"binds_port_fn", "listen_port", "thread_reply_anchor", "opus_voice"}

    monkeypatch.setattr(register_mod, "_supported_entry_fields", lambda: set())
    assert not new & set(register_mod.platform_kwargs())
    monkeypatch.setattr(register_mod, "_supported_entry_fields", lambda: new)
    kwargs = register_mod.platform_kwargs()
    assert kwargs["listen_port"] == ("webhook_port", 8765)
    assert kwargs["thread_reply_anchor"] is True and kwargs["opus_voice"] is True
    binds = kwargs["binds_port_fn"]
    assert binds({"connection_mode": " Webhook "}) is True
    assert binds({"connection_mode": "websocket"}) is False and binds({}) is False and binds(None) is False


def test_platform_hint_keeps_core_bytes():
    from feishu_plugin.adapter_hint import PLATFORM_HINT

    assert PLATFORM_HINT.startswith("You are in a Feishu (Lark) workspace communicating with your user.")
    assert PLATFORM_HINT.endswith("without ffmpeg they fall back to file attachments), and other files as attachments.")
    assert len(PLATFORM_HINT) == 477


def test_env_enablement_seeds_core_extras(monkeypatch):
    from feishu_plugin.gateway_env import env_enablement

    assert env_enablement() == {}
    monkeypatch.setenv("FEISHU_APP_ID", "cli_a")
    assert env_enablement() == {}
    monkeypatch.setenv("FEISHU_APP_SECRET", "s3cret")
    assert env_enablement() == {"app_id": "cli_a", "app_secret": "s3cret", "domain": "feishu",
                                "connection_mode": "websocket"}
    monkeypatch.setenv("FEISHU_DOMAIN", "lark")
    monkeypatch.setenv("FEISHU_CONNECTION_MODE", "webhook")
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "ek")
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "vt")
    monkeypatch.setenv("FEISHU_HOME_CHANNEL", "oc_home")
    monkeypatch.setenv("FEISHU_HOME_CHANNEL_NAME", "Ops")
    seed = env_enablement()
    assert seed["domain"] == "lark" and seed["connection_mode"] == "webhook"
    assert seed["encrypt_key"] == "ek" and seed["verification_token"] == "vt"
    assert seed["home_channel"] == {"chat_id": "oc_home", "name": "Ops"}


@pytest.mark.plugin_registry
def test_gateway_config_enables_feishu_from_env(monkeypatch):
    """The real config loader turns App ID + App Secret in env into an enabled ``platforms.feishu`` with
    core's extras and home channel."""
    from gateway.config import Platform, load_gateway_config

    monkeypatch.setenv("FEISHU_APP_ID", "cli_a")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("FEISHU_HOME_CHANNEL", "oc_home")
    pconf = load_gateway_config().platforms.get(Platform("feishu"))
    assert pconf is not None and pconf.enabled
    assert pconf.extra["app_id"] == "cli_a" and pconf.extra["connection_mode"] == "websocket"
    assert pconf.home_channel is not None and pconf.home_channel.chat_id == "oc_home"


@pytest.mark.parametrize("lang, expected", [
    ("en", "Hermes needs your input"),
    ("zh-CN", None),
    ("xx", "Hermes needs your input"),
])
def test_strings_follow_the_profile_language(monkeypatch, lang, expected):
    from feishu_plugin import _i18n

    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    expected = expected or _i18n._MESSAGES["zh"]["platform.feishu.clarify.title"]
    assert _i18n.t("platform.feishu.clarify.title") == expected
    assert "ada" in _i18n.t("platform.feishu.update_prompt.answered_by", user="ada")


def test_every_language_carries_every_key():
    from feishu_plugin._i18n import _MESSAGES

    assert len(_MESSAGES) == 17
    assert all(set(table) == set(_MESSAGES["en"]) for table in _MESSAGES.values())


def test_inert_when_core_still_ships_feishu(monkeypatch, caplog):
    register_mod = importlib.import_module("feishu_plugin.register")

    monkeypatch.setattr(register_mod, "core_ships_feishu", lambda: True)
    ctx = _Ctx()
    with caplog.at_level(logging.INFO, logger=register_mod.logger.name):
        register_mod.register(ctx)
    assert ctx.kwargs is None and ctx.tools == []
    assert [r.getMessage() for r in caplog.records if "stays inactive" in r.getMessage()] == [
        "feishu: this Hermes core still ships Feishu built in; the plugin stays inactive until core drops it"]


@pytest.mark.parametrize("rel", ["plugins/platforms/feishu/__init__.py", "tools/feishu_doc_tool.py"])
def test_core_ships_feishu_detects_each_bundled_path(tmp_path, monkeypatch, rel):
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("")
    assert _fake_core(monkeypatch, tmp_path).core_ships_feishu() is True


def test_core_ships_feishu_ignores_a_leftover_cache_dir(tmp_path, monkeypatch):
    """An update that deleted the bundled files can leave ``plugins/platforms/feishu/__pycache__``."""
    (tmp_path / "plugins" / "platforms" / "feishu" / "__pycache__").mkdir(parents=True)
    assert _fake_core(monkeypatch, tmp_path).core_ships_feishu() is False
