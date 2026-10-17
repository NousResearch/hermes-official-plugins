"""Catalog capacity must agree with the actual native --model selection."""
import json
import os
import sys

import pytest

from test_directsdk import FAKE

EXPECTED = {
    'claude-sonnet-5-5[1m]': 1_000_000,
    'claude-sonnet-5[1m]': 1_000_000,
    'claude-haiku-5-5[1m]': 1_000_000,
    'claude-haiku-4-5-20251001': 200_000,
    'claude-opus-5-5[1m]': 1_000_000,
    'claude-opus-5[1m]': 1_000_000,
    'claude-opus-4-8[1m]': 1_000_000,
    'claude-fable-5-1[1m]': 1_000_000,
}


def test_catalog_windows_match_explicit_native_routes(profile):
    from agent.model_metadata import get_model_context_length
    assert set(profile.fallback_models) == set(EXPECTED)
    assert profile.default_aux_model == 'claude-sonnet-5[1m]'
    for model, window in EXPECTED.items():
        assert profile.get_model_context_length(model) == window
        assert get_model_context_length(model, provider=profile.name) == window
        assert get_model_context_length(model, provider=profile.name, config_context_length=200000) == 200000
    # Unpinned: the plain id runs natively within the 200K gateway default; [1m] promises nothing.
    assert profile.get_model_context_length('unqualified-future-model') == 200_000
    assert profile.get_model_context_length('unqualified-future-model[1m]') is None


def test_native_argv_enables_only_known_long_context_models(profile, tmp_path):
    capture = tmp_path / 'argv.json'
    native = tmp_path / 'native.py'
    native.write_text(FAKE.replace('rows=[]', "pathlib.Path(os.environ['ARGV_CAPTURE']).write_text(json.dumps(sys.argv))\nrows=[]"))
    aliases = {'sonnet':'claude-sonnet-5-5[1m]', 'opus':'claude-opus-5-5[1m]',
               'haiku':'claude-haiku-5-5[1m]', 'claude-haiku-4-5':'claude-haiku-4-5-20251001',
               'fable':'claude-fable-5-1[1m]',
               'unqualified-future-model':'unqualified-future-model'}
    with_client = profile.create_client(command=[sys.executable,str(native)], env={'PATH':os.defpath,'HOME':str(tmp_path),'ARGV_CAPTURE':str(capture)})
    try:
        for requested, expected in {**{m:m for m in EXPECTED}, **aliases}.items():
            with_client.create(model=requested, messages=[{'role':'user','content':'fixture'}],
                               tools=[{'type':'function','function':{'name':'probe','description':'TAIL','parameters':{'type':'object','properties':{'value':{'type':'string'}}}}}])
            argv = json.loads(capture.read_text())
            assert argv[argv.index('--model')+1] == expected and '--effort' not in argv
    finally:
        with_client.close()


def test_sonnet_5_5_never_receives_the_thinking_disable():
    """Sonnet 5.5 answers ``thinking: {type: disabled}`` with a 400 (docs: thinking can't be turned
    off; lowest setting is ``between_tools``). `sonnet` now resolves to it, so Hermes' reasoning-off
    calls (title generation, ``/reasoning none``) must omit the disable on every spelling, while
    plain Sonnet 5, which still accepts it, keeps receiving it."""
    import directsdk
    def body(model):
        return json.loads(directsdk.request_body({
            'model': model, 'messages': [{'role': 'user', 'content': 'go'}],
            'extra_body': {'reasoning': {'enabled': False}}})[0])
    for route in ('sonnet', 'claude-sonnet-5-5', 'claude-sonnet-5-5[1m]'):
        assert 'thinking' not in body(route), route
        assert 'context_management' not in body(route), route
    for route in ('claude-sonnet-5', 'claude-sonnet-5[1m]'):
        assert body(route)['thinking'] == {'type': 'disabled'}, route


def test_haiku_5_5_never_receives_the_thinking_disable():
    """The docs say thinking can't be turned off on Haiku 5.5, and `haiku` now resolves to it, so
    reasoning-off calls must omit the disable on every spelling. Haiku 5.5 does take adaptive
    thinking, unlike Haiku 4.5, which keeps receiving the disable and never gets adaptive."""
    import directsdk
    def body(model, reasoning):
        return json.loads(directsdk.request_body({
            'model': model, 'messages': [{'role': 'user', 'content': 'go'}],
            'extra_body': {'reasoning': reasoning}})[0])
    for route in ('haiku', 'claude-haiku-5-5', 'claude-haiku-5-5[1m]'):
        assert 'thinking' not in body(route, {'enabled': False}), route
        assert 'context_management' not in body(route, {'enabled': False}), route
        assert body(route, {'enabled': True, 'effort': 'medium'})['thinking'] == {'type': 'adaptive', 'display': 'summarized'}, route
    for route in ('claude-haiku-4-5', 'claude-haiku-4-5-20251001'):
        assert body(route, {'enabled': False})['thinking'] == {'type': 'disabled'}, route
        assert 'thinking' not in body(route, {'enabled': True, 'effort': 'medium'}), route


@pytest.mark.parametrize('route', [
    'sonnet', 'claude-sonnet-5-5', 'claude-sonnet-5-5[1m]',
    'haiku', 'claude-haiku-5-5', 'claude-haiku-5-5[1m]',
    'opus', 'claude-opus-5-5', 'claude-opus-5-5[1m]',
    'fable', 'claude-fable-5-1', 'claude-fable-5-1[1m]',
])
def test_every_pinned_adaptive_route_asks_for_summarized_display(route):
    """4.7+ answers adaptive thinking with a signature and no text unless `display` is requested, so
    Hermes' reasoning panel stays blank on this provider. Every pinned route that receives adaptive
    thinking must carry it, not just the one the request-shape test happens to use: a route added to
    the catalog without it would silently ship signature-only blocks again."""
    import directsdk
    body = json.loads(directsdk.request_body({
        'model': route, 'messages': [{'role': 'user', 'content': 'go'}],
        'extra_body': {'reasoning': {'enabled': True, 'effort': 'medium'}}})[0])
    assert body['thinking'] == {'type': 'adaptive', 'display': 'summarized'}, route
