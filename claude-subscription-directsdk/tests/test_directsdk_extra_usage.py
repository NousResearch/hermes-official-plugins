"""#43: the extra_usage setting acts on the signal Claude Code already reports (rate_limit_event's
isUsingOverage, get_usage's extra_usage + full plan windows), before the next request is billed."""
import json
import logging
import os
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


def test_stop_refuses_the_next_request_once_native_reports_extra_usage(native):
    from agent.error_classifier import FailoverReason, classify_api_error
    from extra_usage import ClaudeExtraUsageBlocked
    _settings(extra_usage="stop")
    native(SPILLING)  # this request crossed the limit: already billed, so it completes
    with pytest.raises(ClaudeExtraUsageBlocked) as raised:
        native()
    assert native.spawns() == 1  # refused before Claude Code started
    message = str(raised.value)
    assert time.strftime("%Y-%m-%dT%H:%M", time.gmtime(RESETS)) in message
    assert "extra_usage ask" in message and "hermes config set" in message
    verdict = classify_api_error(raised.value, provider=PLUGIN_NAME, model="sonnet")
    assert verdict.reason == FailoverReason.billing and not verdict.retryable and not verdict.should_fallback
    assert verdict.error_context["usage_window"] and verdict.error_context["display_message"] == message


def test_ask_refuses_until_the_user_allows_this_window(native):
    from extra_usage import ClaudeExtraUsageBlocked
    _settings(extra_usage="ask")
    native(SPILLING)
    with pytest.raises(ClaudeExtraUsageBlocked) as raised:
        native()
    assert "extra_usage_allowed_until" in str(raised.value)
    _settings(extra_usage="ask", extra_usage_allowed_until=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(RESETS)))
    assert native().choices[0].message.content == "hello\n"
    assert native.spawns() == 2


def test_notify_is_the_default_and_warns_once_per_window(native, caplog):
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        native(SPILLING)
    assert native.spawns() == 3
    assert [r.getMessage() for r in caplog.records].count(
        next(r.getMessage() for r in caplog.records if "drawing paid extra usage" in r.getMessage())) == 1


def test_a_plan_window_that_resets_admits_again(native, monkeypatch):
    import extra_usage
    _settings(extra_usage="stop")
    native(dict(SPILLING, resetsAt=int(time.time()) + 60))
    monkeypatch.setattr(extra_usage.time, "time", lambda: RESETS + 1)
    probed = []
    import directsdk_setup
    monkeypatch.setattr(directsdk_setup, "fetch_usage", lambda **kw: probed.append(kw) or (None, "fixture"))
    assert native().choices[0].message.content == "hello\n"
    assert len(probed) == 1  # past the reset the process asks Claude Code again


@pytest.mark.parametrize("percent, refused", [(100, True), (99, False)])
def test_a_new_process_asks_claude_code_once_before_refusing(native, monkeypatch, percent, refused):
    """No state yet (fresh process) under stop: one zero-inference get_usage decides."""
    import directsdk_setup
    from extra_usage import ClaudeExtraUsageBlocked
    _settings(extra_usage="stop")
    body = {"extra_usage": {"is_enabled": True}, "limits": [
        {"kind": "session", "percent": percent, "resets_at": "2030-01-01T00:00:00Z"},
        {"kind": "weekly_all", "percent": 20}]}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        answer = {"type": "control_response", "response": {"subtype": "success", "request_id": "hermes-usage",
                                                           "response": {"rate_limits": body}}}
        return SimpleNamespace(stdout=json.dumps(answer) + "\n", returncode=0)
    monkeypatch.setattr(directsdk_setup.subprocess, "Popen", popen_from_run(fake_run, turns=directsdk_setup.subprocess.Popen))
    if refused:
        with pytest.raises(ClaudeExtraUsageBlocked, match="2030-01-01T00:00:00Z"):
            native()
    else:
        native()
        native()
    assert len(calls) == 1 and native.spawns() == (0 if refused else 2)


def test_an_unknown_mode_refuses_rather_than_spends(native):
    from extra_usage import ClaudeExtraUsageBlocked
    _settings(extra_usage="stpo")
    native(SPILLING)
    with pytest.raises(ClaudeExtraUsageBlocked):
        native()


def test_the_third_party_extra_usage_400_is_a_billing_stop_not_a_silent_fallback(profile):
    """P#296: classify_api_error used to decline this 400, so core walked the fallback chain silently."""
    from agent.error_classifier import FailoverReason, classify_api_error
    from directsdk import ClaudeAPIError
    error = ClaudeAPIError("Incomplete upstream response (first upstream attempt: status 400, upstream said: Third-party "
                           "apps now draw from your extra usage, not your plan limits. Ask your workspace admin.)", 400)
    verdict = classify_api_error(error, provider=PLUGIN_NAME, model="sonnet")
    assert verdict.reason == FailoverReason.billing and not verdict.should_fallback
    assert "hermes-agent" in verdict.error_context["display_message"]
    assert "Ask your workspace admin" in verdict.error_context["display_message"]
