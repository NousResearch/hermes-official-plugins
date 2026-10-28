"""browser_vault on Camofox: the page backend evaluates on the tab browser_navigate opened, never opens
one, and sends a credential-bearing script only to a loopback or HTTPS Camofox."""

import json

import pytest

pytest.importorskip("agent.browser_page_backend")


@pytest.fixture
def backend():
    from camofox_plugin.camofox_vault import make_page_backend
    return make_page_backend()


def test_vault_scripts_run_on_the_navigated_tab_and_never_open_one(camofox, backend):
    from camofox_plugin import camofox_tools as t

    assert backend.has_page("t1") is False
    with pytest.raises(RuntimeError):
        backend.evaluate("t1", "window.location.href", secret=False)
    assert camofox.calls("POST", "/tabs") == []  # no blank tab in the user's profile

    assert json.loads(t.browser_navigate("https://example.com", task_id="t1"))["success"]
    camofox.evaluate_result = "https://example.com/"
    assert backend.has_page("t1") is True
    assert backend.evaluate("t1", "window.location.href", secret=True) == "https://example.com/"
    tab = camofox.calls("POST", "/tabs")[0]
    evaluates = camofox.calls("POST", "/evaluate")
    assert len(camofox.calls("POST", "/tabs")) == 1 and len(evaluates) == 1
    assert evaluates[0]["path"].startswith("/tabs/tab-") and evaluates[0]["body"]["userId"] == tab["body"]["userId"]


@pytest.mark.parametrize("url, ok", [
    ("http://127.0.0.1:9377", True), ("http://localhost:9377", True), ("https://camofox.internal", True),
    ("http://10.0.0.5:9377", False), ("http://camofox:9377", False), ("https://u:p@camofox.internal", False),
])
def test_secret_scripts_need_loopback_or_https(url, ok):
    from camofox_plugin.camofox_vault import secret_transport_ok
    assert secret_transport_ok(url) is ok
