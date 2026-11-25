"""A model id that is clearly another vendor's is refused before Claude Code starts; every Claude spelling still runs.

A `gpt-*` or `gemini-*` id can reach this provider after a `/model` or fallback mix-up. Native has no route for
it: it spawns, sends the turn upstream on the subscription login, and Anthropic answers `404 not_found_error`
(`model: gpt-5.1`). That wording matches none of core's model-not-found patterns, so the error read as unknown and
retryable: every retry spawned native again for the same 404 before Hermes fell back.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import directsdk

# Logs each spawn's --model, then sends one Messages request when the client points it at an upstream.
NATIVE = r'''
import json, os, pathlib, sys, urllib.request, urllib.error
with (pathlib.Path(os.environ["HOME"]) / "spawns.jsonl").open("a") as out:
    out.write(json.dumps(sys.argv[sys.argv.index("--model") + 1]) + "\n")
for line in sys.stdin:
    if json.loads(line).get("shouldQuery") is False:
        print(json.dumps({"type": "result", "num_turns": 0}), flush=True)
        continue
    break
if os.environ.get("SEND_UPSTREAM"):
    url = os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages"
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}), timeout=5).read()
    except urllib.error.HTTPError:
        pass
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}], "id": "m", "stop_reason": "end_turn"}}), flush=True)
print(json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
'''

FOREIGN = ['gpt-5.1', 'gpt-5.1[1m]', 'GPT-5.1', ' gpt-5.1 ', 'chatgpt-4o-latest', 'o3', 'o4-mini', 'gemini-3-pro',
           'gemma-3-27b', 'llama-4-maverick', 'meta-llama/llama-4-maverick', 'openai/gpt-5.1', 'google/gemini-3-pro',
           'mistral-large', 'deepseek-v4', 'grok-5', 'qwen3-coder', 'kimi-k2', 'glm-4.6']
# Pinned ids and their [1m] forms, plugin and Claude Code aliases, short and vendor-prefixed names, an access-gated
# id, and ids no table knows yet: a new claude-* id, a new family alias, and an id that names no vendor at all.
CLAUDE = ['claude-opus-5-5', 'claude-opus-5-5[1m]', 'sonnet', 'opus', 'haiku', 'fable', 'Opus', 'opus[1m]',
          'default', 'best', 'opusplan', 'opusplan[1m]', 'claude-haiku-4-5', 'sonnet-5.5', 'claude-mythos-5-2',
          'anthropic/claude-opus-5-5', 'openrouter/anthropic/claude-sonnet-5', 'custom/claude-sonnet-5',
          'claude-opus-9-9', 'claude-lyric-6[1m]', 'lyric', 'unqualified-future-model']


def _client(tmp_path, create_client=directsdk.Client, **env):
    script = tmp_path / 'native.py'
    script.write_text(NATIVE)
    return create_client(command=[sys.executable, str(script)],
                         env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path), **env})


def _spawns(tmp_path):
    log = tmp_path / 'spawns.jsonl'
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _request(model, streaming=False):
    return dict(model=model, messages=[{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'go'}],
                stream=streaming, timeout=SimpleNamespace(read=10))


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('model', FOREIGN)
def test_foreign_model_never_spawns_native(tmp_path, model, streaming):
    client = _client(tmp_path)
    try:
        with pytest.raises(Exception) as raised:
            result = client.chat.completions.create(**_request(model, streaming))
            if streaming:
                list(result)
        assert not client._requests
    finally:
        client.close()
    assert _spawns(tmp_path) == [], f'native spawned for {model!r}; error={raised.value!r}'
    assert type(raised.value).__name__ == 'ClaudeModelNotFound'
    assert raised.value.status_code == 404
    assert repr(model) in str(raised.value)


def test_foreign_model_is_refused_on_the_async_path(tmp_path):
    client = _client(tmp_path)

    async def call():
        return await client.create(**_request('gpt-5.1'))

    try:
        with pytest.raises(Exception) as raised:
            asyncio.run(call())
        assert not client._requests
    finally:
        client.close()
    assert type(raised.value).__name__ == 'ClaudeModelNotFound'
    assert _spawns(tmp_path) == []


def test_foreign_model_falls_back_at_once_without_reaching_anthropic(profile, tmp_path):
    from agent.error_classifier import classify_api_error

    calls = []

    class Anthropic(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers.get('Content-Length') or 0))
            body = json.dumps({'type': 'error', 'error': {'type': 'not_found_error', 'message': 'model: gpt-5.1'}}).encode()
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Anthropic)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    client = _client(tmp_path, profile.create_client, SEND_UPSTREAM='1',
                     ANTHROPIC_BASE_URL=f'http://127.0.0.1:{peer.server_port}')
    try:
        with pytest.raises(Exception) as raised:
            client.create(**_request('gpt-5.1'))
    finally:
        client.close()
        peer.shutdown()
        thread.join()
        peer.server_close()
    assert calls == [] and _spawns(tmp_path) == []
    # A large session: a refusal read as a generic 400 could be taken for an overflow and compress the history.
    verdict = classify_api_error(raised.value, provider=profile.name, model='gpt-5.1',
                                 approx_tokens=190_000, context_length=200_000, num_messages=400)
    assert (verdict.reason.value, verdict.retryable, verdict.should_fallback, verdict.should_compress) == \
        ('model_not_found', False, True, False)
    assert profile.classify_api_error(raised.value) == {'reason': 'model_not_found', 'retryable': False, 'should_fallback': True}


def test_other_refusals_keep_their_verdict(profile):
    assert profile.classify_api_error(directsdk.RequestRejected('model is required')) == \
        {'reason': 'format_error', 'retryable': False, 'should_fallback': True}
    assert profile.classify_api_error(directsdk.ClaudeAPIError('Native API error: model: gpt-5.1', 404)) is None


@pytest.mark.parametrize('model', CLAUDE)
def test_claude_models_and_new_ids_still_spawn_native(tmp_path, model):
    client = _client(tmp_path)
    try:
        result = client.chat.completions.create(**_request(model))
    finally:
        client.close()
    assert result.choices[0].message.content == 'ok'
    assert _spawns(tmp_path) == [directsdk.native_model(model)]


def test_guard_never_refuses_a_route_the_profile_or_picker_knows(profile):
    from model_catalog import ACCESS_GATED, ALIASES, CONTEXT_WINDOWS, names_foreign_model

    known = {*profile.fallback_models, *profile.model_aliases, *profile.model_aliases.values(),
             *ACCESS_GATED, *ALIASES, *ALIASES.values(), *CONTEXT_WINDOWS}
    for model in known:
        for spelling in (model, model + '[1m]', model.upper(), 'anthropic/' + model):
            assert not names_foreign_model(spelling), spelling
    for model in FOREIGN:
        assert names_foreign_model(model), model
