"""Deployment labels on a trace: ``service`` / ``host`` / ``environment``.

Langfuse stores ``metadata`` as a flat ``Map(String,String)``. The OTel resource's
``service.name`` is not selectable through the UI or the Metrics API, so a deployment that wants
"which agent on which host" needs those three as metadata keys. Driven through the real
``pre_api_request`` hook; only the SDK client is replaced by a recorder.
"""
from __future__ import annotations

import contextlib
import importlib
import socket
import sys

import pytest

_LABELS = ("service", "host", "environment")


class _Span:
    def __init__(self, children=None):
        self._children = children

    def update(self, **kw):
        pass

    def set_trace_io(self, **kw):
        pass

    def end(self, **kw):
        pass

    def start_observation(self, **kw):
        if self._children is not None:
            self._children.append(kw)
        return _Span(self._children)


class _Client:
    def __init__(self, roots, children):
        self._roots = roots
        self._children = children

    def create_trace_id(self, seed=None):
        return f"trace::{seed}"

    def start_observation(self, **kw):
        self._roots.append(kw)
        return _Span(self._children)

    def flush(self):
        pass


@pytest.fixture
def traced(tmp_path, monkeypatch):
    """Plugin wired to a recording client; returns (module, roots, propagated, children)."""
    home = tmp_path / "hermes"
    (home / "profiles" / "arkana").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))  # so HERMES_HOME (tmp/hermes) is ~/.hermes
    for name in ("HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_LANGFUSE_SERVICE",
                 "HERMES_LANGFUSE_HOST", "HERMES_LANGFUSE_ENV", "OTEL_SERVICE_NAME",
                 "LANGFUSE_ENV", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    roots: list = []
    propagated: list = []
    children: list = []

    @contextlib.contextmanager
    def _propagate(**kw):
        propagated.append(kw)
        yield

    monkeypatch.setattr(mod, "_get_langfuse", lambda: _Client(roots, children))
    monkeypatch.setattr(mod, "propagate_attributes", _propagate)
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    mod.register(PluginContext(PluginManifest(name="langfuse"), PluginManager()))
    mod._TRACE_STATE.clear()
    return mod, roots, propagated, children


def _open_turn(mod, session="s1", platform="cli"):
    mod.on_pre_llm_request(
        task_id=session, session_id=session, platform=platform, model="m", provider="p", api_mode="chat",
        api_call_count=1, request_messages=[{"role": "user", "content": "hi"}],
        turn_id=f"{session}:t1", api_request_id=f"{session}:t1:api:1",
    )


# ---------------------------------------------------------------------------
# The labels a deployment filters on.
# ---------------------------------------------------------------------------

def test_operator_overrides_reach_root_and_propagated_metadata(traced, monkeypatch):
    mod, roots, propagated, _ = traced
    monkeypatch.setenv("HERMES_LANGFUSE_SERVICE", "hermes-gateway")
    monkeypatch.setenv("HERMES_LANGFUSE_HOST", "hermes.gdev")
    monkeypatch.setenv("HERMES_LANGFUSE_ENV", "homelab")
    _open_turn(mod)

    assert {k: roots[0]["metadata"][k] for k in _LABELS} == {
        "service": "hermes-gateway", "host": "hermes.gdev", "environment": "homelab"}
    assert {k: propagated[0]["metadata"][k] for k in _LABELS} == {
        "service": "hermes-gateway", "host": "hermes.gdev", "environment": "homelab"}


def test_children_carry_the_same_labels(traced, monkeypatch):
    """propagate_attributes() covers session/tags/user only, so the labels have to be stamped on
    each child observation — otherwise a per-service Metrics API query sees roots but no tokens."""
    mod, _, _, children = traced
    monkeypatch.setenv("HERMES_LANGFUSE_SERVICE", "hermes-gateway")
    monkeypatch.setenv("HERMES_LANGFUSE_HOST", "hermes.gdev")
    monkeypatch.setenv("HERMES_LANGFUSE_ENV", "homelab")
    _open_turn(mod)
    assert children, "no child observation was opened"
    child = children[0]
    assert {k: child["metadata"][k] for k in _LABELS} == {
        "service": "hermes-gateway", "host": "hermes.gdev", "environment": "homelab"}
    # The child keeps its own metadata as well; the labels are additive, never a replacement.
    assert child["metadata"]["provider"] == "p"


def test_host_falls_back_to_this_machines_hostname(traced):
    mod, roots, _, _ = traced
    _open_turn(mod)
    assert roots[0]["metadata"]["host"] == socket.gethostname()


def test_service_defaults_to_hermes_agent(traced):
    mod, roots, _, _ = traced
    _open_turn(mod)
    assert roots[0]["metadata"]["service"] == "hermes-agent"


def test_plugin_service_override_beats_otel_service_name(traced, monkeypatch):
    """An operator who set OTEL_SERVICE_NAME for their own exporter keeps it as the fallback, but the
    plugin's own variable is the more specific answer."""
    mod, roots, _, _ = traced
    monkeypatch.setenv("OTEL_SERVICE_NAME", "otel-name")
    _open_turn(mod)
    assert roots[0]["metadata"]["service"] == "otel-name"

    mod._TRACE_STATE.clear()
    monkeypatch.setenv("HERMES_LANGFUSE_SERVICE", "hermes-plugin-name")
    _open_turn(mod, session="s2")
    assert roots[1]["metadata"]["service"] == "hermes-plugin-name"


def test_unset_environment_is_omitted_not_invented(traced):
    mod, roots, _, _ = traced
    _open_turn(mod)
    assert "environment" not in roots[0]["metadata"]


def test_values_are_collapsed_to_one_bounded_line(traced, monkeypatch):
    mod, roots, _, _ = traced
    monkeypatch.setenv("HERMES_LANGFUSE_HOST", "  host\twith\nnewlines  ")
    long_label = "x" * 500
    monkeypatch.setenv("HERMES_LANGFUSE_ENV", long_label)
    _open_turn(mod)
    assert roots[0]["metadata"]["host"] == "host with newlines"
    assert roots[0]["metadata"]["environment"] == long_label[:mod._RESOURCE_VALUE_MAX_CHARS]


def test_only_the_three_labels_are_added_to_metadata(traced, monkeypatch):
    """Nothing else from the environment may leak into a trace: the reader is a fixed whitelist."""
    mod, roots, _, _ = traced
    canary = "not-a-credential-but-must-not-ship"
    monkeypatch.setenv("SOME_UNRELATED_VARIABLE", canary)
    _open_turn(mod)
    metadata = roots[0]["metadata"]
    assert not any(canary in str(value) for value in metadata.values())
    assert set(_LABELS) & set(metadata) == {"service", "host"}


# ---------------------------------------------------------------------------
# Fail-open: a label that cannot be read must never cost the turn.
# ---------------------------------------------------------------------------

def test_a_raising_secret_scope_falls_back_to_the_process_env(traced, monkeypatch):
    mod, roots, _, _ = traced
    monkeypatch.setenv("HERMES_LANGFUSE_SERVICE", "hermes-cron")

    def _boom(name):
        raise RuntimeError("no scope")

    monkeypatch.setattr(mod, "_secret", _boom)
    _open_turn(mod)
    assert roots[0]["metadata"]["service"] == "hermes-cron"
    assert roots[0]["metadata"]["host"] == socket.gethostname()


def test_gethostname_failure_drops_only_the_host_key(traced, monkeypatch):
    mod, roots, propagated, _ = traced
    monkeypatch.setenv("HERMES_LANGFUSE_ENV", "homelab")

    def _boom():
        raise OSError("no hostname")

    monkeypatch.setattr(mod.socket, "gethostname", _boom)
    _open_turn(mod)
    assert "host" not in roots[0]["metadata"]
    assert roots[0]["metadata"]["service"] == "hermes-agent"
    assert propagated[0]["metadata"]["environment"] == "homelab"


def test_resource_metadata_returns_a_flat_string_map(traced, monkeypatch):
    """Langfuse's Map(String,String) takes strings only; a non-str label would be dropped server-side."""
    mod, _, _, _ = traced
    monkeypatch.setenv("HERMES_LANGFUSE_ENV", "homelab")
    labels = mod._resource_metadata()
    assert labels and all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items())
