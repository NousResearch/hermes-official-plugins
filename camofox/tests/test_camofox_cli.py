"""``hermes camofox status`` reports what a user needs to fix."""

from __future__ import annotations

from camofox_plugin import camofox_cli as cli


def test_status_without_url_points_at_setup(capsys):
    assert cli.cmd_status() == 1
    assert "hermes camofox setup" in capsys.readouterr().out


def test_status_reachable_server(camofox, capsys):
    camofox.health = {"ok": True, "vncPort": 6080}
    assert cli.cmd_status() == 0
    out = capsys.readouterr().out
    assert "reachable" in out and "http://127.0.0.1:6080" in out and "ephemeral" in out


def test_status_unreachable_server(monkeypatch, capsys):
    monkeypatch.setenv("CAMOFOX_URL", "http://127.0.0.1:9")
    assert cli.cmd_status() == 1
    assert "not reachable" in capsys.readouterr().out


def test_setup_saves_the_url_to_the_profile_env(camofox, monkeypatch, _isolated_home):
    import hermes_cli.setup as setup
    answers = iter([camofox.url, ""])
    monkeypatch.setattr(setup, "prompt", lambda *a, **k: next(answers))
    assert cli.cmd_setup(None) == 0
    assert f"CAMOFOX_URL={camofox.url}" in (_isolated_home / ".env").read_text()

