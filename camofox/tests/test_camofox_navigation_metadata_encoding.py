"""Alternate IPv4 literals (decimal, hex, octal, inet_aton) for the metadata floor.

Browsers accept them as addresses; core's always-blocked check only recognises dotted literals,
so the plugin's navigation policy must decode them before sending a request.
"""
import pytest

from camofox_plugin.camofox_policy import navigation_policy_error


@pytest.mark.parametrize("url", [
    "http://2852039166/latest/meta-data/",              # decimal 169.254.169.254
    "http://0xa9fea9fe/latest/meta-data/",              # hex
    "http://0251.0376.0251.0376/latest/meta-data/",     # dotted octal
    "http://169.254.43902/latest/meta-data/",           # mixed high-order parts
])
def test_alternate_ip_encodings_of_metadata_are_refused(url):
    _normalized, error = navigation_policy_error(url)
    assert error is not None, url
    assert "metadata" in error["error"]


def test_ordinary_public_url_still_passes():
    _normalized, error = navigation_policy_error("https://example.com/page")
    assert error is None
