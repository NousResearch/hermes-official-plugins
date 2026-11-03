import argparse
import json

import pytest

from hermes_security.errors import NotFound


class FakeService:
    def __init__(self):
        self.calls = []
        self.result = {"scanId": "s"}
        self.error = None

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            if self.error:
                raise self.error
            return self.result
        return call


def slash(service, text):
    from hermes_security.commands import make_command
    return json.loads(make_command(lambda: service)(text))


def test_slash_status_help_and_revoke():
    s = FakeService()
    assert slash(s, "status")["ok"]
    assert [c[0] for c in s.calls] == ["summary", "list_scans"]
    assert slash(s, "help")["ok"]
    assert slash(s, "revoke g")["ok"]
    assert s.calls[-1] == ("revoke_grant", ("g",), {})


def test_slash_grant_defaults_and_bounds():
    s = FakeService()
    assert slash(s, "authorize-validation s https://example.test")["ok"]
    assert s.calls[-1] == ("mint_grant", ("s",), {
        "origins": ["https://example.test"], "actions": ["http-probe"],
        "expires_in_s": 3600, "max_requests": 200, "created_by": "user-command"})
    assert slash(s, "authorize-validation s https://example.test --actions http-probe --minutes 240 --max-requests 200")["ok"]
    assert s.calls[-1][2]["expires_in_s"] == 14400


@pytest.mark.parametrize("text", ["wat", "status extra", "revoke", "allow-local", "'", "authorize-validation s",
    "authorize-validation s https://example.test --minutes 241", "authorize-validation s https://example.test --minutes 0",
    "authorize-validation s https://example.test --max-requests 201", "authorize-validation s https://example.test --max-requests -1",
    "authorize-validation s https://example.test --actions shell", "authorize-validation s https://example.test --actions http-probe,",
    "authorize-validation s https://example.test --actions local-command",
    "authorize-validation s https://example.test --actions http-probe,local-command",
    "authorize-validation s https://example.test/path", "authorize-validation s https://user:pw@example.test"])
def test_slash_rejects_bad_arguments(text):
    s = FakeService()
    result = slash(s, text)
    assert not result["ok"] and result["error"]["code"] == "invalid_input"
    assert not s.calls


