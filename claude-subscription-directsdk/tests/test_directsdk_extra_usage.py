"""#43: the extra_usage setting acts on the signal Claude Code already reports (rate_limit_event's
isUsingOverage, get_usage's extra_usage + full plan windows), before the next request is billed."""
import json
import logging
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import PLUGIN_NAME, popen_from_run
from test_directsdk import FAKE

REQUEST = dict(model="sonnet", messages=[{"role": "user", "content": "go"}],
               tools=[{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                                        "parameters": {"type": "object", "properties": {}}}}])
RESETS = int(time.time()) + 3 * 3600
SPILLING = {"status": "allowed_warning", "rateLimitType": "five_hour", "isUsingOverage": True,
            "overageStatus": "allowed", "resetsAt": RESETS}


def _settings(**values):
    from hermes_cli.config import get_config_path
    lines = "".join(f"        {k}: '{v}'\n" for k, v in values.items())
    get_config_path().write_text(f"plugins:\n  enabled: []\n  entries:\n    {PLUGIN_NAME}:\n      settings:\n{lines}",
                                 encoding="utf-8")


@pytest.fixture
def native(profile, tmp_path, monkeypatch):
    """A fake Claude Code that records each spawn and can emit one rate_limit_event."""
    import extra_usage
    extra_usage.reset()
    spawns = tmp_path / "spawns"
    event = ("open(os.environ['SPAWNS'], 'a').write('x')\n"
             "if os.environ.get('RATE_EVENT'):\n"
             " print(json.dumps({'type':'rate_limit_event','rate_limit_info':json.loads(os.environ['RATE_EVENT']),"
             "'uuid':'u','session_id':'s'}),flush=True)\n")
    script = tmp_path / "native.py"
    script.write_text(FAKE.replace("for b in blocks:", event + "for b in blocks:", 1), encoding="utf-8")
    for key in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SPAWNS", str(spawns))
    monkeypatch.setenv("HOME", str(tmp_path))
    # A cold stop/ask check asks get_usage first: by default it answers "within the plan limits".
    _usage_probe(monkeypatch, json.dumps({"type": "control_response", "response": {
        "subtype": "success", "request_id": "hermes-usage", "response": {"rate_limits": {"limits": []}}}}) + "\n")

    def run(rate_event=None):
        import directsdk
        if rate_event is None:
            monkeypatch.delenv("RATE_EVENT", raising=False)
        else:
            monkeypatch.setenv("RATE_EVENT", json.dumps(rate_event))
        client = directsdk.Client(command=[sys.executable, str(script)])
        try:
            return client.create(**REQUEST)
        finally:
            client.close()
    run.spawns = lambda: len(spawns.read_text()) if spawns.exists() else 0
    yield run
    extra_usage.reset()


@pytest.mark.parametrize("mode", ["notify", "stop", "ask", "stpo"])
def test_a_spilling_window_follows_the_extra_usage_setting(native, mode, caplog):
    """notify (the default) warns once per window; stop and ask refuse the next request before it is billed; an
    unknown mode refuses rather than spends."""
    from agent.error_classifier import FailoverReason, classify_api_error
    from extra_usage import ClaudeExtraUsageBlocked
    if mode != "notify":
        _settings(extra_usage=mode)
    caplog.set_level(logging.WARNING)
    native(SPILLING)  # this request crossed the limit: already billed, so it completes
    if mode == "notify":
        native(SPILLING)
        native()
        assert native.spawns() == 3
        assert sum("drawing paid extra usage" in r.getMessage() for r in caplog.records) == 1
        return
    with pytest.raises(ClaudeExtraUsageBlocked) as raised:
        native()
    assert native.spawns() == 1  # refused before Claude Code started
    message = str(raised.value)
    assert time.strftime("%Y-%m-%dT%H:%M", time.gmtime(RESETS)) in message
    verdict = classify_api_error(raised.value, provider=PLUGIN_NAME, model="sonnet")
    assert verdict.reason == FailoverReason.billing and not verdict.retryable and not verdict.should_fallback
    assert verdict.error_context["display_message"] == message
    if mode == "ask":
        assert "extra_usage_allowed_until" in message
        _settings(extra_usage="ask", extra_usage_allowed_until=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(RESETS)))
        assert native().choices[0].message.content == "hello\n"
        assert native.spawns() == 2


def _answer(body):
    return json.dumps({"type": "control_response", "response": {
        "subtype": "success", "request_id": "hermes-usage", "response": {"rate_limits": body}}}) + "\n"


@pytest.mark.parametrize("answer, refused", [
    (_answer({"extra_usage": {"is_enabled": True}, "limits": [{"kind": "session", "percent": 100,
                                                                "resets_at": "2030-01-01T00:00:00Z"}]}), "2030-01-01T00:00:00Z"),
    # Legacy `utilization` is a 0..1 fraction (as /usage reads it), so 1 is a full window.
    (_answer({"extra_usage": {"is_enabled": True}, "five_hour": {"utilization": 1, "resets_at": "2030-01-01T00:00:00Z"}}),
     "2030-01-01T00:00:00Z"),
    ("", "needs a version with get_usage"),  # an answer that cannot tell refuses rather than spends
    (_answer({"extra_usage": {"is_enabled": True}, "limits": [{"kind": "session", "percent": 99}]}), None),
], ids=["full", "legacy-full", "unknown", "within"])
def test_a_fresh_process_under_stop_asks_claude_code_before_the_first_turn(native, monkeypatch, answer, refused):
    from extra_usage import ClaudeExtraUsageBlocked
    _settings(extra_usage="stop")
    probes = _usage_probe(monkeypatch, answer)
    if refused:
        with pytest.raises(ClaudeExtraUsageBlocked, match=refused):
            native()
    else:
        native()
        native()
    assert native.spawns() == (0 if refused else 2) and len(probes) == 1


def _usage_probe(monkeypatch, answer):
    """Fake only the get_usage probe child; the turn spawns the real fake native. Returns the probe envs."""
    import directsdk_setup
    envs = []

    def fake_run(argv, **kwargs):
        envs.append(kwargs["env"])
        return SimpleNamespace(stdout=answer, returncode=0)
    monkeypatch.setattr(directsdk_setup.subprocess, "Popen", popen_from_run(fake_run, turns=directsdk_setup.subprocess.Popen))
    return envs
