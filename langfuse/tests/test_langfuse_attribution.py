"""Trace attribution: which profile a turn ran for (hermes-agent#135213).

Several profiles can share one Langfuse project (and one gateway, when multiplexed). Driven
through the real ``pre_api_request`` hook against the per-test HERMES_HOME; only the SDK client
is replaced by a recorder.
"""
from __future__ import annotations

import contextlib
import importlib
import sys

import pytest


class _Span:
    def update(self, **kw):
        pass

    def set_trace_io(self, **kw):
        pass

    def end(self, **kw):
        pass

    def start_observation(self, **kw):
        return _Span()


class _Client:
    def __init__(self, roots):
        self._roots = roots

    def create_trace_id(self, seed=None):
        return f"trace::{seed}"

    def start_observation(self, **kw):
        self._roots.append(kw)
        return _Span()

    def flush(self):
        pass


@pytest.fixture
def traced(tmp_path, monkeypatch):
    """Plugin wired to a recording client; returns (module, roots, propagated, home)."""
    home = tmp_path / "hermes"
    (home / "profiles" / "arkana").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))  # so HERMES_HOME (tmp/hermes) is ~/.hermes
    for name in ("HERMES_PROFILE", "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(name, raising=False)
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    roots: list = []
    propagated: list = []

    @contextlib.contextmanager
    def _propagate(**kw):
        propagated.append(kw)
        yield

    monkeypatch.setattr(mod, "_get_langfuse", lambda: _Client(roots))
    monkeypatch.setattr(mod, "propagate_attributes", _propagate)
    # Register through a real PluginContext: profile / cron identity come from its public seams.
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    mod.register(PluginContext(PluginManifest(name="langfuse"), PluginManager()))
    mod._TRACE_STATE.clear()
    mod._SENDER_BY_TURN.clear()
    mod._USER_BY_CHILD_SESSION.clear()
    return mod, roots, propagated, home


def _open_turn(mod, session="s1", platform="cli"):
    mod.on_pre_llm_request(
        task_id=session, session_id=session, platform=platform, model="m", provider="p", api_mode="chat",
        api_call_count=1, request_messages=[{"role": "user", "content": "hi"}],
        turn_id=f"{session}:t1", api_request_id=f"{session}:t1:api:1",
    )


def test_default_profile_is_named(traced):
    mod, roots, propagated, _ = traced
    _open_turn(mod)
    assert roots[0]["metadata"]["profile"] == "default"
    assert propagated[0]["metadata"] == {"profile": "default"}


def test_routed_turn_names_the_served_profile_not_the_launch_one(traced, monkeypatch):
    mod, roots, propagated, home = traced
    # The launch profile's env pin must not relabel a routed turn.
    monkeypatch.setenv("HERMES_PROFILE", "default")
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(home / "profiles" / "arkana"))
    try:
        _open_turn(mod, session="s2")
    finally:
        reset_hermes_home_override(token)
    assert roots[0]["metadata"]["profile"] == "arkana"
    assert propagated[0]["metadata"] == {"profile": "arkana"}


def test_kanban_worker_trace_names_its_board_task_and_run(traced, monkeypatch):
    """hermes-agent#68384 / #72990: the dispatcher hands a worker HERMES_KANBAN_TASK / _RUN_ID, so its
    traces can be joined back to the board card."""
    mod, roots, propagated, _ = traced
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_8f2c")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    _open_turn(mod)
    assert roots[0]["metadata"]["kanban_task_id"] == "t_8f2c" and roots[0]["metadata"]["kanban_run_id"] == "42"
    assert propagated[0]["metadata"] == {"profile": "default", "kanban_task_id": "t_8f2c", "kanban_run_id": "42"}


# Platform / trigger / cron-job tags (hermes-agent#102755, #74930, #88502).

def test_interactive_turn_is_tagged_with_platform_and_trigger(traced):
    mod, _, propagated, _ = traced
    # A cron-shaped session id outside a cron execution must not be tagged with a job.
    _open_turn(mod, session="cron_lookalike_20260101_000000", platform="Telegram")
    assert propagated[0]["tags"] == ["hermes", "langfuse", "platform:telegram", "trigger:interactive"]


def test_cron_run_is_tagged_with_its_stable_job_id(traced):
    mod, _, propagated, _ = traced
    from cron.execution_identity import enter_cron_execution, exit_cron_execution

    # The scheduler binds the running job; legacy / hand-edited ids (not 12-hex) are valid too.
    token = enter_cron_execution({"id": "test-job"}, "exec-1", {"source": "direct"})
    try:
        _open_turn(mod, session="cron_test-job_20261012_093000", platform="cron")
    finally:
        exit_cron_execution(token)
    assert propagated[0]["tags"] == ["hermes", "langfuse", "platform:cron", "trigger:cron", "cron-job:test-job"]


