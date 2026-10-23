"""A reasoning-off request on a route that cannot disable thinking must not fall back to native's
default effort."""
import json

import directsdk


def reasoning_off_body(model, reasoning):
    return json.loads(directsdk.request_body({
        'model': model, 'messages': [{'role': 'user', 'content': 'go'}],
        'extra_body': {'reasoning': reasoning}})[0])


def test_reasoning_off_asks_for_the_lowest_effort_where_thinking_cannot_be_disabled():
    # These routes 400 on the disable, so "off" is impossible; with no effort native thinks at its
    # default (measured: 600-1200 reasoning tokens on a short structured answer, ~2x the latency).
    for route in ('haiku', 'claude-haiku-5-5[1m]', 'sonnet', 'claude-sonnet-5-5', 'opus', 'claude-opus-5-5', 'fable'):
        for reasoning in ({'enabled': False}, {'effort': 'none'}):
            body = reasoning_off_body(route, reasoning)
            assert 'thinking' not in body, route
            assert body['output_config'] == {'effort': 'low'}, route


def test_reasoning_off_keeps_the_real_disable_where_it_is_accepted():
    for route in ('claude-sonnet-5', 'claude-opus-5', 'claude-haiku-4-5'):
        body = reasoning_off_body(route, {'enabled': False})
        assert body['thinking'] == {'type': 'disabled'}, route
        assert 'output_config' not in body, route


def test_reasoning_off_effort_coexists_with_a_structured_output_format():
    fmt = {'type': 'json_schema', 'json_schema': {'name': 'o', 'schema': {'type': 'object', 'properties': {}}}}
    body = json.loads(directsdk.request_body({
        'model': 'haiku', 'messages': [{'role': 'user', 'content': 'go'}], 'response_format': fmt,
        'extra_body': {'reasoning': {'enabled': False}}})[0])
    assert body['output_config']['effort'] == 'low'
    assert body['output_config']['format']['type'] == 'json_schema'


def test_reasoning_enabled_effort_is_unchanged():
    body = reasoning_off_body('haiku', {'enabled': True, 'effort': 'high'})
    assert body['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert body['output_config'] == {'effort': 'high'}
