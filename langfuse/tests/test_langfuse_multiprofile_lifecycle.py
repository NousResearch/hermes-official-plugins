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
