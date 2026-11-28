"""Trace ownership and client lifecycle under context-local profile overrides."""

import contextlib
import importlib
import sys

import pytest
from hermes_constants import (
    hermes_home_key,
    reset_hermes_home_override,
    set_hermes_home_override,
)


class Span:
    def __init__(self):
        self.ended = False

    def start_observation(self, **kwargs):
        return Span()

    def set_trace_io(self, **kwargs):
        pass

    def update(self, **kwargs):
        pass

    def end(self):
        self.ended = True


class Client:
    def __init__(self):
        self.roots = []
        self.shutdown_count = 0

    def create_trace_id(self, *, seed):
        return seed

    def start_observation(self, **kwargs):
        root = Span()
        self.roots.append(root)
        return root

    def flush(self):
        pass

    def shutdown(self):
        self.shutdown_count += 1


@contextlib.contextmanager
def profile(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    sys.modules.pop("langfuse_plugin", None)
    module = importlib.import_module("langfuse_plugin")
    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    clients = {}
    for home in homes:
        home.mkdir()
        with profile(home):
            clients[hermes_home_key()] = Client()
    monkeypatch.setattr(module, "_build_client", lambda: clients[hermes_home_key()])
    monkeypatch.setattr(module.atexit, "register", lambda callback: None)
    monkeypatch.setattr(module, "propagate_attributes", None)
    return module, homes, clients


def open_turn(module, *, session_id="same-session", turn_id="same-turn", task_id=""):
    module.on_pre_llm_request(
        task_id=task_id, session_id=session_id, turn_id=turn_id,
        api_request_id="request", provider="provider", model="model", messages=[],
    )
    return module._TRACE_STATE[module._trace_key(task_id, session_id, turn_id=turn_id)]


@pytest.mark.parametrize("scope", ["session", "task", "thread"])
def test_same_ids_open_distinct_profile_traces(plugin, scope):
    module, homes, clients = plugin
    states = []
    for home in homes:
        with profile(home):
            states.append(open_turn(
                module, session_id="" if scope == "thread" else "same-session",
                task_id="same-task" if scope == "task" else "",
            ))
    assert states[0] is not states[1]
    assert [len(client.roots) for client in clients.values()] == [1, 1]
    assert len(module._TRACE_STATE) == 2


def test_turn_lookup_stays_in_current_profile(plugin):
    module, homes, _ = plugin
    with profile(homes[0]):
        first = open_turn(module, session_id="session-a")
    with profile(homes[1]):
        # A suffix-only lookup must not return the other profile, even before we open ours.
        assert module._state_for_turn("same-turn") is None
        second = open_turn(module, session_id="session-b")
        assert module._state_for_turn("same-turn") is second
    with profile(homes[0]):
        assert module._state_for_turn("same-turn") is first


@pytest.mark.parametrize("session_id", ["", "same-session"])
def test_finalize_only_closes_current_profile(plugin, session_id):
    module, homes, _ = plugin
    with profile(homes[0]):
        first = open_turn(module, turn_id="turn-a")
    with profile(homes[1]):
        second = open_turn(module, turn_id="turn-b")
    with profile(homes[0]):
        module.on_session_finalize(session_id=session_id)
    assert first.root_span.ended
    assert not second.root_span.ended
    assert list(module._TRACE_STATE.values()) == [second]


def test_shutdown_waits_for_profiles_live_traces(plugin):
    module, homes, _ = plugin
    with profile(homes[0]):
        first = open_turn(module, session_id="session-a")
        second = open_turn(module, session_id="session-b")
        client = module._get_langfuse()
        module.on_session_finalize(session_id="session-a", reason="shutdown")
        assert first.root_span.ended
        assert not second.root_span.ended
        assert client.shutdown_count == 0
        module.on_session_finalize(session_id="session-b", reason="shutdown")
        assert second.root_span.ended
        assert client.shutdown_count == 1


def test_shutdown_is_issued_at_most_once_per_client(plugin):
    module, homes, _ = plugin
    with profile(homes[0]):
        open_turn(module)
        client = module._get_langfuse()
        module.on_session_finalize(reason="shutdown")
        module.on_session_finalize(reason="shutdown")
        assert client.shutdown_count == 1


def test_sessionless_trace_exports_no_profile_namespace(plugin, monkeypatch):
    """The profile namespace on in-process keys is the home path: it must not become
    the Langfuse session id of a turn that has no session of its own."""
    module, homes, _ = plugin
    exported = []

    @contextlib.contextmanager
    def capture(**attributes):
        exported.append(attributes)
        yield

    monkeypatch.setattr(module, "propagate_attributes", capture)
    with profile(homes[0]):
        home_key = hermes_home_key()
        open_turn(module, session_id="")
    assert exported, "the root trace did not propagate its attributes"
    session = exported[0]["session_id"]
    assert not session.startswith("profile:")
    assert home_key not in session


# ── End-user attribution under multiplexing ──────────────────────────────────────────────────────
# The sender maps are keyed per profile: two profiles can reuse a turn or child-session id, and an
# unscoped key would export one profile's end user on another profile's trace.


class _OptInByHome:
    plugin_id = "langfuse"
    profile_name = "custom"

    def __init__(self, opt_in):
        self.opt_in = opt_in

    def current_cron_execution(self):
        return None

    def get_config(self, name, default=False):
        assert name == "end_user_attribution"
        return self.opt_in.get(hermes_home_key(), False)


def _opt_in(module, homes, a_enabled, b_enabled):
    keys = []
    for home in homes:
        with profile(home):
            keys.append(hermes_home_key())
    module._PLUGIN_CTX = _OptInByHome({keys[0]: a_enabled, keys[1]: b_enabled})
    return keys


def _sender_turn(module, *, turn_id="same-turn", sender=None, session_id="same-session"):
    if sender is not None:
        module.on_pre_llm_call(turn_id=turn_id, sender_id=sender)
    module.on_pre_llm_request(session_id=session_id, turn_id=turn_id, api_request_id="request",
                              provider="provider", model="model", messages=[])
    return module._TRACE_STATE[module._trace_key("", session_id, turn_id=turn_id)]


def test_opted_out_profile_never_exports_another_profiles_sender(plugin):
    module, homes, _ = plugin
    _opt_in(module, homes, True, False)
    with profile(homes[0]):
        module.on_pre_llm_call(turn_id="same-turn", sender_id="user-a")
    with profile(homes[1]):
        state = _sender_turn(module, sender="user-b")
    assert state.propagated.get("user_id", "") == ""


def test_same_turn_id_keeps_each_profiles_own_sender(plugin):
    module, homes, _ = plugin
    _opt_in(module, homes, True, True)
    with profile(homes[0]):
        module.on_pre_llm_call(turn_id="same-turn", sender_id="user-a")
    with profile(homes[1]):
        module.on_pre_llm_call(turn_id="same-turn", sender_id="user-b")
    # Both senders are stashed before either root opens (pre_llm_call precedes pre_api_request).
    with profile(homes[0]):
        assert _sender_turn(module).propagated.get("user_id") == "user-a"
    with profile(homes[1]):
        assert _sender_turn(module).propagated.get("user_id") == "user-b"


def test_subagent_user_is_inherited_only_within_its_profile(plugin):
    module, homes, _ = plugin
    _opt_in(module, homes, True, False)
    with profile(homes[0]):
        _sender_turn(module, turn_id="parent-turn", sender="user-a")
        module.on_subagent_start(parent_turn_id="parent-turn", child_session_id="same-child", child_role="helper")
        inherited = dict(module._USER_BY_CHILD_SESSION)
        assert list(inherited.values()) == ["user-a"]
    with profile(homes[1]):
        # Same child-session id in another profile: no inherited user, and its stop leaves A's link alone.
        child = _sender_turn(module, turn_id="child-turn", sender="", session_id="same-child")
        assert child.propagated.get("user_id", "") == ""
        module.on_subagent_stop(child_session_id="same-child")
    assert dict(module._USER_BY_CHILD_SESSION) == inherited


def test_concurrent_profiles_with_the_same_turn_ids_keep_their_senders(plugin):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    module, homes, _ = plugin
    _opt_in(module, homes, True, True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for iteration in range(64):  # below the 256 live-trace cap
            barrier = Barrier(2)
            turn_id = f"same-turn-{iteration}"

            def run(side):
                with profile(homes[side]):
                    module.on_pre_llm_call(turn_id=turn_id, sender_id=f"user-{side}-{iteration}")
                    barrier.wait(timeout=4)
                    module.on_pre_llm_request(session_id="same-session", turn_id=turn_id, api_request_id="request",
                                              provider="provider", model="model", messages=[])
                    return module._TRACE_STATE[module._trace_key("", "same-session", turn_id=turn_id)] \
                        .propagated.get("user_id", "")

            actual = [f.result(timeout=7) for f in [pool.submit(run, side) for side in (0, 1)]]
            assert actual == [f"user-0-{iteration}", f"user-1-{iteration}"], f"iteration {iteration}"
