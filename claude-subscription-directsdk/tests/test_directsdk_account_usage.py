"""`/usage` plan limits through core's own dispatch and renderer. Claude Code answers its `get_usage`
control request; only that subprocess is faked. The plugin never reads or sends the CLI's token."""
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import popen_from_run

PROVIDER = 'claude-subscription-directsdk-experimental'
LIMITS = {'limits': [
    {'kind': 'session', 'percent': 45, 'resets_at': '2026-10-01T07:09:00Z'},
    {'kind': 'weekly_all', 'percent': 23, 'resets_at': '2026-10-02T06:59:00Z'},
    {'kind': 'weekly_model', 'percent': 17, 'resets_at': '2026-10-02T06:59:00Z', 'scope': {'model': {'display_name': 'Fable'}}},
    {'kind': 'something_new', 'percent': None, 'resets_at': None},
    {'kind': ['odd'], 'percent': float('nan'), 'scope': 'odd'},
]}
LEGACY = {'five_hour': {'utilization': 0.4, 'resets_at': '2026-10-01T07:09:00Z'},
          'seven_day': {'utilization': 12}, 'seven_day_opus': {'utilization': True}, 'seven_day_sonnet': 'odd',
          'extra_usage': {'is_enabled': True, 'used_credits': 1.5, 'monthly_limit': 10, 'currency': 'EUR'}}


def _answer(rate_limits=None, available=True, subtype='success'):
    return {'type': 'control_response', 'response': {'subtype': subtype, 'request_id': 'hermes-usage', 'response': {
        'subscription_type': 'max' if available else None, 'rate_limits_available': available,
        'rate_limits': rate_limits, 'session': {}, 'behaviors': None}}}


@pytest.fixture
def cli(profile, monkeypatch):
    """A fake Claude Code: `cli.reply` is its control answer; `cli.calls` records argv/env/stdin."""
    import agent.anthropic_credentials as credentials

    class State:
        reply, calls = _answer(LIMITS), []

    def run(argv, **kwargs):
        State.calls.append(SimpleNamespace(argv=argv, env=kwargs['env'], input=kwargs.get('input')))
        if isinstance(State.reply, Exception):
            raise State.reply
        out = State.reply if isinstance(State.reply, str) else json.dumps(State.reply, allow_nan=True)
        return SimpleNamespace(stdout='{"type":"system","subtype":"init"}\n' + out + '\n', returncode=0)

    def refuse(*_, **__):
        raise AssertionError('the usage hook must never read, refresh or send the Claude Code login')

    monkeypatch.setattr(subprocess, 'run', run)
    # The installed copy Hermes loads has its own module objects, so the fake is installed process-wide.
    monkeypatch.setattr(subprocess, 'Popen', popen_from_run(run))
    for name in ('read_claude_code_credentials', 'resolve_anthropic_token', '_refresh_oauth_token',
                 '_write_claude_code_credentials', '_read_claude_code_credentials_from_keychain'):
        monkeypatch.setattr(credentials, name, refuse, raising=False)
    monkeypatch.setenv('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND', sys.executable)
    return State


def _usage():
    from agent.account_usage import fetch_account_usage, render_account_usage_lines
    snapshot = fetch_account_usage(PROVIDER)
    return snapshot, render_account_usage_lines(snapshot)


def test_limits_come_from_claude_code_get_usage(cli):
    snapshot, lines = _usage()
    assert [(w.label, w.used_percent) for w in snapshot.windows] == [
        ('Current session', 45.0), ('Current week', 23.0), ('Fable week', 17.0), ('something_new', None), ("['odd']", None)]
    assert snapshot.windows[0].reset_at == datetime(2026, 10, 1, 7, 9, tzinfo=timezone.utc)
    assert lines[0] == '\U0001F4C8 Claude plan limits' and lines[1] == f'Provider: {PROVIDER}'
    assert lines[2].startswith('Current session: 55% remaining (45% used)')
    assert snapshot.unavailable_reason is None
    call, = cli.calls
    assert json.loads(call.input)['request'] == {'subtype': 'get_usage', 'skip_behaviors': True}
    # Behind the unused admission gate: no inference can leave.
    assert call.env['ANTHROPIC_BASE_URL'].startswith('http://127.0.0.1:')


