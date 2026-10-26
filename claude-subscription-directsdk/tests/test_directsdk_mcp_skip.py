"""Tool-less requests skip the inert MCP helper entirely (#42)."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

NATIVE = r'''
import json, os, pathlib, sys
argv = sys.argv
mcp = json.loads(argv[argv.index('--mcp-config') + 1])
root = pathlib.Path(argv[argv.index('--system-prompt-file') + 1]).parent
report = {'mcp': mcp, 'tools_json': (root / 'tools.json').exists()}
pathlib.Path(os.environ['REPORT']).write_text(json.dumps(report))
assert (rows_ok := True)
for line in sys.stdin:
    frame = json.loads(line)
    break
usage = {'input_tokens': 1, 'output_tokens': 1}
print(json.dumps({'type': 'assistant', 'message': {'id': 'm1', 'role': 'assistant', 'model': 'sonnet',
      'content': [{'type': 'text', 'text': 'OK'}], 'stop_reason': 'end_turn', 'usage': usage}}), flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'usage': usage}), flush=True)
'''


def _client(tmp_path):
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    report = tmp_path / 'report.json'
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'REPORT': str(report)}
    return directsdk.Client(command=[sys.executable, str(native)], env=env), report


def _messages():
    return [{'role': 'user', 'content': 'go'}]


def test_a_request_without_tools_skips_the_helper(tmp_path):
    client, report = _client(tmp_path)
    try:
        result = client.create(model='sonnet', messages=_messages())
        assert result.choices[0].message.content == 'OK'
    finally:
        client.close()
    seen = json.loads(report.read_text())
    assert seen == {'mcp': {'mcpServers': {}}, 'tools_json': False}


def test_a_request_with_tools_still_mounts_the_helper(tmp_path):
    client, report = _client(tmp_path)
    tools = [{'type': 'function', 'function': {'name': 'probe', 'description': 'd',
              'parameters': {'type': 'object', 'properties': {}}}}]
    try:
        client.create(model='sonnet', messages=_messages(), tools=tools)
    finally:
        client.close()
    seen = json.loads(report.read_text())
    assert list(seen['mcp']['mcpServers']) == ['hermes']
    assert seen['tools_json'] is True
