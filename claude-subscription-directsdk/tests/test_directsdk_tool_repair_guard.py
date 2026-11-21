"""A native tool name reaches Hermes as the tool it names, never as one core's fuzzy repair picks.

Hermes' turn validation first routes a call to a Tool Search-deferred tool through the ``tool_call``
bridge, then repairs a name it does not know (case, separators, ``_tool`` suffixes, then a 0.7
fuzzy match, ``agent_runtime_helpers.repair_tool_call``) and runs the repaired tool with the
model's arguments. The plugin maps the offered name and its slips (dropped prefix #62 or server
segment #342, case, separators, suffix) onto the offered tool, hands deferred and unrepairable
names to Hermes as is (#39), and fails closed only on a name core would fuzzy-match onto a
different offered tool.
"""
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import directsdk
import test_directsdk
from test_directsdk_tool_stream import REQUEST, reply, tool_deltas, upstream

# Names core's repair only fuzzy-matches onto the offered `probe`: a different name, not a slip of it.
FUZZY_ONLY = ["mcp__hermes__prob", "probes", "mcp__hermes__prove"]
# The offered name and slips of it: each must run `probe` and reach Hermes already spelled `probe`.
SLIPS = ["mcp__hermes__probe", "probe", "mcp__probe", "Probe", "PROBE", "probe_tool", "mcp__hermes__Probe",
         "mcp__hermes__probe-tool", "ProbeTool"]


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": "TAIL",
                                             "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}}


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    # Core reads the Tool Search defer list from config.yaml: use its defaults, never the developer's.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))


def _native(tmp_path, native_name):
    fixture = test_directsdk.Contract()
    return fixture, fixture.client(str(tmp_path), TOOL_NAME=native_name, REPLAY_NAME="mcp__hermes__probe")


