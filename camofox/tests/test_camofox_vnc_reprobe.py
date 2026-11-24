"""A health probe without vncPort must not cache 'no VNC' forever."""
import io
from unittest.mock import Mock

import requests

from camofox_plugin import camofox_client as client


def _health(payload):
    resp = requests.Response()
    resp.status_code = 200
    resp._content = payload
    resp.raw = io.BytesIO(payload)
    resp.close = Mock(wraps=resp.close)
    return resp


def test_vnc_url_is_reprobed_after_a_negative_health_result(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://camofox.local:9377")
    client._vnc_url_by_camofox_url.clear()
    responses = iter([_health(b'{"ok": true}'),
                      _health(b'{"ok": true, "vncPort": 6080}')])
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: next(responses))

    assert client.get_vnc_url() is None
    assert client.get_vnc_url() == "http://camofox.local:6080"
