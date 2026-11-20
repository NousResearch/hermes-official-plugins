"""The pricing lookup sends the key core sends to the main endpoint, and nothing else.

Hermes prices a custom endpoint from its OpenAI-compatible ``/models`` list. A keyed endpoint (LiteLLM,
vLLM with ``--api-key``) answers that list with 401 without the key, so a keyless lookup fails each time
the cached list is cold or expired: the endpoint logs a rejected request and Langfuse gets no cost. The
plugin must send the key core's own pricing lookup sends, never send it to another host, never send a
saved key beside a live credential source, and keep exporting usage when the config backend is down.
"""
from __future__ import annotations

import importlib
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

KEY = "sk-test-0f3b9c2e7a51"
USAGE = {"input_tokens": 1000, "output_tokens": 500, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "reasoning_tokens": 0, "request_count": 1}


def _fresh_plugin():
    sys.modules.pop("langfuse_plugin", None)
    return importlib.import_module("langfuse_plugin")


def _host():
    # Core keeps the fetched model list in a process-wide cache keyed by URL and credential, so every
    # test uses its own hosts: a warm cache from an earlier test would skip the request under test.
    return f"http://llm-{uuid.uuid4().hex[:12]}.pricing-key.test"


@pytest.fixture
def endpoint(monkeypatch):
    """A keyed main endpoint at a fresh host, behind a transport-level mock that records each request.

    Mocking the transport (not ``Client.send``) keeps core's request hooks in the path, including the
    one that strips credentials from a request that leaves the original origin.
    """
    main = _host()
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        f"model:\n  provider: custom\n  base_url: {main}/v1\n  default: priced-model\n  api_key: {KEY}\n",
        encoding="utf-8")
    seen: list[tuple[str, str]] = []

    def handle_request(self, request):
        auth = request.headers.get("authorization", "")
        seen.append((str(request.url), auth))
        if not request.url.path.endswith("/models"):
            return httpx.Response(404, request=request)
        if auth != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": "No api key passed in."}, request=request)
        priced = {"id": "priced-model", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
        return httpx.Response(200, json={"object": "list", "data": [priced]}, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    return main, seen


def _export(mod, base_url):
    return mod._usage_and_cost(None, provider="custom", model="priced-model", base_url=base_url, usage=USAGE)


def test_a_keyed_main_endpoint_is_priced_with_its_key(endpoint):
    main, seen = endpoint
    mod = _fresh_plugin()

    usage_details, cost_details = _export(mod, f"{main}/v1")

    assert seen == [(f"{main}/v1/models", f"Bearer {KEY}")]
    assert usage_details == {"input": 1000, "output": 500}
    # 1000 * 1e-6 + 500 * 2e-6: the rates come from the authenticated list.
    assert cost_details == pytest.approx({"total": 0.002, "input": 0.001, "output": 0.001})


def test_another_host_never_gets_the_main_key(endpoint):
    main, seen = endpoint
    other = _host()
    mod = _fresh_plugin()

    usage_details, cost_details = _export(mod, f"{other}/v1")

    assert seen and seen[0][0] == f"{other}/v1/models"
    assert all(url.startswith(other) and auth == "" for url, auth in seen)
    assert usage_details == {"input": 1000, "output": 500}
    assert cost_details == {}


def test_no_saved_key_goes_beside_a_live_credential_source(endpoint):
    main, seen = endpoint
    mod = _fresh_plugin()
    from agent.auxiliary_client import reset_runtime_main, set_runtime_main

    # The live main runtime authenticates with a credential source (a key_cmd token source), not the
    # saved key: core sends no saved key beside it, so neither may the plugin.
    token = set_runtime_main("custom", "priced-model", base_url=f"{main}/v1", api_key=lambda: "minted-token")
    try:
        usage_details, cost_details = _export(mod, f"{main}/v1")
    finally:
        reset_runtime_main(token)

    assert seen and seen[0][0] == f"{main}/v1/models"
    assert all(KEY not in auth for _, auth in seen)
    assert usage_details == {"input": 1000, "output": 500}
    assert cost_details == {}


def test_usage_is_exported_when_the_config_backend_is_down(endpoint, monkeypatch):
    main, seen = endpoint
    mod = _fresh_plugin()
    import hermes_cli.config as config
    from hermes_cli.config_backend import ConfigBackendUnavailable

    def unavailable(*_args, **_kwargs):
        raise ConfigBackendUnavailable("config backend unreachable")

    monkeypatch.setattr(config, "load_config", unavailable)

    usage_details, cost_details = _export(mod, f"{main}/v1")

    assert usage_details == {"input": 1000, "output": 500}
    assert all(KEY not in auth for _, auth in seen)
    assert cost_details == {}


def test_a_core_without_the_key_lookup_prices_without_a_key(endpoint, monkeypatch):
    """Core before v0.23.3 has no ``_pricing_api_key``: the plugin prices without a key, as before."""
    main, seen = endpoint
    mod = _fresh_plugin()
    import agent.aux_accounting as aux_accounting

    monkeypatch.delattr(aux_accounting, "_pricing_api_key")

    usage_details, cost_details = _export(mod, f"{main}/v1")

    assert seen and seen[0][0] == f"{main}/v1/models"
    assert all(auth == "" for _, auth in seen)
    assert usage_details == {"input": 1000, "output": 500}
    assert cost_details == {}
