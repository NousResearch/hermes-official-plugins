"""`/usage` plan limits through core's own dispatch and renderer; only the HTTP call and the
Claude Code credential read are faked."""
from datetime import datetime, timedelta, timezone

import pytest

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


def _creds(expires_in):
    return {'accessToken': 'login-token', 'refreshToken': 'r',
            'expiresAt': int((datetime.now(timezone.utc) + expires_in).timestamp() * 1000)}


class Response:
    def __init__(self, status, payload):
        self.status_code, self.payload = status, payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def api(profile, monkeypatch):
    """Answers every GET with `api.reply`; `api.requests` records them."""
    import httpx
    import agent.anthropic_credentials as credentials

    class State:
        reply, requests, creds = Response(200, LIMITS), [], _creds(timedelta(hours=1))

    class Client:
        def __init__(self, *, timeout):
            assert timeout < 10  # inside core's hook deadline

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, url, headers):
            State.requests.append((url, headers))
            if isinstance(State.reply, Exception):
                raise State.reply
            return State.reply

    def refuse(*_, **__):
        raise AssertionError('the usage hook must never refresh, write or resolve a login')

    monkeypatch.setattr(httpx, 'Client', Client)
    monkeypatch.setattr(credentials, 'read_claude_code_credentials', lambda: State.creds)
    for name in ('resolve_anthropic_token', '_refresh_oauth_token', '_write_claude_code_credentials'):
        monkeypatch.setattr(credentials, name, refuse, raising=False)
    monkeypatch.delenv('CLAUDE_CODE_OAUTH_TOKEN', raising=False)
    return State


def _usage():
    from agent.account_usage import fetch_account_usage, render_account_usage_lines
    snapshot = fetch_account_usage(PROVIDER)
    return snapshot, render_account_usage_lines(snapshot)


def test_limits_render_one_line_each_in_api_order(api):
    snapshot, lines = _usage()
    assert [(w.label, w.used_percent) for w in snapshot.windows] == [
        ('Current session', 45.0), ('Current week', 23.0), ('Fable week', 17.0), ('something_new', None), ("['odd']", None)]
    assert snapshot.windows[0].reset_at == datetime(2026, 10, 1, 7, 9, tzinfo=timezone.utc)
    assert lines[0] == '\U0001F4C8 Claude plan limits' and lines[1] == f'Provider: {PROVIDER}'
    assert lines[2].startswith('Current session: 55% remaining (45% used)')
    assert snapshot.details == () and snapshot.unavailable_reason is None
    (url, headers), = api.requests
    assert url == 'https://api.anthropic.com/api/oauth/usage' and headers['Authorization'] == 'Bearer login-token'


def test_a_body_without_limits_reads_the_legacy_windows(api):
    api.reply = Response(200, LEGACY)
    snapshot, _ = _usage()
    assert [(w.label, w.used_percent) for w in snapshot.windows] == [('Current session', 40.0), ('Current week', 12.0)]
    assert snapshot.details == ('Extra usage: 1.50 / 10.00 EUR',)
    assert len(api.requests) == 1


def test_setup_token_wins_over_the_stored_login(api, monkeypatch):
    # Native runs on CLAUDE_CODE_OAUTH_TOKEN when it is set (the headless `claude setup-token` login).
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'setup-token')
    api.creds = None
    _usage()
    assert api.requests[0][1]['Authorization'] == 'Bearer setup-token'


@pytest.mark.parametrize('creds, reason', [
    (None, 'no Claude Code login found (run `claude` and log in)'),
    (_creds(-timedelta(minutes=5)), 'token expired (run `claude` once to refresh)'),
    ({'accessToken': 't', 'expiresAt': 'soon'}, 'token expired (run `claude` once to refresh)'),
])
def test_no_usable_login_makes_no_request(api, creds, reason):
    api.creds = creds
    snapshot, lines = _usage()
    assert api.requests == [] and lines[-1] == f'Unavailable: {reason}'