# End-user attribution: Langfuse userId is the messaging sender (hermes-agent#44639), only after the
# profile opts in through plugins.entries.langfuse.settings.end_user_attribution.

def _announce_turn(mod, session="s1", sender="@alice:example.org"):
    """The turn-scoped pre_llm_call: no API messages, but it is the hook that names the sender."""
    mod.on_pre_llm_call(session_id=session, task_id=session, turn_id=f"{session}:t1", platform="matrix",
                        user_message="hi", conversation_history=[], sender_id=sender)


def _set_attribution(home, value):
    (home / "config.yaml").write_text(
        f"plugins:\n  entries:\n    langfuse:\n      settings:\n        end_user_attribution: {value}\n",
        encoding="utf-8")


@pytest.fixture
def opted_in(traced):
    _set_attribution(traced[3], "true")
    return traced


def test_sender_is_not_exported_without_the_opt_in(traced):
    mod, _, propagated, _ = traced
    _announce_turn(mod)
    _open_turn(mod, platform="matrix")
    assert "user_id" not in propagated[0]
    assert mod._SENDER_BY_TURN == {}  # dropped at the hook, not just left off the trace


def test_explicit_opt_out_exports_no_sender(traced):
    mod, _, propagated, home = traced
    _set_attribution(home, "false")
    _announce_turn(mod)
    _open_turn(mod, platform="matrix")
    assert "user_id" not in propagated[0]


def test_malformed_setting_warns_once_and_exports_no_sender(traced, caplog):
    mod, _, propagated, home = traced
    _set_attribution(home, '"yes please"')
    for session in ("s1", "s2"):
        _announce_turn(mod, session=session)
        _open_turn(mod, session=session, platform="matrix")
    assert all("user_id" not in p for p in propagated)
    warnings = [r for r in caplog.records if "end_user_attribution" in r.getMessage()]
    assert len(warnings) == 1 and "hermes config set" in warnings[0].getMessage()


def test_routed_turn_follows_the_served_profiles_opt_in(traced):
    """Under multiplexing each profile decides for itself (A, then B, then A again): the launch
    profile's opt-in must not export the sender of a turn served for a profile that never opted in,
    and serving that profile must not switch attribution off for the launch profile's next turn."""
    mod, _, propagated, home = traced
    _set_attribution(home, "true")
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    _announce_turn(mod, session="s1")
    _open_turn(mod, session="s1", platform="matrix")
    token = set_hermes_home_override(str(home / "profiles" / "arkana"))
    try:
        _announce_turn(mod, session="s2")
        _open_turn(mod, session="s2", platform="matrix")
    finally:
        reset_hermes_home_override(token)
    _announce_turn(mod, session="s3")
    _open_turn(mod, session="s3", platform="matrix")
    users = {p["session_id"]: p.get("user_id") for p in propagated}
    assert users == {"s1": "@alice:example.org", "s2": None, "s3": "@alice:example.org"}


@pytest.mark.parametrize("settings, expected", [
    ("\n  entries:\n    langfuse:\n      settings:\n        end_user_attribution: true", "@alice:example.org"),
    ("", None),
], ids=["opted-in", "default"])
def test_installed_plugin_reads_its_own_setting(tmp_path, monkeypatch, settings, expected):
    """Loaded the way Hermes loads it (copied into the profile's plugins/ dir, enabled in config.yaml),
    the plugin finds the setting under its installed id, and the real hooks carry the sender."""
    import shutil
    from pathlib import Path

    from hermes_cli import plugins as plugins_mod

    home = tmp_path / "hermes"
    shutil.copytree(Path(__file__).resolve().parents[1], home / "plugins" / "langfuse",
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))
    (home / "config.yaml").write_text(f"plugins:\n  enabled: [langfuse]{settings}\n", encoding="utf-8")
    manager = plugins_mod.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["langfuse"]
    assert loaded.enabled, loaded.error
    mod = loaded.module
    roots: list = []
    propagated: list = []

    @contextlib.contextmanager
    def _propagate(**kw):
        propagated.append(kw)
        yield

    monkeypatch.setattr(mod, "_get_langfuse", lambda: _Client(roots))
    monkeypatch.setattr(mod, "propagate_attributes", _propagate)
    manager.invoke_hook("pre_llm_call", session_id="s1", task_id="s1", turn_id="s1:t1", platform="matrix",
                        user_message="hi", conversation_history=[], sender_id="@alice:example.org")
    manager.invoke_hook("pre_api_request", task_id="s1", session_id="s1", platform="matrix", model="m",
                        provider="p", api_mode="chat", api_call_count=1, turn_id="s1:t1",
                        api_request_id="s1:t1:api:1", request_messages=[{"role": "user", "content": "hi"}])
    assert len(roots) == 1
    assert propagated[0].get("user_id") == expected


