"""Preset slash behavior with isolated fake persistence and core facade."""
import pytest
from advanced_delegation_plugin import slash


@pytest.fixture
def state(monkeypatch):
    section = {"backend": "default"}
    presets = {"work": {"options": [{"name": "fast", "description": "Fast", "provider": "test", "model": "m"}], "default": "fast"}}
    writes = []
    monkeypatch.setattr(slash.store, "delegation_section", lambda: section)
    monkeypatch.setattr(slash.store, "list_presets", lambda s: list(presets))
    def get(s, name):
        if name not in presets:
            raise ValueError(f"Unknown preset {name!r}")
        return presets[name]
    monkeypatch.setattr(slash.store, "get_preset", get)
    monkeypatch.setattr(slash.store, "active_preset", lambda s: "")
    monkeypatch.setattr(slash.store, "use_preset", lambda name: writes.append(name))
    return writes


class API:
    def __init__(self, available=True, fail=False):
        self.available = available
        self.fail = fail
        self.calls = []
    def list_backends(self):
        return [{"name": "model_map", "available": self.available, "reason": "unavailable"}]
    def set_global_backend(self, name, *, session_key="", apply_now=False):
        self.calls.append((name, session_key, apply_now))
        if self.fail:
            raise RuntimeError("rebuild failed")


def test_plain_preset_saves_without_rebuild(state):
    api = API()
    text = slash.handle("preset work", api=api, session_key="s")
    assert state == ["work"] and api.calls == []
    assert "next session" in text


def test_now_rebuilds_even_when_backend_same(state):
    api = API()
    text = slash.handle("preset work --now", api=api, session_key="s")
    assert state == ["work"] and api.calls == [("model_map", "s", True)]
    assert "applied" in text


@pytest.mark.parametrize("args", ["preset missing", "preset work --bogus", "preset work extra", "preset work --now --now", "preset work --now"])
def test_invalid_input_does_not_write(state, args):
    api = API()
    text = slash.handle(args, api=api, session_key="")
    assert state == [] and api.calls == []
    assert "not" in text.lower() or "usage" in text.lower() or "unknown" in text.lower()


def test_unavailable_backend_does_not_write(state):
    text = slash.handle("preset work", api=API(False), session_key="s")
    assert state == [] and "unavailable" in text


def test_picker_and_listing(state):
    text = slash.handle("preset")
    assert "work" in text and state == []
    seen = []
    def ask(q, choices):
        seen.append(choices)
        return "work" if len(seen) == 1 else "now"
    api = API()
    text = slash.handle("preset", api=api, session_key="s", ask_choice=ask)
    assert state == ["work"] and api.calls == [("model_map", "s", True)]
    assert len(seen) == 2 and "applied" in text


def test_post_save_rebuild_failure_reports_saved(state):
    api = API(fail=True)
    text = slash.handle("preset work --now", api=api, session_key="s")
    assert state == ["work"] and "saved" in text.lower() and "rebuild failed" in text


def test_old_core_now_is_honest(state):
    text = slash.handle("preset work --now", session_key="s")
    assert state == ["work"] and "newer Hermes" in text and "next session" in text


def test_old_facade_no_apply_now_does_not_save_twice(state):
    class Old(API):
        def set_global_backend(self, name, *, session_key=""):
            pytest.fail("old API should not be called after rejecting apply_now")
    text = slash.handle("preset work --now", session_key="s", api=Old())
    assert state == ["work"] and "newer Hermes" in text


def test_store_refusal_does_not_request_rebuild(state, monkeypatch):
    def refuse(name):
        raise PermissionError("managed config")
    monkeypatch.setattr(slash.store, "use_preset", refuse)
    api = API()
    text = slash.handle("preset work --now", session_key="s", api=api)
    assert not state and not api.calls and "managed config" in text


def test_picker_cancellation_does_not_write(state):
    answers = iter(["work", None])
    api = API()
    text = slash.handle("preset", session_key="s", api=api,
                        ask_choice=lambda q, c: next(answers))
    assert not state and not api.calls and "unchanged" in text
