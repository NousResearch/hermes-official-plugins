"""Headless preset CLI contract; storage is mocked and never touches live config."""
import argparse

import pytest

from advanced_delegation_plugin import cli


def run(*argv):
    parser = argparse.ArgumentParser(prog="hermes delegation")
    cli.setup_parser(parser)
    args = parser.parse_args(argv)
    return args.func(args)


def test_list_marks_active_and_handles_empty(monkeypatch, capsys):
    section = {"sentinel": True}
    monkeypatch.setattr(cli.store, "delegation_section", lambda: section)
    monkeypatch.setattr(cli.store, "list_presets", lambda s: ["work", "home"] if s is section else [], raising=False)
    monkeypatch.setattr(cli.store, "active_preset", lambda s: "work", raising=False)
    assert run("preset", "list") == 0
    out = capsys.readouterr().out
    assert "work" in out and "home" in out and "active" in out.lower()
    monkeypatch.setattr(cli.store, "list_presets", lambda s: [], raising=False)
    assert run("preset", "list") == 0
    assert "no preset" in capsys.readouterr().out.lower()


def test_show_includes_default_and_complete_route_fields(monkeypatch, capsys):
    section = {}
    monkeypatch.setattr(cli.store, "delegation_section", lambda: section)
    def get(s, name):
        assert s is section and name == "work"
        return {"default": "fast", "options": [{"name": "fast", "model": "m1", "fallback_providers": [{"model": "m2"}]}]}
    monkeypatch.setattr(cli.store, "get_preset", get, raising=False)
    assert run("preset", "show", "work") == 0
    out = capsys.readouterr().out
    assert all(s in out for s in ("work", "default", "fast", "m1", "fallback_providers", "m2"))


@pytest.mark.parametrize("argv,method,expected", [
    (("save", "work"), "save_preset", (("work",), {"overwrite": False})),
    (("save", "work", "--force"), "save_preset", (("work",), {"overwrite": True})),
    (("use", "work"), "use_preset", (("work",), {})),
    (("rename", "work", "home"), "rename_preset", (("work", "home"), {})),
    (("delete", "work"), "delete_preset", (("work",), {})),
])
def test_mutations_noninteractive(monkeypatch, capsys, argv, method, expected):
    calls = []
    monkeypatch.setattr(cli.store, method, lambda *a, **kw: calls.append((a, kw)), raising=False)
    monkeypatch.setattr(cli, "_has_terminal", lambda: False)
    assert run("preset", *argv) == 0
    assert calls == [expected]
    out = capsys.readouterr().out.lower()
    assert argv[1] in out
    if argv[0] == "use":
        assert "new session" in out and "model" in out


@pytest.mark.parametrize("command,method", [("save", "save_preset"), ("use", "use_preset"),
                                             ("delete", "delete_preset"), ("rename", "rename_preset")])
@pytest.mark.parametrize("error", [ValueError("invalid or duplicate preset"), RuntimeError("config is managed")])
def test_errors_are_nonzero_and_helpful(monkeypatch, capsys, command, method, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(cli.store, method, fail, raising=False)
    args = ("preset", command, "bad", "new") if command == "rename" else ("preset", command, "bad")
    assert run(*args) != 0
    assert str(error) in capsys.readouterr().err


def test_show_missing_is_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(cli.store, "delegation_section", lambda: {})
    def missing(section, name):
        raise ValueError("preset not found")
    monkeypatch.setattr(cli.store, "get_preset", missing, raising=False)
    assert run("preset", "show", "missing") != 0
    assert "not found" in capsys.readouterr().err


def test_show_masks_credentials_without_mutating_snapshot(monkeypatch, capsys):
    from copy import deepcopy
    snapshot = {"default": "fast", "options": [{"name": "fast", "model": "m1",
        "api_key": "top-secret-fixture-value", "fallback_providers": [
            {"model": "backup", "api_key": "fallback-secret-fixture-value"}],
        "request_overrides": {"headers": {"Authorization": "Bearer nested-fixture-value"}}}]}
    before = deepcopy(snapshot)
    monkeypatch.setattr(cli.store, "delegation_section", lambda: {})
    monkeypatch.setattr(cli.store, "get_preset", lambda *_: snapshot)
    assert run("preset", "show", "work") == 0
    out = capsys.readouterr().out
    for secret in ("top-secret-fixture-value", "fallback-secret-fixture-value", "Bearer nested-fixture-value"):
        assert secret not in out
    assert "backup" in out and "m1" in out
    assert snapshot == before


def test_missing_preset_subcommand_is_an_error(capsys):
    assert run("preset") != 0
    assert "preset" in capsys.readouterr().err.lower()
