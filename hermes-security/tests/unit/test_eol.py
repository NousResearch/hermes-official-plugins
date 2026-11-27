"""Tests for non-dict JSON handling in hermes_security.dependencies.eol."""
import io
import json

from hermes_security.dependencies import eol


class _FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=None):
        return self._raw


def test_declarations_ignores_non_dict_json_manifests():
    files = ['package.json', 'composer.json', 'ok/package.json']
    contents = {
        'package.json': '[]',
        'composer.json': 'null',
        'ok/package.json': '{"engines": {"node": "20"}}',
    }
    found = eol.declarations(files, contents.get)
    assert found == [('nodejs', '20', 'ok/package.json')]


def test_check_runtimes_handles_non_dict_api_payloads():
    files = ['package.json']
    contents = {'package.json': '{"engines": {"node": "20"}}'}
    for payload in ([], {'result': None}, {'result': {'releases': None}}):
        rows = eol.check_runtimes(files, contents.get, opener=lambda *_a, **_kw: _FakeResponse(payload))
        assert len(rows) == 1
        assert rows[0]['status'] == 'unknown'
        assert rows[0]['reason'] == 'release cycle not listed on endoflife.date'


def test_check_runtimes_handles_non_dict_latest_field():
    files = ['package.json']
    contents = {'package.json': '{"engines": {"node": "20"}}'}
    payload = {'result': {'releases': [{'name': '20', 'isEol': False, 'eolFrom': '2026-04-30', 'latest': '20.11.0'}]}}
    rows = eol.check_runtimes(files, contents.get, opener=lambda *_a, **_kw: _FakeResponse(payload))
    assert rows == [{
        'runtime': 'nodejs',
        'declared': '20',
        'path': 'package.json',
        'cycle': '20',
        'status': 'supported',
        'eolDate': '2026-04-30',
        'latest': None,
    }]
