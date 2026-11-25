"""The feishu_doc / feishu_drive tools reach the model only once Feishu is set up for the active profile.

Installed but unconfigured (the default install on every home) must add nothing to ``tools[]``; App ID +
App Secret in the profile's env or ``platforms.feishu`` config bring all five back, without a restart.
Runs through the real registry and ``model_tools.get_tool_definitions``.
"""

import importlib
import json

import pytest

from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

FEISHU_TOOLS = {"feishu_doc_read", "feishu_drive_list_comments", "feishu_drive_list_comment_replies",
                "feishu_drive_reply_comment", "feishu_drive_add_comment"}
CREDS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")


class _RegistryCtx:
    """The loader's ``ctx.register_tool`` lands in ``tools.registry``; the platform entry is not needed here."""

    def __init__(self, registry):
        self.registry = registry

    def register_platform(self, **kwargs):
        pass

    def register_tool(self, **kwargs):
        self.registry.register(**kwargs)


@pytest.fixture
def model_tool_names(monkeypatch, tmp_path):
    import model_tools
    from tools.registry import invalidate_check_fn_cache, registry

    for name in CREDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "default"))
    (tmp_path / "default").mkdir()
    register_mod = importlib.import_module("feishu_plugin.register")
    monkeypatch.setattr(register_mod, "core_ships_feishu", lambda: False)
    for name in FEISHU_TOOLS:
        registry.deregister(name)
    register_mod.register(_RegistryCtx(registry))

    def names() -> set:
        """Feishu tool names anywhere in the model's ``tools[]``: as tools, or listed in the tool_search
        catalog that stands in for deferred ones."""
        invalidate_check_fn_cache()  # the registry's 30 s verdict TTL elapsing between sessions
        defs = model_tools.get_tool_definitions(enabled_toolsets=["feishu_doc", "feishu_drive"], quiet_mode=True)
        blob = json.dumps(defs)
        return {name for name in FEISHU_TOOLS if name in blob}

    yield names
    for name in FEISHU_TOOLS:
        registry.deregister(name)


def test_unconfigured_install_offers_no_tools(model_tool_names):
    assert model_tool_names() == set()


def test_env_credentials_offer_all_five(model_tool_names, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_a")
    assert model_tool_names() == set(), "App ID alone is not a usable setup"
    monkeypatch.setenv("FEISHU_APP_SECRET", "s3cret")
    assert model_tool_names() == FEISHU_TOOLS


@pytest.mark.parametrize("block", [
    "platforms:\n  feishu:\n    extra:\n      app_id: cli_a\n      app_secret: s3cret\n",
    "platforms:\n  feishu:\n    app_id: cli_a\n    app_secret: s3cret\n",
    "gateway:\n  platforms:\n    feishu:\n      extra:\n        app_id: cli_a\n        app_secret: s3cret\n",
])
def test_yaml_credentials_offer_all_five(model_tool_names, tmp_path, block):
    (tmp_path / "default" / "config.yaml").write_text(block, encoding="utf-8")
    assert model_tool_names() == FEISHU_TOOLS


def test_no_tools_without_lark_oapi(model_tool_names, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_a")
    monkeypatch.setenv("FEISHU_APP_SECRET", "s3cret")
    monkeypatch.setattr(importlib.import_module("feishu_plugin.lark_client"), "_lark_sdk_installed", lambda: False)
    assert model_tool_names() == set()


def test_each_multiplexed_profile_sees_only_its_own_setup(model_tool_names, monkeypatch, tmp_path):
    """The default profile (process env, no creds) offers none; a secondary whose own .env scope carries
    the creds offers all five; a secondary without them offers none even when the process env has them."""
    secondary = tmp_path / "default" / "profiles" / "b"
    secondary.mkdir(parents=True)
    set_multiplex_active(True)
    try:
        assert model_tool_names() == set()
        home = set_hermes_home_override(secondary)
        scope = set_secret_scope({"FEISHU_APP_ID": "cli_b", "FEISHU_APP_SECRET": "b-secret"},
                                 profile_home=str(secondary))
        try:
            assert model_tool_names() == FEISHU_TOOLS
        finally:
            reset_secret_scope(scope)
            reset_hermes_home_override(home)

        monkeypatch.setenv("FEISHU_APP_ID", "cli_default")
        monkeypatch.setenv("FEISHU_APP_SECRET", "default-secret")
        home = set_hermes_home_override(secondary)
        scope = set_secret_scope({}, profile_home=str(secondary))
        try:
            assert model_tool_names() == set()
        finally:
            reset_secret_scope(scope)
            reset_hermes_home_override(home)
    finally:
        set_multiplex_active(False)
