"""Tests that HERMES_LANGFUSE_SAMPLE_RATE rejects non-finite and out-of-range values."""
import importlib

import pytest


@pytest.mark.parametrize("bad_rate", ["nan", "inf", "-inf", "-0.1", "1.5", "abc"])
def test_build_client_ignores_out_of_range_or_non_finite_sample_rate(monkeypatch, bad_rate):
    plugin = importlib.import_module("langfuse_plugin")
    captured = {}

    class FakeLangfuse:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(plugin, "Langfuse", FakeLangfuse)
    monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("HERMES_LANGFUSE_SAMPLE_RATE", bad_rate)

    client = plugin._build_client()
    assert client is not None
    assert "sample_rate" not in captured


def test_build_client_accepts_valid_sample_rate(monkeypatch):
    plugin = importlib.import_module("langfuse_plugin")
    captured = {}

    class FakeLangfuse:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(plugin, "Langfuse", FakeLangfuse)
    monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("HERMES_LANGFUSE_SAMPLE_RATE", "0.25")

    client = plugin._build_client()
    assert client is not None
    assert captured["sample_rate"] == 0.25
