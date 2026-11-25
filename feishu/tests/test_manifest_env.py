"""The Channels card shows ``dashboard_env_vars``; a field gets its label and masking only from the
manifest's ``requires_env`` / ``optional_env`` declaration, so every card field must be declared there and
every secret the adapter reads must be a password field."""

import importlib
from pathlib import Path

import hermes_yaml as yaml

SECRETS = {"FEISHU_APP_SECRET", "FEISHU_ENCRYPT_KEY", "FEISHU_VERIFICATION_TOKEN"}


def _declared():
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8"))
    return {e["name"]: e for e in [*manifest.get("requires_env", []), *manifest.get("optional_env", [])]}


def test_secrets_are_declared_password_fields():
    declared = _declared()
    for name in SECRETS:
        assert declared.get(name, {}).get("password") is True, name


def test_every_card_field_is_declared_with_a_label():
    declared = _declared()
    card = importlib.import_module("feishu_plugin.dashboard_card").DASHBOARD_CARD
    for name in card["dashboard_env_vars"]:
        assert declared.get(name, {}).get("prompt") and declared[name].get("description"), name
