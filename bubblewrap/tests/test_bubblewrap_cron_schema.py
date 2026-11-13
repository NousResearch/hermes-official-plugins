"""Manifest/load checks that run without bwrap or a working user namespace."""

from pathlib import Path

import pytest
import yaml

from bubblewrap_plugin.sandbox import load_bubblewrap_config


MANIFEST = Path(__file__).resolve().parents[1] / "plugin.yaml"


def test_plugin_yaml_loads_with_pyyaml():
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    assert manifest["name"] == "bubblewrap"
    assert isinstance(manifest["config_schema"], dict)


def test_cron_dir_config_schema_matches_runtime_defaults():
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    schema = manifest["config_schema"]["cron_dir"]
    assert schema["type"] == "str"
    assert schema["default"] == ""
    assert schema["label"] == "Sandbox cron directory"
    assert "does not relocate the host scheduler's cron storage" in schema["description"]
    assert load_bubblewrap_config({}).cron_dir == schema["default"]
    assert load_bubblewrap_config({"cron_dir": schema["default"]}).cron_dir == ""
    assert load_bubblewrap_config({"cron_dir": "/external/cron"}).cron_dir == "/external/cron"


@pytest.mark.parametrize("value", [True, 123, [], {}])
def test_cron_dir_runtime_rejects_non_string_schema_values(value):
    with pytest.raises(ValueError, match="cron_dir must be a string path or empty"):
        load_bubblewrap_config({"cron_dir": value})
