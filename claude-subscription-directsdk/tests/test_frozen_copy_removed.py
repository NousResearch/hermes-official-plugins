"""The plugin keeps working after core's frozen import copy is deleted under it (hermes-agent#156493).

Hermes imports a provider plugin from a frozen copy in the temp directory. A temp cleaner (Hermes' scratch
pruner, or macOS's daily cleanup of files older than three days) can remove that copy while the process runs.
"""
import json
import os
import shutil
import sys
from pathlib import Path

NATIVE = r'''
import json, os, pathlib, subprocess, sys
argv = sys.argv
mcp = json.loads(argv[argv.index('--mcp-config') + 1])
server = mcp['mcpServers']['hermes']
helper = subprocess.run([server['command'], *server['args']], text=True, capture_output=True, timeout=30,
                        input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n')
listed = [row['name'] for row in json.loads(helper.stdout)['result']['tools']] if helper.returncode == 0 else None
pathlib.Path(os.environ['REPORT']).write_text(json.dumps({'helper': server['args'][0], 'listed': listed}))
for line in sys.stdin:
    break
usage = {'input_tokens': 1, 'output_tokens': 1}
print(json.dumps({'type': 'assistant', 'message': {'id': 'm1', 'role': 'assistant', 'model': 'sonnet',
      'content': [{'type': 'text', 'text': 'OK'}], 'stop_reason': 'end_turn', 'usage': usage}}), flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'usage': usage}), flush=True)
'''


def test_requests_work_after_the_frozen_copy_is_deleted(profile, tmp_path, monkeypatch):
    # In Hermes only the frozen copy holds the plugin's modules: hide the repo checkout the test bootstrap puts
    # on sys.path, or the flat-import fallback would load from it and mask the bug.
    repo = str(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(sys, 'path', [entry for entry in sys.path if str(Path(entry or '.').resolve()) != repo])
    for name in ('directsdk', 'directsdk_setup', 'admission', 'model_catalog', 'inert_mcp'):
        monkeypatch.delitem(sys.modules, name, raising=False)
    package = sys.modules[type(profile).__module__]
    copy = Path(package.__file__).resolve().parent
    installed = Path(os.environ['HERMES_HOME']).resolve() / 'plugins'
    assert installed not in copy.parents, 'core no longer imports from a frozen copy; this test proves nothing'
    shutil.rmtree(copy)

    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    report = tmp_path / 'report.json'
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'REPORT': str(report)}
    client = profile.create_client(command=[sys.executable, str(native)], env=env)
    # The client must come from the discovered package, not a flat-import fallback bound before sys.path changed.
    assert type(client).__module__ == package.__name__ + '.directsdk'
    tools = [{'type': 'function', 'function': {'name': 'probe', 'description': 'd',
              'parameters': {'type': 'object', 'properties': {}}}}]
    try:
        result = client.create(model='sonnet', messages=[{'role': 'user', 'content': 'go'}], tools=tools)
        assert result.choices[0].message.content == 'OK'
    finally:
        client.close()
    seen = json.loads(report.read_text())
    assert copy not in Path(seen['helper']).parents
    assert seen['listed'] == ['probe']
