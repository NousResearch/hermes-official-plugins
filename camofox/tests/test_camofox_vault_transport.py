"""Which Camofox hosts may receive a credential-bearing script over plain HTTP.

The vault sends a fill script with the user's password in it. Plain HTTP is only
acceptable on loopback, or on a host the operator named in the profile's own
config (a self-hosted Camofox in its own container is reached by container name).

These tests do not need the ``agent.browser_page_backend`` seam: they exercise the
transport decision on its own.
"""

import pytest


@pytest.mark.parametrize("url, ok", [
    ("http://127.0.0.1:9377", True), ("http://localhost:9377", True), ("https://camofox.internal", True),
    ("http://10.0.0.5:9377", False), ("http://camofox:9377", False), ("https://u:p@camofox.internal", False),
])
def test_secret_scripts_need_loopback_or_https(url, ok):
    from camofox_plugin.camofox_vault import secret_transport_ok
    assert secret_transport_ok(url) is ok


def test_an_allowed_host_may_receive_a_secret_over_plain_http(camofox, monkeypatch, write_config):
    """A self-hosted Camofox in its own container is reached by container name over plain HTTP.

    The operator names that host in the profile's own config; every other host keeps the
    loopback-or-HTTPS rule.
    """
    from camofox_plugin.camofox_vault import secret_transport_ok

    assert secret_transport_ok("http://camofox:9377") is False
    write_config("browser:\n  camofox:\n    allowed_plain_http_hosts:\n      - camofox\n")
    assert secret_transport_ok("http://camofox:9377") is True
    assert secret_transport_ok("http://camofox:9377/path") is True
    assert secret_transport_ok("http://other-host:9377") is False
    # A host in the allowlist does not make userinfo, query or fragment acceptable.
    assert secret_transport_ok("http://u:p@camofox:9377") is False
    assert secret_transport_ok("http://camofox:9377?token=1") is False


def test_the_env_allowlist_wins_over_the_config_one(camofox, monkeypatch, write_config):
    from camofox_plugin.camofox_vault import secret_transport_ok

    write_config("browser:\n  camofox:\n    allowed_plain_http_hosts:\n      - config-host\n")
    assert secret_transport_ok("http://config-host:9377") is True
    monkeypatch.setenv("CAMOFOX_ALLOWED_PLAIN_HTTP_HOSTS", "env-host, other-host")
    assert secret_transport_ok("http://env-host:9377") is True
    assert secret_transport_ok("http://other-host:9377") is True
    assert secret_transport_ok("http://config-host:9377") is False


def test_the_allowlist_is_case_insensitive(camofox, monkeypatch):
    from camofox_plugin.camofox_vault import secret_transport_ok

    monkeypatch.setenv("CAMOFOX_ALLOWED_PLAIN_HTTP_HOSTS", "Camofox")
    assert secret_transport_ok("http://camofox:9377") is True
    assert secret_transport_ok("http://CAMOFOX:9377") is True


def test_a_comma_separated_config_string_also_works(camofox, monkeypatch, write_config):
    from camofox_plugin.camofox_vault import secret_transport_ok

    write_config("browser:\n  camofox:\n    allowed_plain_http_hosts: camofox, other-host\n")
    assert secret_transport_ok("http://camofox:9377") is True
    assert secret_transport_ok("http://other-host:9377") is True
    assert secret_transport_ok("http://third-host:9377") is False
