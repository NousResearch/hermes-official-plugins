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


def test_secret_scripts_allow_plain_http_only_when_explicitly_opted_in(camofox, write_config, monkeypatch):
    from camofox_plugin.camofox_vault import secret_transport_ok

    # Default unchanged, regardless of what CAMOFOX_URL points at; an env var does not opt in.
    monkeypatch.setenv("CAMOFOX_URL", "http://test-browser-host:9377")
    monkeypatch.setenv("CAMOFOX_ALLOW_INSECURE_TRANSPORT", "true")
    assert secret_transport_ok("http://test-browser-host:9377") is False
    write_config("browser:\n  camofox:\n    allow_insecure_transport: \"false\"\n")
    assert secret_transport_ok("http://test-browser-host:9377") is False  # only a real boolean opts in

    write_config("browser:\n  camofox:\n    allow_insecure_transport: true\n")
    assert secret_transport_ok("http://test-browser-host:9377") is True
    assert secret_transport_ok("http://test-browser-host:9377/tabs") is True  # a path is fine, as on loopback
    assert secret_transport_ok("http://10.0.0.5:9377") is True
    assert secret_transport_ok("http://test-browser-host:9377?x=1") is False  # query stays refused
    assert secret_transport_ok("http://test-browser-host:9377#frag") is False  # fragment stays refused
    assert secret_transport_ok("http://u:p@test-browser-host:9377") is False  # userinfo stays refused
    assert secret_transport_ok("ftp://test-browser-host:9377") is False  # only plain HTTP is admitted
    assert secret_transport_ok("https://camofox.internal") is True


def test_secret_scripts_ignore_environment_proxies(camofox, backend, monkeypatch):
    """HTTP_PROXY would carry the fill script in clear text past the loopback check."""
    from camofox_plugin import camofox_tools as t

    assert json.loads(t.browser_navigate("https://example.com", task_id="t1"))["success"]
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # nothing listens: a proxied call fails
    monkeypatch.setenv("NO_PROXY", "")
    assert backend.evaluate("t1", "1", secret=True) == camofox.evaluate_result
