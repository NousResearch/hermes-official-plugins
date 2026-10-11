"""Issue #44: the child env must carry the login identity Claude Code's local check reads,
and native stderr must reach the raised error.

Without ``USER`` a perfectly logged-in CLI reports ``loggedIn: false`` - instantly, with empty
stderr and no upstream request (confirmed in the issue thread with ``claude auth status``).
A supervised gateway or a filtered env dict can omit it. And with stderr on DEVNULL a
gateway-only failure was undiagnosable: the reporter had to patch the plugin to learn the
stream was empty.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

NATIVE = r'''
import json, os, sys
mode = sys.argv[1]
user = os.environ.get('USER') or os.environ.get('USERNAME') or ''
for line in sys.stdin:
    frame = json.loads(line)
    if frame.get('shouldQuery') is False:
        print(json.dumps({'type': 'result', 'num_turns': 0}), flush=True)
        continue
    break
if mode == 'identity' and not user:
    print(json.dumps({'type': 'assistant', 'error': 'authentication_failed', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Not logged in'}]}}), flush=True)
    print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'usage': {'input_tokens': 0, 'output_tokens': 0}}), flush=True)
    sys.exit(0)
if mode == 'stderr':
    sys.stderr.write('secret diagnostic: keychain locked\n')
    print(json.dumps({'type': 'assistant', 'error': 'authentication_failed', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Not logged in'}]}}), flush=True)
    print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'usage': {'input_tokens': 0, 'output_tokens': 0}}), flush=True)
    sys.exit(0)
print(json.dumps({'type': 'assistant', 'message': {'id': 'ok', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'OK'}]}}), flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'usage': {'input_tokens': 0, 'output_tokens': 0}}), flush=True)
'''


def _client(tmp_path, mode):
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    # Deliberately identity-free: the parent test env must not leak USER/USERNAME in.
    return directsdk.Client(
        command=[sys.executable, str(native), mode],
        env={'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9'},
        timeout=25,
    )


def test_child_env_carries_the_login_identity(tmp_path):
    """The identity-free child env still passes native's local login check: the plugin
    derives USER/LOGNAME (USERNAME on Windows) before spawn."""
    client = _client(tmp_path, 'identity')
    try:
        result = client.create(model='sonnet', messages=[{'role': 'user', 'content': 'fixture'}])
        assert result.choices[0].message.content == 'OK'
    finally:
        client.close()


def test_native_stderr_reaches_the_logged_out_error(tmp_path):
    """A logged-out failure carries native's stderr so it is diagnosable."""
    client = _client(tmp_path, 'stderr')
    try:
        with pytest.raises(directsdk.ClaudeCodeLoggedOut, match='keychain locked'):
            client.create(model='sonnet', messages=[{'role': 'user', 'content': 'fixture'}])
    finally:
        client.close()