@pytest.mark.parametrize('reply, reason', [
    (Response(401, {}), 'token rejected (run `claude` once to refresh)'),
    (Response(403, {}), 'token rejected (run `claude` once to refresh)'),
    (Response(529, {}), 'usage API returned HTTP 529'),
    (Response(200, ValueError('not json')), 'usage API returned an unreadable response'),
])
def test_api_failures_name_the_reason(api, reply, reason):
    api.reply = reply
    snapshot, lines = _usage()
    assert snapshot.windows == () and lines[-1] == f'Unavailable: {reason}'


def test_network_failure_names_the_reason(api):
    import httpx
    api.reply = httpx.ConnectTimeout('slow')
    _, lines = _usage()
    assert lines[-1] == 'Unavailable: could not reach the usage API'


def _write_creds_file(path, token="file-token", expires_in=timedelta(hours=1)):
    import json
    expires_at = int((datetime.now(timezone.utc) + expires_in).timestamp() * 1000)
    payload = {"claudeAiOauth": {"accessToken": token, "refreshToken": "r", "expiresAt": expires_at}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_usage_works_when_adopt_external_logins_is_false(api, tmp_path, monkeypatch):
    # Simulates auth.adopt_external_logins: false where core's read_claude_code_credentials() returns None (#82).
    api.creds = None
    login_dir = tmp_path / "claude-login"
    _write_creds_file(login_dir / ".credentials.json", token="adopted-login-token")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(login_dir))
    snapshot, lines = _usage()
    assert snapshot.unavailable_reason is None
    assert api.requests[0][1]["Authorization"] == "Bearer adopted-login-token"


def test_usage_honours_claude_config_dir_plugin_setting(api, tmp_path):
    import json
    from hermes_cli.config import get_config_path
    api.creds = None
    setting_dir = tmp_path / "profile-login"
    _write_creds_file(setting_dir / ".credentials.json", token="profile-setting-token")
    get_config_path().write_text(
        "plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n        %s: %s\n"
        % (PROVIDER, "claude_config_dir", json.dumps(str(setting_dir))), encoding="utf-8")
    snapshot, lines = _usage()
    assert snapshot.unavailable_reason is None
    assert api.requests[0][1]["Authorization"] == "Bearer profile-setting-token"


def test_usage_honours_directsdk_config_dir_env_override(api, tmp_path, monkeypatch):
    api.creds = None
    env_dir = tmp_path / "env-override-login"
    _write_creds_file(env_dir / ".credentials.json", token="override-token")
    monkeypatch.setenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", str(env_dir))
    snapshot, lines = _usage()
    assert snapshot.unavailable_reason is None
    assert api.requests[0][1]["Authorization"] == "Bearer override-token"


def test_usage_reads_keychain_when_no_custom_config_dir(api, monkeypatch):
    import agent.anthropic_credentials as credentials
    api.creds = None
    kc_token = "keychain-token"
    kc_payload = _creds(timedelta(hours=2))
    kc_payload["accessToken"] = kc_token
    monkeypatch.setattr(credentials, "_read_claude_code_credentials_from_keychain", lambda: kc_payload, raising=False)
    snapshot, lines = _usage()
    assert snapshot.unavailable_reason is None
    assert api.requests[0][1]["Authorization"] == f"Bearer {kc_token}"


def test_usage_skips_keychain_when_custom_config_dir_configured(api, tmp_path, monkeypatch):
    import agent.anthropic_credentials as credentials
    api.creds = None
    kc_payload = _creds(timedelta(hours=2))
    kc_payload["accessToken"] = "keychain-token"
    monkeypatch.setattr(credentials, "_read_claude_code_credentials_from_keychain", lambda: kc_payload, raising=False)
    empty_custom_dir = tmp_path / "empty-dir"
    empty_custom_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty_custom_dir))
    snapshot, lines = _usage()
    assert snapshot.unavailable_reason == "no Claude Code login found (run `claude` and log in)"
    assert api.requests == []
