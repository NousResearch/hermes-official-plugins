"""Standalone-plugin contracts: the registry entry, the cached platform hint, the env bridge, the
authorization fields core used to key on ``Platform.QQBOT`` (open-policy startup refusal, allowlist
suppresses pairing, /update allowed) and the inert guard on cores that still bundle QQ Bot."""

import importlib
import logging
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_AUTH_ENV = ("GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS")


class _Ctx:
    def __init__(self):
        self.kwargs = None

    def register_platform(self, **kwargs):
        self.kwargs = kwargs


def _register_mod():
    return importlib.import_module("qqbot_plugin.register")


def _clear_auth(monkeypatch):
    for name in _AUTH_ENV:
        monkeypatch.delenv(name, raising=False)


def test_register_shape(monkeypatch):
    register_mod = _register_mod()
    monkeypatch.setattr(register_mod, "core_ships_qqbot", lambda: False)
    ctx = _Ctx()
    register_mod.register(ctx)
    kw = ctx.kwargs
    assert kw["name"] == "qqbot" and kw["label"] == "QQ Bot" and kw["emoji"] == "🐧"
    assert kw["allow_update_command"] is True
    assert kw["allowed_users_env"] == "QQ_ALLOWED_USERS" and kw["allow_all_env"] == "QQ_ALLOW_ALL_USERS"
    assert kw["own_policy_env"] == ("", "")
    assert kw["group_allowed_chats_env"] == "QQ_GROUP_ALLOWED_USERS"
    assert kw["allowlist_suppresses_pairing"] is True
    assert kw["env_enable_credentials"] == ("QQ_APP_ID", "QQ_CLIENT_SECRET")
    assert kw["standalone_send"].label == "QQ Bot" and kw["standalone_send"].media is True
    assert "env_overrides" not in kw  # the import-time EnvCred row needed a Platform.QQBOT member


def test_platform_hint_keeps_core_bytes():
    """Cached-prompt bytes: core's adapter hint only (core had no QQ session note to carry over)."""
    from qqbot_plugin.register import _PLATFORM_HINT

    assert _PLATFORM_HINT == (
        "You are on QQ, a popular Chinese messaging platform. QQ supports markdown formatting "
        "and emoji. You can send media files natively: include MEDIA:/absolute/path/to/file in "
        "your response. Images are sent as native photos, and other files arrive as downloadable documents.")


def test_env_enablement_seeds_core_extras(monkeypatch):
    from qqbot_plugin.gateway_env import env_enablement

    assert env_enablement() == {}
    monkeypatch.setenv("QQ_APP_ID", "app")
    assert env_enablement() == {}
    monkeypatch.setenv("QQ_CLIENT_SECRET", "secret")
    assert env_enablement() == {"app_id": "app", "client_secret": "secret"}
    monkeypatch.setenv("QQ_ALLOWED_USERS", " u1,u2 ")
    monkeypatch.setenv("QQ_GROUP_ALLOWED_USERS", " g1 ")
    monkeypatch.setenv("QQBOT_HOME_CHANNEL", " c2c:owner ")
    monkeypatch.setenv("QQBOT_HOME_CHANNEL_THREAD_ID", "t9")
    seed = env_enablement()
    assert seed["allow_from"] == "u1,u2" and seed["group_allow_from"] == "g1"
    assert seed["home_channel"] == {"chat_id": "c2c:owner", "name": "Home", "thread_id": "t9"}


def test_legacy_home_channel_env_still_works_and_warns(monkeypatch, caplog):
    from qqbot_plugin.gateway_env import env_enablement

    monkeypatch.setenv("QQ_APP_ID", "app")
    monkeypatch.setenv("QQ_CLIENT_SECRET", "secret")
    monkeypatch.setenv("QQ_HOME_CHANNEL", "group:7")
    monkeypatch.setenv("QQ_HOME_CHANNEL_NAME", "Ops")
    with caplog.at_level(logging.WARNING, logger="gateway.config"):
        seed = env_enablement()
    assert seed["home_channel"] == {"chat_id": "group:7", "name": "Ops", "thread_id": None}
    assert any("QQ_HOME_CHANNEL is deprecated" in r.getMessage() for r in caplog.records)
    monkeypatch.setenv("QQBOT_HOME_CHANNEL_NAME", "New")
    assert env_enablement()["home_channel"]["name"] == "New"


