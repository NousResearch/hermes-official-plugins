"""Standalone-plugin contracts: the registry entry, the env bridge under a core without ``Platform.WEIXIN``,
the local strings, every send through the native sender, and the inert guard on cores that still bundle
Weixin. Several cases came from core's suite when Weixin left core."""

import importlib
import types

import pytest

adapter = importlib.import_module("weixin_plugin.adapter")
i18n = importlib.import_module("weixin_plugin._i18n")


class _Ctx:
    def __init__(self):
        self.kwargs = None

    def register_platform(self, **kwargs):
        self.kwargs = kwargs


def test_registers_the_weixin_platform_with_core_s_setup_and_authz_fields(monkeypatch):
    monkeypatch.setattr(adapter, "core_ships_weixin", lambda: False)
    ctx = _Ctx()
    adapter.register(ctx)
    kw = ctx.kwargs
    assert kw["name"] == "weixin" and kw["adapter_factory"] is adapter.WeixinAdapter
    assert kw["setup_fn"] is adapter.interactive_setup  # the QR login of `hermes gateway setup`
    assert kw["required_env"] == ["WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN"]
    assert kw["cron_deliver_env_var"] == "WEIXIN_HOME_CHANNEL"
    assert kw["allowed_users_env"] == "WEIXIN_ALLOWED_USERS"
    assert kw["own_policy_env"] == ("WEIXIN_DM_POLICY", "WEIXIN_GROUP_POLICY")
    assert kw["standalone_send"].all_sends and kw["standalone_send"].media


def test_register_is_inert_on_a_core_that_still_bundles_weixin(monkeypatch):
    monkeypatch.setattr(adapter, "core_ships_weixin", lambda: True)
    ctx = _Ctx()
    adapter.register(ctx)
    assert ctx.kwargs is None


def test_core_ships_weixin_ignores_a_leftover_pycache_only_dir(monkeypatch, tmp_path):
    (tmp_path / "weixin" / "__pycache__").mkdir(parents=True)
    real = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name: types.SimpleNamespace(__path__=[str(tmp_path)])
                        if name == "plugins.platforms" else real(name))
    assert adapter.core_ships_weixin() is False
    (tmp_path / "weixin" / "__init__.py").write_text("", encoding="utf-8")
    assert adapter.core_ships_weixin() is True


@pytest.mark.plugin_registry
def test_env_credentials_enable_weixin_with_its_extras_and_home_channel(monkeypatch):
    from gateway.config import Platform, load_gateway_config
    for name, value in (("WEIXIN_ACCOUNT_ID", "acc@im.bot"), ("WEIXIN_TOKEN", "tok-1234"), ("WEIXIN_DM_POLICY", "Open"),
                        ("WEIXIN_BASE_URL", "https://ilink.example/"), ("WEIXIN_HOME_CHANNEL", " wxid_home ")):
        monkeypatch.setenv(name, value)  # the gateway loads the profile's .env into its environment
    cfg = load_gateway_config().platforms[Platform("weixin")]
    assert cfg.enabled and cfg.token == "tok-1234"
    assert cfg.extra["account_id"] == "acc@im.bot" and cfg.extra["dm_policy"] == "open"
    assert cfg.extra["base_url"] == "https://ilink.example"
    assert cfg.home_channel is not None and cfg.home_channel.chat_id == "wxid_home"


@pytest.mark.plugin_registry
def test_a_yaml_disable_survives_env_credentials(_isolated_hermes_home, monkeypatch):
    from gateway.config import Platform, load_gateway_config
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "acc")
    monkeypatch.setenv("WEIXIN_TOKEN", "tok")
    (_isolated_hermes_home / "config.yaml").write_text("platforms:\n  weixin:\n    enabled: false\n", encoding="utf-8")
    cfg = load_gateway_config().platforms.get(Platform("weixin"))
    assert cfg is None or cfg.enabled is False


@pytest.mark.plugin_registry
def test_a_fresh_home_does_not_enable_weixin(_isolated_hermes_home):
    from gateway.config import Platform, load_gateway_config
    assert Platform("weixin") not in load_gateway_config().get_connected_platforms()


@pytest.mark.parametrize(("lang", "text"), [
    ("en", "[voice message as attachment]"), ("zh", "[语音消息作为附件]"), ("zh-TW", "[語音訊息作為附件]"),
    ("de", "[Sprachnachricht als Anhang]"), ("xx", "[voice message as attachment]"),
])
def test_voice_attachment_string_follows_the_profile_language(monkeypatch, lang, text):
    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    assert i18n.t("platform.weixin.voice_as_attachment") == text


def test_every_language_core_shipped_is_carried():
    assert set(i18n._MESSAGES) == {"af", "ar", "de", "en", "es", "fr", "ga", "hu", "it", "ja", "ko", "pt", "ru",
                                   "tr", "uk", "zh", "zh-hant"}


def test_gateway_setup_runs_the_qr_login_and_saves_core_s_env_names(monkeypatch, _isolated_hermes_home):
    """`hermes gateway setup` → Weixin: the QR login's credentials land in the profile's .env under the same
    WEIXIN_* names core's wizard wrote, through the public setup helpers only."""
    import hermes_cli.setup as setup
    from hermes_cli.config import get_env_value

    async def fake_qr_login(home, **_):
        assert home == str(_isolated_hermes_home)
        return {"account_id": "acc@im.bot", "token": "tok", "base_url": "https://ilink.example", "user_id": "wxid_me"}
    monkeypatch.setattr(adapter, "qr_login", fake_qr_login)
    monkeypatch.setattr(setup, "prompt_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(setup, "prompt_choice", lambda question, choices, default=0, **k: 0)
    import hermes_cli.gateway_setup_wizard as wizard
    monkeypatch.setattr(wizard, "offer_home_channel", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "confirm_reconfigure", lambda *a, **k: True)
    adapter.interactive_setup()
    assert get_env_value("WEIXIN_ACCOUNT_ID") == "acc@im.bot"
    assert get_env_value("WEIXIN_TOKEN") == "tok"
    assert get_env_value("WEIXIN_BASE_URL") == "https://ilink.example"
    assert get_env_value("WEIXIN_DM_POLICY") == "pairing"
    assert get_env_value("WEIXIN_GROUP_POLICY") == "disabled"


@pytest.mark.plugin_registry
@pytest.mark.parametrize(("token", "enabled"), [("tok", False), ("changeme", False), ("tokprobe1234", True)])
def test_a_placeholder_token_never_starts_the_adapter(monkeypatch, token, enabled):
    """Core refused a WEIXIN_TOKEN under 4 characters or of a placeholder shape at gateway start; so does
    the plugin's env bridge."""
    from gateway.config import Platform, load_gateway_config
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "acc@im.bot")
    monkeypatch.setenv("WEIXIN_TOKEN", token)
    config = load_gateway_config()
    cfg = config.platforms.get(Platform("weixin"))
    assert bool(cfg and cfg.enabled) is enabled
    assert (Platform("weixin") in config.get_connected_platforms()) is enabled


def test_send_only_homes_keep_delivering_on_a_core_with_the_seam(monkeypatch):
    """Core delivered Weixin from credentials with ``platforms.weixin.enabled: false``; the policy asks for
    the same wherever the running core's StandaloneSendPolicy offers ``sends_while_disabled``."""
    import dataclasses
    from gateway.platforms.send_targets import StandaloneSendPolicy
    policy = adapter.platform_kwargs()["standalone_send"]
    has_seam = "sends_while_disabled" in {f.name for f in dataclasses.fields(StandaloneSendPolicy)}
    assert getattr(policy, "sends_while_disabled", False) is has_seam
