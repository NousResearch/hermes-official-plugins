"""The relay upstream is Anthropic unless the operator explicitly opts into a gateway.

``CLAUDE_SUBSCRIPTION_DIRECTSDK_UPSTREAM`` is the supported organizational
gateway selection. A generic inherited ``ANTHROPIC_BASE_URL`` is never read
as an upstream here: the provider guard refuses it, so an accidental value
cannot redirect native subscription credentials.
"""

import pytest

from admission import Admission, upstream_from_env


def test_default_upstream_is_anthropic():
    assert upstream_from_env({}) == "https://api.anthropic.com"
    assert upstream_from_env({"CLAUDE_SUBSCRIPTION_DIRECTSDK_UPSTREAM": "  "}) == "https://api.anthropic.com"


def test_explicit_gateway_upstream_is_used():
    env = {"CLAUDE_SUBSCRIPTION_DIRECTSDK_UPSTREAM": "https://seatbelt.example.internal"}
    assert upstream_from_env(env) == "https://seatbelt.example.internal"


def test_generic_base_url_is_not_an_upstream():
    env = {"ANTHROPIC_BASE_URL": "https://attacker.example"}
    assert upstream_from_env(env) == "https://api.anthropic.com"


def test_gateway_upstream_must_be_https_without_credentials():
    for value in (
        "http://seatbelt.example.internal",
        "https://user:pass@seatbelt.example.internal",
        "https://seatbelt.example.internal?tenant=a",
    ):
        with pytest.raises(ValueError):
            Admission(value, 5).close()
