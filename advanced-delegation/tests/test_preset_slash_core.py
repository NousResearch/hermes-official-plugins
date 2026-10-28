"""Exercise presets with the actual store, host facade and agent tool rebuild."""
import shutil
import pytest
import hermes_yaml as yaml

pytest.importorskip("agent.delegation_backends")


def test_same_backend_preset_rebuilds_live_schema(tmp_path, plugin_root, monkeypatch):
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    from agent.delegation_backends import apply_requested_tool_rebuild
    from run_agent import AIAgent

    home = tmp_path / "os-home" / ".hermes"
    shutil.copytree(plugin_root, home / "plugins" / "advanced-delegation",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    route = lambda name: {"name": name, "description": name, "provider": "openrouter", "model": "test"}
    config = {"plugins": {"enabled": ["advanced-delegation"]}, "delegation": {
        "backend": "model_map", "backends": {"model_map": {
            "options": [route("old")], "default": "old",
            "presets": [{"name": "work", "options": [route("new")], "default": "new"}]}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    manager = PluginManager()
    manager.discover_and_load()
    try:
        fn = manager._plugin_commands["delegation"]["handler"]
        agent = AIAgent(base_url="http://127.0.0.1:9/v1", api_key="test", model="test-model",
                        quiet_mode=True, enabled_toolsets=["delegation"], session_id="preset-key",
                        skip_context_files=True, skip_memory=True)
        def options():
            tool = next(t for t in agent.tools if t["function"]["name"] == "delegate_task")
            return tool["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]["option"]["enum"]
        assert options() == ["old"]
        result = invoke_plugin_command(fn, "preset work --now", source=None, session_id="preset-key",
                                       session_key="preset-key", platform="cli")
        assert "applied" in result
        saved = yaml.safe_load((home / "config.yaml").read_text())["delegation"]
        assert saved["backend"] == "model_map"
        assert saved["backends"]["model_map"]["options"] == [route("new")]
        assert options() == ["old"]
        assert apply_requested_tool_rebuild(agent)
        assert options() == ["new"]
    finally:
        manager.unload()