def _agent(plugin, request, streaming=False):
    """Core's production AIAgent loop on this plugin's client; only the tool handler is stubbed."""
    from run_agent import AIAgent

    offered = [t["function"]["name"] for t in request["tools"]]
    with (
        patch("model_tools.get_tool_definitions", return_value=request["tools"]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True, max_iterations=3)
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._api_max_retries = 1

    def create(**kwargs):
        if any(m.get("role") == "tool" for m in kwargs["messages"]):
            # The tool ran; this fake native only plays the first turn, so the closing answer is canned.
            from openai.types.chat import ChatCompletion
            return ChatCompletion.model_validate({
                "id": "done", "object": "chat.completion", "created": 0, "model": "sonnet",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        assert [t["function"]["name"] for t in kwargs["tools"]] == offered  # Hermes offered exactly these
        return plugin.chat.completions.create(**request, **({"stream": True} if streaming else {}))

    agent.client.chat.completions.create.side_effect = create
    if streaming:
        agent.stream_delta_callback = lambda text: None  # a consumer makes core stream the request
    return agent


def _run(agent):
    """Run one conversation; returns the result and every ``(tool, arguments)`` core dispatched, by either route."""
    from tools.registry import registry

    handler = MagicMock(return_value="ok")
    dispatch = MagicMock(return_value='"ok"')  # the `tool_call` bridge dispatches its tool through the registry
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("model_tools.handle_function_call", handler),
        patch.object(registry, "dispatch", dispatch),
        patch("time.sleep"),
    ):
        result = agent.run_conversation("go")
    return result, [c.args[:2] for c in handler.call_args_list + dispatch.call_args_list]


@pytest.mark.parametrize("native_name", FUZZY_ONLY)
def test_fuzzy_only_match_never_runs_the_offered_tool_in_hermes_loop(tmp_path, native_name):
    fixture, plugin = _native(tmp_path, native_name)
    try:
        _, ran = _run(_agent(plugin, fixture.request()))
    finally:
        plugin.close()
    assert ran == []


@pytest.mark.parametrize("native_name", SLIPS)
def test_offered_name_and_its_slips_run_the_tool_in_hermes_loop(tmp_path, native_name):
    fixture, plugin = _native(tmp_path, native_name)
    try:
        result, ran = _run(_agent(plugin, fixture.request()))
    finally:
        plugin.close()
    assert ran == [("probe", {"value": "x"})]
    assert result["final_response"] == "done"


@pytest.mark.parametrize("native_name", SLIPS)
def test_a_slip_reaches_hermes_as_the_offered_name(tmp_path, native_name):
    # Spelled as offered, so core logs no auto-repair, and native's replayed history carries the advertised name.
    fixture, plugin = _native(tmp_path, native_name)
    try:
        result = plugin.chat.completions.create(**fixture.request())
    finally:
        plugin.close()
    assert [c.function.name for c in result.choices[0].message.tool_calls] == ["probe"]
    assert directsdk.offered_tool_name(native_name, {"probe": "probe"}) == ("mcp__hermes__probe", "probe")


@pytest.mark.parametrize("native_name", ["read_terminal", "mcp__hermes__read_terminal"])
def test_a_deferred_tool_next_to_an_offered_near_match_runs_the_deferred_tool(tmp_path, native_name):
    # `read_terminal` is deferred by default and fuzzy-matches the always-visible `terminal`. Core routes it
    # through the offered `tool_call` bridge before it repairs anything, so it must reach Hermes as named.
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS

    fixture, plugin = _native(tmp_path, native_name)
    request = dict(fixture.request(), tools=[_tool("terminal"), _tool("tool_call")])
    read_terminal = MagicMock(return_value='"ok"')  # core dispatches this GUI tool inline, not via the registry
    try:
        with patch.dict(INLINE_TOOL_EXECUTORS, {"read_terminal": lambda agent, args, ctx: read_terminal(args)}):
            result, ran = _run(_agent(plugin, request))
    finally:
        plugin.close()
    assert ran == []  # `terminal` never ran
    assert [c.args for c in read_terminal.call_args_list] == [({"value": "x"},)]
    assert result["final_response"] == "done"


@pytest.mark.parametrize("native_name", ["read_terminal", "mcp__hermes__read_terminal", "close_terminal", "process"])
def test_a_deferred_name_reaches_hermes_unchanged_when_the_bridge_is_offered(native_name):
    # `process` is the legacy alias core canonicalizes to the deferred `process_manage` before routing.
    offered = {"terminal": "terminal", "tool_call": "tool_call"}
    assert directsdk.offered_tool_name(native_name, offered) == (native_name, native_name.removeprefix(directsdk.PREFIX))


def test_without_the_bridge_a_deferred_name_is_a_fuzzy_match_and_fails_closed(tmp_path):
    # No `tool_call` offered: core cannot route `read_terminal`, so its repair would run `terminal`.
    fixture, plugin = _native(tmp_path, "read_terminal")
    request = dict(fixture.request(), tools=[_tool("terminal")])
    try:
        _, ran = _run(_agent(plugin, request))
    finally:
        plugin.close()
    assert ran == []


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("native_name", FUZZY_ONLY)
def test_fuzzy_only_match_fails_closed_before_any_tool_delta(tmp_path, native_name, streaming):
    fixture, plugin = _native(tmp_path, native_name)
    names = []
    try:
        with pytest.raises(directsdk.ClaudeToolOutsideInventory) as raised:
            result = plugin.chat.completions.create(**fixture.request(), stream=streaming)
            for chunk in (result if streaming else ()):
                names += [tc.function.name for tc in (chunk.choices[0].delta.tool_calls or []) if chunk.choices]
    finally:
        plugin.close()
    assert names == []
    assert native_name[:80] in str(raised.value) and "'value'" not in str(raised.value)
    assert "not one of the offered tools" in str(raised.value) and "'probe'" in str(raised.value)


def _refused_after_text():
    events = reply(['{"path": "/tmp/a.txt"}'], "tool_use")  # text "Reading.", then the call
    events[4]["content_block"]["name"] = "mcp__hermes__read_files"
    return events


def test_text_streamed_before_a_refused_call_stays_delivered_and_no_tool_delta_follows(tmp_path):
    # The refusal comes at the call's content_block_start. Text native streamed before it has already reached
    # Hermes; no header or argument of the refused call does.
    chunks = []
    with upstream(tmp_path, _refused_after_text()) as (client, _):
        with pytest.raises(directsdk.ClaudeToolOutsideInventory, match="read_files"):
            for chunk in client.create(**REQUEST, stream=True):
                chunks.append(chunk)
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Reading."
    assert tool_deltas([c for c in chunks if c.choices]) == []


def test_text_streamed_before_a_refused_call_runs_no_tool_in_hermes_streaming_loop(tmp_path):
    # After partial delivery core does not classify the error for fallback: it keeps the streamed text as a cut-short
    # reply and asks the model to continue, a bounded number of times. Whatever it does, no tool runs.
    with upstream(tmp_path, _refused_after_text()) as (client, bodies):
        result, ran = _run(_agent(client, dict(REQUEST), streaming=True))
    assert ran == []
    assert len(bodies) > 1 and not result["completed"]  # re-asked within the turn's budget, never ran the call


def test_the_exact_advertised_name_wins_over_a_slip_reading(tmp_path):
    # With `probe` and `mcp__hermes__probe` both offered, native's `mcp__hermes__probe` is exactly what it was
    # told `probe` is; it is also the prefix-dropped form of `mcp__hermes__mcp__hermes__probe`.
    fixture, plugin = _native(tmp_path, "mcp__hermes__probe")
    request = fixture.request()
    request["tools"].append(_tool("mcp__hermes__probe"))
    try:
        result = plugin.chat.completions.create(**request)
    finally:
        plugin.close()
    assert [c.function.name for c in result.choices[0].message.tool_calls] == ["probe"]


def test_a_slip_two_offered_tools_both_claim_fails_closed(tmp_path):
    # `mcp__probe` is `mcp__probe` with the prefix dropped, or `probe` with the server segment dropped.
    fixture, plugin = _native(tmp_path, "mcp__probe")
    request = fixture.request()
    request["tools"].append(_tool("mcp__probe"))
    try:
        with pytest.raises(directsdk.ClaudeToolOutsideInventory, match="more than one offered tool"):
            plugin.chat.completions.create(**request)
    finally:
        plugin.close()


def test_name_core_cannot_repair_still_reaches_the_host(tmp_path):
    # #39: a foreign tool goes to Hermes, which answers with its recoverable unknown-tool error.
    fixture, plugin = _native(tmp_path, "mcp__fastmail__draft_email")
    try:
        result = plugin.chat.completions.create(**fixture.request())
    finally:
        plugin.close()
    assert result.choices[0].message.tool_calls[0].function.name == "mcp__fastmail__draft_email"


def test_core_routing_helpers_import_from_this_core():
    # The guard asks core's own helpers; a rename there must fail here, not silently turn the guard off.
    from agent.agent_runtime_helpers import repair_tool_call
    from agent.tool_executor import _canonical_tool_name
    from tools.tool_search import is_deferrable_tool_name
    from tools.tool_search_catalog import TOOL_CALL_NAME

    core = directsdk._core_tool_routing()
    assert core is not None
    assert (core.repair, core.canonical, core.deferrable, core.bridge) == (
        repair_tool_call, _canonical_tool_name, is_deferrable_tool_name, TOOL_CALL_NAME)
    assert TOOL_CALL_NAME == "tool_call"


@pytest.mark.parametrize("module", ["agent.agent_runtime_helpers", "tools.tool_search"])
def test_without_core_helpers_an_unknown_name_passes_through(module, monkeypatch):
    # A core that renamed its helpers: behave as before the guard, never fail every unknown name.
    monkeypatch.setitem(sys.modules, module, None)
    assert directsdk._core_tool_routing() is None
    assert directsdk.offered_tool_name("mcp__hermes__prob", {"probe": "probe"}) == ("mcp__hermes__prob", "prob")
    assert directsdk.offered_tool_name("read_terminal", {"terminal": "terminal"}) == ("read_terminal", "read_terminal")


def test_outside_inventory_is_not_retried_but_may_fall_back(profile):
    # The model's own output for this request, not a transport failure: not retried; another provider may answer.
    from agent.error_classifier import FailoverReason, classify_api_error

    verdict = classify_api_error(directsdk.ClaudeToolOutsideInventory("x"), provider=profile.name, model="sonnet")
    assert (verdict.reason, verdict.retryable, verdict.should_fallback) == (FailoverReason.format_error, False, True)