def test_a_cli_that_never_answers_still_fits_cores_hook_deadline(cli, monkeypatch):
    """Core abandons the /usage hook at 10 s; the probe gives up before that even while stdout stays open."""
    hung = threading.Event()

    class Hanging:
        # The Windows shape: the launcher already exited (so the kill skips it) while a child holds stdout open.
        pid, returncode = os.getpid(), 0

        def __init__(self, argv, **kwargs):
            self.stdin = SimpleNamespace(write=lambda _: None, close=lambda: None)
            self.stdout = SimpleNamespace(read=lambda: hung.wait(30) and '')

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(subprocess, 'Popen', Hanging)
    started = time.monotonic()
    try:
        snapshot, _ = _usage()
    finally:
        hung.set()
    assert time.monotonic() - started < 10
    assert snapshot.unavailable_reason == 'Claude Code did not answer in time'


def test_a_body_without_limits_reads_the_legacy_windows(cli):
    cli.reply = _answer(LEGACY)
    snapshot, _ = _usage()
    assert [(w.label, w.used_percent) for w in snapshot.windows] == [('Current session', 40.0), ('Current week', 12.0)]
    assert snapshot.details == ('Extra usage: 1.50 / 10.00 EUR',)


# The detail get_usage carries beside `limits` on Claude Code 2.1.319 (Max, trimmed from a real answer).
REAL_2_1_319 = {
    'limits': [{'kind': 'session', 'percent': 1, 'resets_at': '2026-11-09T09:00:00+00:00', 'scope': None},
               {'kind': 'weekly_all', 'percent': 35, 'resets_at': '2026-11-10T06:00:00+00:00', 'scope': None}],
    'extra_usage': {'is_enabled': True, 'monthly_limit': 50, 'used_credits': 12.5, 'currency': 'USD'},
    'spend': {'used': {'amount_minor': 1250, 'currency': 'USD', 'exponent': 2},
              'limit': {'amount_minor': 5000, 'currency': 'USD', 'exponent': 2}},
    'seven_day_breakdown': {'rows': [{'key': 'claude_code', 'display_name': 'Claude Code', 'percent': 99},
                                     {'key': 'cowork', 'display_name': 'Cowork', 'percent': 1}]},
    'model_scoped': [{'display_name': 'Fable', 'utilization': 7, 'resets_at': '2026-11-10T06:00:00+00:00'}],
}


def test_usage_detail_is_shown_beside_the_limits(cli):
    """#331: `limits` is present on current Claude Code, and the extra-usage, spend, per-surface and per-model
    detail beside it used to be dropped."""
    cli.reply = _answer(REAL_2_1_319)
    snapshot, lines = _usage()
    assert [(w.label, w.used_percent) for w in snapshot.windows] == [
        ('Current session', 1.0), ('Current week', 35.0), ('Fable week', 7.0)]
    assert snapshot.details == ('Extra usage: 12.50 / 50.00 USD', 'Usage credits spent: 12.50 USD of 50.00 USD',
                                'This week by surface: Claude Code 99%, Cowork 1%')
    assert lines[-3:] == list(snapshot.details)


def test_extra_usage_off_is_said_and_a_listed_model_limit_is_not_doubled(cli):
    body = {**REAL_2_1_319, 'extra_usage': {'is_enabled': False, 'disabled_reason': 'org_level_disabled'},
            'limits': REAL_2_1_319['limits'] + [{'kind': 'weekly_scoped', 'percent': 7, 'scope': {'model': {'display_name': 'Fable'}}}]}
    cli.reply = _answer(body)
    snapshot, _ = _usage()
    assert [w.label for w in snapshot.windows].count('Fable week') == 1
    assert snapshot.details[0] == 'Extra usage: off (org_level_disabled)'