@pytest.mark.plugin_registry
def test_gateway_config_enables_qqbot_from_env(monkeypatch):
    """The real config loader turns App ID + Client Secret in env into an enabled ``platforms.qqbot``
    with core's extras and home channel (thread id included)."""
    from gateway.config import Platform, load_gateway_config

    monkeypatch.setenv("QQ_APP_ID", "app")
    monkeypatch.setenv("QQ_CLIENT_SECRET", "secret")
    monkeypatch.setenv("QQ_ALLOWED_USERS", "u1")
    monkeypatch.setenv("QQBOT_HOME_CHANNEL", "c2c:owner")
    monkeypatch.setenv("QQBOT_HOME_CHANNEL_NAME", "Me")
    monkeypatch.setenv("QQBOT_HOME_CHANNEL_THREAD_ID", "t1")
    pconf = load_gateway_config().platforms.get(Platform("qqbot"))
    assert pconf is not None and pconf.enabled
    assert pconf.extra["app_id"] == "app" and pconf.extra["client_secret"] == "secret"
    assert pconf.extra["allow_from"] == "u1"
    home = pconf.home_channel
    assert (home.chat_id, home.name, home.thread_id) == ("c2c:owner", "Me", "t1")


@pytest.mark.plugin_registry
def test_open_policy_without_allow_all_refuses_startup(monkeypatch):
    """``dm_policy: open`` in platforms.qqbot without QQ_ALLOW_ALL_USERS still aborts gateway startup:
    ``own_policy_env=("", "")`` marks QQ as an own-policy platform with YAML-only policies."""
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import _own_policy_open_startup_violation

    _clear_auth(monkeypatch)
    for policy in ({"dm_policy": "open"}, {"group_policy": "open"}):
        cfg = GatewayConfig(platforms={Platform("qqbot"): PlatformConfig(enabled=True, extra=policy)})
        assert _own_policy_open_startup_violation(cfg) == "qqbot: open policy without allow-all opt-in"
    monkeypatch.setenv("QQ_ALLOW_ALL_USERS", "true")
    assert _own_policy_open_startup_violation(cfg) is None


def _runner(platform, config):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {platform: SimpleNamespace(send=AsyncMock())}
    runner.pairing_store = MagicMock()
    runner._voice_mode = {}
    return runner


@pytest.mark.plugin_registry
@pytest.mark.parametrize("env", ["QQ_ALLOWED_USERS", "QQ_GROUP_ALLOWED_USERS"])
def test_an_allowlist_suppresses_pairing_codes(monkeypatch, env):
    """A QQ operator with a user (or group) allowlist never gets pairing codes sent to strangers."""
    from gateway.config import GatewayConfig, Platform, PlatformConfig

    _clear_auth(monkeypatch)
    qq = Platform("qqbot")
    runner = _runner(qq, GatewayConfig(platforms={qq: PlatformConfig(enabled=True)}))
    assert runner._get_unauthorized_dm_behavior(qq) == "pair"
    monkeypatch.setenv(env, "allowed-openid-1")
    assert runner._get_unauthorized_dm_behavior(qq) == "ignore"


@pytest.mark.plugin_registry
@pytest.mark.asyncio
async def test_update_command_is_allowed_from_qq(monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource

    runner = _runner(Platform("qqbot"), GatewayConfig())
    runner.adapters = {}
    event = MessageEvent(text="/update", source=SessionSource(
        platform=Platform("qqbot"), user_id="u1", chat_id="c1", user_name="t"))
    monkeypatch.setenv("HERMES_MANAGED", "")
    with patch("subprocess.Popen"):
        result = await runner._handle_update_command(event)
    assert "only available from messaging platforms" not in result


def test_inert_when_core_still_ships_qqbot(monkeypatch, caplog):
    register_mod = _register_mod()
    monkeypatch.setattr(register_mod, "core_ships_qqbot", lambda: True)
    ctx = _Ctx()
    with caplog.at_level(logging.INFO, logger=register_mod.logger.name):
        register_mod.register(ctx)
    assert ctx.kwargs is None
    assert [r.getMessage() for r in caplog.records if "stays inactive" in r.getMessage()] == [
        "qqbot: this Hermes core still ships QQ Bot built in; the plugin stays inactive until core drops it"]


def _fake_core(monkeypatch, root):
    register_mod = _register_mod()
    (root / "plugins" / "platforms").mkdir(parents=True, exist_ok=True)
    packages = {"plugins.platforms": types.SimpleNamespace(__path__=[str(root / "plugins" / "platforms")])}
    monkeypatch.setattr(register_mod.importlib, "import_module", packages.__getitem__)
    return register_mod


def test_core_ships_qqbot_detects_the_bundled_dir(tmp_path, monkeypatch):
    (tmp_path / "plugins" / "platforms" / "qqbot").mkdir(parents=True)
    (tmp_path / "plugins" / "platforms" / "qqbot" / "__init__.py").write_text("")
    assert _fake_core(monkeypatch, tmp_path).core_ships_qqbot() is True


def test_core_ships_qqbot_ignores_a_leftover_cache_dir(tmp_path, monkeypatch):
    """An update that deleted the bundled files can leave ``plugins/platforms/qqbot/__pycache__``."""
    (tmp_path / "plugins" / "platforms" / "qqbot" / "__pycache__").mkdir(parents=True)
    assert _fake_core(monkeypatch, tmp_path).core_ships_qqbot() is False