def test_gateway_turn_is_attributed_to_its_sender(opted_in):
    mod, roots, propagated, _ = opted_in
    _announce_turn(mod)
    assert roots == []  # the turn-scoped hook must still not open an orphan root
    _open_turn(mod, platform="matrix")
    assert propagated[0]["user_id"] == "@alice:example.org"
    assert propagated[0]["metadata"] == {"profile": "default"}


def test_senderless_turn_leaves_user_unset(opted_in):
    mod, _, propagated, _ = opted_in
    _announce_turn(mod, sender="")
    _open_turn(mod)
    assert "user_id" not in propagated[0]


def test_sender_does_not_leak_into_the_next_turn(opted_in):
    mod, _, propagated, _ = opted_in
    _announce_turn(mod)
    _open_turn(mod)
    mod._finish_trace(mod._trace_key("s1", "s1", turn_id="s1:t1"))
    assert mod._SENDER_BY_TURN == {}
    mod.on_pre_llm_request(task_id="s1", session_id="s1", model="m", provider="p", api_mode="chat", api_call_count=1,
                           request_messages=[{"role": "user", "content": "hi"}],
                           turn_id="s1:t2", api_request_id="s1:t2:api:1")
    assert "user_id" not in propagated[-1]


def test_subagent_traces_inherit_the_spawning_user(opted_in):
    mod, _, propagated, _ = opted_in
    _announce_turn(mod)
    _open_turn(mod, platform="matrix")
    mod.on_subagent_start(parent_turn_id="s1:t1", child_session_id="child-1", child_role="leaf", child_goal="go")
    mod.on_pre_llm_request(task_id="child-1", session_id="child-1", platform="subagent", model="m", provider="p",
                           api_mode="chat", api_call_count=1, request_messages=[{"role": "user", "content": "go"}],
                           turn_id="child-1:t1", api_request_id="child-1:t1:api:1")
    assert propagated[-1]["session_id"] == "child-1" and propagated[-1]["user_id"] == "@alice:example.org"
    mod.on_subagent_stop(parent_turn_id="s1:t1", child_session_id="child-1", child_role="leaf")
    assert mod._USER_BY_CHILD_SESSION == {}


def test_sender_stash_is_bounded(opted_in, monkeypatch):
    mod, _, _, _ = opted_in
    monkeypatch.setattr(mod, "_MAX_TRACE_STATE", 4)
    mod._SENDER_BY_TURN.clear()
    for i in range(10):
        mod.on_pre_llm_call(session_id="s", turn_id=f"s:t{i}", sender_id=f"u{i}")
    assert list(mod._SENDER_BY_TURN) == ["s:t6", "s:t7", "s:t8", "s:t9"]


# Responses API tool items in generation input (hermes-agent#84186).

_RESPONSES_ITEMS = [
    {"type": "function_call", "call_id": "call-1", "name": "web_extract",
     "arguments": '{"urls": ["https://example.com"]}'},
    {"type": "function_call_output", "call_id": "call-1", "output": '{"ok": true}'},
]


def test_serialize_messages_preserves_responses_tool_items():
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    assert mod._serialize_messages(_RESPONSES_ITEMS) == [
        {"type": "function_call", "call_id": "call-1", "name": "web_extract",
         "arguments": {"urls": ["https://example.com"]}},
        {"type": "function_call_output", "call_id": "call-1", "output": {"ok": True}},
    ]


def test_responses_tool_payloads_follow_metadata_capture_mode(monkeypatch):
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", "metadata")
    sys.modules.pop("langfuse_plugin", None)
    mod = importlib.import_module("langfuse_plugin")
    call, output = mod._serialize_messages(_RESPONSES_ITEMS)
    assert call["arguments"] == {"omitted": True, "type": "text", "chars": 33}
    assert output["output"]["omitted"] is True and output["call_id"] == "call-1"


def test_responses_reasoning_and_compaction_items_keep_their_type(traced, monkeypatch):
    """Codex Responses input also replays reasoning / compaction items (hermes-agent#84186): they must
    not export as {"role": None, "content": None}, and their sealed encrypted_content stays out."""
    mod, _, _, _ = traced
    children: list = []
    monkeypatch.setattr(_Span, "start_observation", lambda self, **kw: children.append(kw) or _Span())
    mod.on_pre_llm_request(
        task_id="s1", session_id="s1", model="m", provider="p", api_mode="codex_responses", api_call_count=1,
        turn_id="s1:t1", api_request_id="s1:t1:api:1", request_messages=[
            {"role": "user", "content": "hi"},
            {"type": "reasoning", "encrypted_content": "gAAAA-sealed",
             "summary": [{"type": "summary_text", "text": "checked the docs"}]},
            {"type": "compaction", "encrypted_content": "gAAAA-sealed"},
        ],
    )
    assert children[0]["input"][1:] == [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "checked the docs"}]},
        {"type": "compaction"},
    ]
