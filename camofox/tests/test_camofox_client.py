"""Camofox client plumbing: health probe."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("status", [200, 503])
def test_health_probe_closes_its_response(monkeypatch, status):
    import io
    from unittest.mock import Mock

    import requests

    from camofox_plugin import camofox_client as client

    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    response = requests.Response()
    response.status_code = status
    response._content = b'{"ok": true, "vncPort": 6080}'
    response.raw = io.BytesIO(response._content)
    response.close = Mock(wraps=response.close)
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: response)
    assert client.check_camofox_available() is (status == 200)
    response.close.assert_called_once_with()