def test_works_with_adopt_external_logins_off_and_follows_the_profile_login(cli, monkeypatch):
    # #82: with auth.adopt_external_logins false core will not read the CLI login; the CLI itself still can.
    import directsdk_setup
    monkeypatch.setattr(directsdk_setup, '_setting', lambda key, default=None: '/logins/work' if key == 'claude_config_dir' else default)
    cli.reply = _answer(LEGACY)
    limits, reason = directsdk_setup.fetch_usage()
    assert reason is None and limits == LEGACY
    assert cli.calls[-1].env['CLAUDE_CONFIG_DIR'] == '/logins/work'


@pytest.mark.parametrize('reply, reason', [
    (_answer(None, available=False), 'no Claude subscription login found (run `claude` and log in)'),
    (_answer(None), 'Claude Code returned no plan limits'),
    (_answer(None, subtype='error'), 'Claude Code refused the usage request (needs a version with get_usage)'),
    ('not json', 'Claude Code gave no usage answer (needs a version with get_usage)'),
    (subprocess.TimeoutExpired('claude', 8), 'Claude Code did not answer in time'),
    (OSError('gone'), 'Claude Code gave no usage answer (needs a version with get_usage)'),
])
def test_failures_name_the_reason(cli, reply, reason):
    cli.reply = reply
    snapshot, lines = _usage()
    assert snapshot.windows == () and lines[-1] == f'Unavailable: {reason}'


def test_quiet_mode_still_fetches_limits(cli, monkeypatch):
    # The default (telemetry off) must not blind /usage: the CLI skips get_usage under the
    # nonessential-traffic flag, so the probe drops that one flag and keeps the rest of the quiet set.
    import directsdk_setup
    monkeypatch.delenv('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', raising=False)
    cli.reply = _answer(LEGACY)
    limits, reason = directsdk_setup.fetch_usage()
    assert reason is None and limits == LEGACY
    env = cli.calls[-1].env
    assert 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC' not in env
    assert env['DISABLE_TELEMETRY'] == '1' and env['DISABLE_ERROR_REPORTING'] == '1'
    assert env['ANTHROPIC_BASE_URL'].startswith('http://127.0.0.1:')


def test_a_user_exported_quiet_flag_is_kept_and_named(cli, monkeypatch):
    import directsdk_setup
    monkeypatch.setenv('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', '1')
    cli.reply = _answer(None)
    limits, reason = directsdk_setup.fetch_usage()
    assert cli.calls[-1].env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] == '1'
    assert limits is None and 'nonessential traffic off' in reason


def test_missing_claude_code_makes_no_call(cli, monkeypatch, tmp_path):
    monkeypatch.setenv('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND', str(tmp_path / 'no-such-claude'))
    snapshot, lines = _usage()
    assert cli.calls == [] and lines[-1].startswith('Unavailable: Claude Code is not installed')


@pytest.mark.parametrize('details', [{'seven_day_breakdown': {'rows': 7}}, {'model_scoped': True},
                                     {'seven_day_breakdown': {'rows': 'abc'}, 'model_scoped': 3}])
def test_a_malformed_detail_container_keeps_the_plan_limits(cli, details):
    """Greptile 4511161876: a truthy scalar where a list of rows belongs raised and lost the whole snapshot."""
    import extra_usage
    cli.reply = _answer({**LIMITS, **details})
    snapshot, lines = _usage()
    assert snapshot.unavailable_reason is None
    assert [w.label for w in snapshot.windows][:2] == ['Current session', 'Current week']
    # The extra-usage policy reads the same body and must not raise on it either.
    extra_usage.observe_usage('malformed', {'limits': 5, 'extra_usage': {'is_enabled': True}})
    extra_usage.reset()