def test_cli_submit_reads_file_and_checks_scan(tmp_path, monkeypatch, capsys):
    from hermes_security import cli
    s = FakeService()
    monkeypatch.setattr(cli, "get_service", lambda: s)
    file = tmp_path / "worker.json"
    file.write_text(json.dumps({"scanId": "s", "attemptId": "att_1"}))
    assert cli.main(["submit", "--scan", "s", "--file", str(file)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert s.calls[-1] == ("submit_worker_result", ({"scanId": "s", "attemptId": "att_1"},), {})
    assert cli.main(["submit", "--scan", "different", "--file", str(file)]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"
    assert len(s.calls) == 1


@pytest.mark.parametrize("body", ["not json", "[]", '{"scanId":"s","x":NaN}', '{"scanId":"s","scanId":"t"}'])
def test_cli_invalid_json(tmp_path, monkeypatch, capsys, body):
    from hermes_security import cli
    s = FakeService()
    monkeypatch.setattr(cli, "get_service", lambda: s)
    file = tmp_path / "bad.json"
    file.write_text(body)
    assert cli.main(["submit", "--scan", "s", "--file", str(file)]) == 3
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert not s.calls


def test_cli_exit_codes(monkeypatch, capsys):
    from hermes_security import cli
    s = FakeService()
    monkeypatch.setattr(cli, "get_service", lambda: s)
    assert cli.main(["get"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"
    s.error = NotFound("Missing scan")
    assert cli.main(["get", "--scan", "s"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
    s.error = RuntimeError("secret text")
    assert cli.main(["get", "--scan", "s"]) == 1
    assert "secret text" not in capsys.readouterr().out
    assert cli.main(["--help"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]


def test_cli_validation_consent_only_explicit(tmp_path, monkeypatch, capsys):
    from hermes_security import cli
    s = FakeService()
    monkeypatch.setattr(cli, "get_service", lambda: s)
    file = tmp_path / "plans.json"
    file.write_text('{"plans": []}')
    base = ["validate", "--scan", "s", "--file", str(file)]
    assert cli.main(base) == 0
    assert s.calls[-1][2]["user_authorized"] is False
    assert cli.main(base + ["--allow-local"]) == 0
    assert s.calls[-1][2]["user_authorized"] is True
    file.write_text('{"receipts": []}')
    assert cli.main(base + ["--allow-local"]) == 3
    assert len(s.calls) == 2
    assert cli.main(["start", "--path", "/repo", "--safety-level", "local-safe"]) == 0
    assert "allowLocalValidation" not in s.calls[-1][2]
    assert cli.main(["start", "--path", "/repo", "--safety-level", "local-safe", "--allow-local"]) == 0
    assert s.calls[-1][2]["allowLocalValidation"] is True


@pytest.mark.parametrize("argv,method", [
    (["start", "--path", "/repo"], "start_scan"),
    (["get", "--scan", "s"], "get_scan"),
    (["chains", "--scan", "s"], "propose_chains"),
    (["finalize", "--scan", "s"], "finalize"),
    (["cancel", "--scan", "s"], "cancel"),
    (["resume", "--scan", "s"], "resume"),
    (["export", "--scan", "s", "--format", "json"], "export"),
    (["list"], "list_scans"),
    (["authorize-validation", "s", "https://example.test"], "mint_grant"),
    (["revoke", "g"], "revoke_grant"),
])
def test_cli_dispatch(argv, method, monkeypatch, capsys):
    from hermes_security import cli
    s = FakeService()
    monkeypatch.setattr(cli, "get_service", lambda: s)
    assert cli.main(argv) == 0
    assert s.calls[-1][0] == method
    assert json.loads(capsys.readouterr().out)["ok"]


def test_cli_export_out(tmp_path, monkeypatch, capsys):
    from hermes_security import cli
    s = FakeService()
    s.result = (b"report", "text/markdown", "report.md")
    monkeypatch.setattr(cli, "get_service", lambda: s)
    path = tmp_path / "out.md"
    assert cli.main(["export", "--scan", "s", "--format", "md", "--out", str(path)]) == 0
    assert path.read_bytes() == b"report"
    assert json.loads(capsys.readouterr().out)["ok"]


def test_cli_checkpoint_and_explanations(tmp_path, monkeypatch, capsys):
    from hermes_security import cli
    s = FakeService()
    monkeypatch.setattr(cli, "get_service", lambda: s)
    file = tmp_path / "input.json"
    file.write_text('{"coverage": [], "note": "review pending"}')
    assert cli.main(["checkpoint", "--scan", "s", "--file", str(file)]) == 0
    assert s.calls[-1] == ("checkpoint", ("s",), {"coverage": [], "note": "review pending"})
    file.write_text('{"chain_1": {"title": "Supported path"}}')
    assert cli.main(["chains", "--scan", "s", "--explanations", str(file)]) == 0
    assert s.calls[-1] == ("record_chains", ("s",), {"explanations": {"chain_1": {"title": "Supported path"}}})


def test_cli_file_limits_and_missing_files(tmp_path, monkeypatch, capsys):
    from hermes_security import cli
    s = FakeService()
    monkeypatch.setattr(cli, "get_service", lambda: s)
    file = tmp_path / "missing.json"
    assert cli.main(["submit", "--scan", "s", "--file", str(file)]) == 3
    file.write_bytes(b"x" * (cli.MAX_INPUT_BYTES + 1))
    assert cli.main(["submit", "--scan", "s", "--file", str(file)]) == 3
    file.write_bytes(b"\xff")
    assert cli.main(["submit", "--scan", "s", "--file", str(file)]) == 3
    assert not s.calls


def test_cli_data_dir_is_lazy(tmp_path, monkeypatch, capsys):
    import sys
    from types import SimpleNamespace
    from hermes_security import cli
    monkeypatch.setitem(sys.modules, "hermes_security.config", SimpleNamespace(resolve_data_dir=lambda: tmp_path))
    monkeypatch.setattr(cli, "get_service", lambda: (_ for _ in ()).throw(AssertionError("eager service")))
    assert cli.main(["data-dir"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["data_dir"] == str(tmp_path)


def test_slash_expected_and_unexpected_errors():
    s = FakeService()
    s.error = NotFound("Missing scan")
    assert slash(s, "revoke g")["error"]["code"] == "not_found"
    s.error = RuntimeError("private detail")
    assert slash(s, "status")["error"] == {"code": "internal_error", "message": "Security operation failed"}


def test_registered_cli_preserves_exit_code(tmp_path, monkeypatch, capsys):
    from hermes_security import plugin
    from test_registration import FakeContext
    s = FakeService()
    s.error = NotFound("Missing")
    monkeypatch.setattr(plugin, "get_service", lambda ctx=None: s)
    ctx = FakeContext(tmp_path)
    plugin.register(ctx)
    setup, handler = ctx.cli["security-review"]
    parser = argparse.ArgumentParser()
    setup(parser)
    args = parser.parse_args(["get", "--scan", "s"])
    assert handler(args) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
