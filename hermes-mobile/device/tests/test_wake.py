"""Waking the phone through the push relay with this profile's credentials."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hermes_mobile_wake_plugin", ROOT / "__init__.py",
                                              submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

SECRETS = {"HERMES_MOBILE_PUSH_RELAY_URL": "https://relay.example/", "HERMES_MOBILE_PUSH_INSTALL_KEY": "install-key"}


def scope(monkeypatch, secrets):
    module = types.ModuleType("agent.secret_scope")
    module.get_secret = lambda name, default=None: secrets.get(name, default)
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *error):
        return False

    def read(self, size):
        return b"{}"


def test_wake_uses_the_profiles_scoped_credentials(monkeypatch):
    for name in SECRETS:
        monkeypatch.setenv(name, "from-the-launch-process")
    scope(monkeypatch, SECRETS)
    sent = []
    monkeypatch.setattr(plugin.urllib.request, "urlopen", lambda request, timeout: sent.append(request) or Response())
    assert plugin.wake("phone").startswith("Background refresh requested")
    assert [request.full_url for request in sent] == ["https://relay.example/devices/phone/wake"]
    assert sent[0].get_header("Authorization") == "Bearer install-key"


def test_wake_without_scoped_credentials_is_not_configured(monkeypatch):
    for name in SECRETS:
        monkeypatch.setenv(name, "from-the-launch-process")
    scope(monkeypatch, {})
    assert plugin.wake("phone").startswith("Push is not configured")
    assert plugin.can_deliver_followup() is False


def test_the_device_id_is_one_path_segment(monkeypatch):
    scope(monkeypatch, SECRETS)
    sent = []
    monkeypatch.setattr(plugin.urllib.request, "urlopen", lambda request, timeout: sent.append(request) or Response())
    plugin.wake("../phone 1?x")
    assert sent[0].full_url == "https://relay.example/devices/..%2Fphone%201%3Fx/wake"



@pytest.mark.parametrize(("relay", "sent_to"), [
    ("https://relay.example", "https://relay.example/devices/phone/wake"),
    ("http://localhost:8080/", "http://localhost:8080/devices/phone/wake"),
    ("http://127.0.0.1:8080", "http://127.0.0.1:8080/devices/phone/wake"),
    ("http://[::1]:8080", "http://[::1]:8080/devices/phone/wake"),
    ("http://relay.example", None),
    ("http://localhost.relay.example", None),
    ("ftp://relay.example", None),
    ("file:///tmp/relay", None),
    ("relay.example", None),
    ("https://", None),
])
def test_the_install_key_goes_only_to_https_or_a_relay_on_this_machine(monkeypatch, relay, sent_to):
    scope(monkeypatch, {**SECRETS, "HERMES_MOBILE_PUSH_RELAY_URL": relay})
    sent = []
    monkeypatch.setattr(plugin.urllib.request, "urlopen", lambda request, timeout: sent.append(request) or Response())
    answer = plugin.wake("phone")
    if sent_to is None:
        assert sent == []
        assert answer.startswith("Background refresh unavailable: the push relay URL must start with https://")
    else:
        assert [request.full_url for request in sent] == [sent_to]
        assert answer.startswith("Background refresh requested")


def test_no_follow_up_is_promised_through_a_refused_relay(monkeypatch):
    scope(monkeypatch, {**SECRETS, "HERMES_MOBILE_PUSH_RELAY_URL": "http://relay.example"})
    assert plugin.can_deliver_followup() is False
