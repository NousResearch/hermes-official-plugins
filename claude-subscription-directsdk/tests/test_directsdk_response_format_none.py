"""An explicit response_format=None must not discard extra_body's structured-output contract."""
import json

import directsdk

FMT = {"type": "json_schema", "json_schema": {
    "name": "out", "strict": True, "schema": {"type": "object", "properties": {"x": {"type": "string"}}}}}


def _body(**kwargs):
    return json.loads(directsdk.request_body({
        "model": "claude-sonnet-5", "messages": [{"role": "user", "content": "go"}], **kwargs})[0])


def test_explicit_none_keeps_extra_body_response_format():
    body = _body(response_format=None, extra_body={"response_format": FMT})
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "response_format" not in body


def test_kwargs_response_format_still_wins():
    other = dict(FMT)
    other["json_schema"] = dict(FMT["json_schema"], name="other")
    body = _body(response_format=other, extra_body={"response_format": FMT})
    assert body["output_config"]["format"]["schema"]["properties"] == {"x": {"type": "string"}}


def test_extra_body_only_still_works():
    assert _body(extra_body={"response_format": FMT})["output_config"]["format"]["type"] == "json_schema"
