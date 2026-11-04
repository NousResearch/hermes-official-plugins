"""Secret hygiene contract for vault fills and tool output.

C2 plaintext non-loopback refused; C3 redirects never followed with a secret; C4 ambiguous authority
refused; C5 NO environment proxy (HTTP_PROXY, HTTPS_PROXY *or* ALL_PROXY) carries a secret script;
C7/C8 registered vault values are scrubbed from snapshot and nested navigate output; C9 a numeric
vault secret is scrubbed after JavaScript Number coercion in browser_console output.
"""
import json

import pytest

pytest.importorskip("agent.browser_page_backend")


@pytest.fixture
def backend():
    from camofox_plugin.camofox_vault import make_page_backend
    return make_page_backend()


@pytest.fixture
def vault_values():
    from agent import redact
    yield redact.register_vault_redaction_value
    redact.clear_vault_redaction_values()


def _navigate(task="t1"):
    from camofox_plugin import camofox_tools as t
    assert json.loads(t.browser_navigate("https://example.com", task_id=task))["success"]


def test_c2_plaintext_non_loopback_secret_eval_sends_nothing(camofox, backend, monkeypatch):
    _navigate()
    before = len(camofox.requests)
    monkeypatch.setenv("CAMOFOX_URL", camofox.url.replace("127.0.0.1", "camofox.invalid"))
    with pytest.raises(RuntimeError):
        backend.evaluate("t1", "fill('s')", secret=True)
    assert len(camofox.requests) == before


def test_c3_secret_eval_never_follows_a_redirect(camofox, backend):
    _navigate()
    tab = camofox.calls("POST", "/tabs")[0]
    path = f"/tabs/{next(iter(camofox.tabs))}/evaluate"
    camofox.overrides[("POST", path)] = (307, {"error": "moved"})
    n = len(camofox.calls("POST", "/evaluate"))
    with pytest.raises(Exception):
        backend.evaluate("t1", "fill('secret-expression')", secret=True)
    assert len(camofox.calls("POST", "/evaluate")) == n + 1  # exactly one POST, the checked endpoint
    assert tab


def test_c4_ambiguous_authority_is_refused():
    from camofox_plugin.camofox_vault import secret_transport_ok
    assert secret_transport_ok(r"http://offhost.example\@127.0.0.1:9377") is False


@pytest.mark.parametrize("var", ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"])
def test_c5_no_environment_proxy_carries_a_secret_script(camofox, backend, monkeypatch, var):
    """Nothing listens on 127.0.0.1:9; a proxied secret POST would fail, a direct one succeeds."""
    _navigate()
    for v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
              "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv(var, "http://127.0.0.1:9")
    assert backend.evaluate("t1", "1", secret=True) == camofox.evaluate_result


def test_c7_snapshot_scrubs_registered_vault_values(camofox, vault_values):
    from camofox_plugin import camofox_tools as t
    canary = "short-snapshot-vault-canary"
    vault_values(canary)
    camofox.snapshot = f'- textbox "{canary}" [e1]'
    _navigate()
    raw = t.browser_snapshot(task_id="t1")
    assert canary not in raw and "«redacted-vault-secret»" in raw


def test_c8_navigate_output_scrubs_registered_vault_values(camofox, vault_values):
    from camofox_plugin import camofox_tools as t
    canary = "plain-vault-readback-canary"
    vault_values(canary)
    camofox.navigate_title = canary
    raw = t.browser_navigate(f"https://example.com/{canary}", task_id="t1")
    assert canary not in raw


@pytest.mark.parametrize("secret, coerced", [
    ("135790", 135790), ("013579", 13579), ("13579.", 13579), (".13579", 0.13579),
    ("0.1000000000000000055511151231257827021181583404541015625", 0.1),
    ("  42  ", 42), ("﻿135790﻿", 135790), ("0x2a", 42), ("0b101010", 42), ("0o52", 42),
])
def test_c9_numeric_vault_secret_is_scrubbed_after_js_number_coercion(camofox, vault_values, secret, coerced):
    from agent import redact
    if not hasattr(redact, "redact_registered_vault_number"):
        pytest.skip("core lacks redact_registered_vault_number (numeric scrub is a no-op there)")
    from camofox_plugin import camofox_tools as t
    vault_values(secret)
    camofox.evaluate_result = coerced
    _navigate()
    out = json.loads(t.browser_console("Number(document.querySelector('input').value)", task_id="t1"))
    assert out["result"] == "«redacted-vault-secret»"


def test_c9_unrelated_numbers_and_booleans_are_untouched(camofox, vault_values):
    from camofox_plugin import camofox_tools as t
    vault_values("135790")
    _navigate()
    for value in (246810, True):
        camofox.evaluate_result = value
        assert json.loads(t.browser_console("x", task_id="t1"))["result"] == value
