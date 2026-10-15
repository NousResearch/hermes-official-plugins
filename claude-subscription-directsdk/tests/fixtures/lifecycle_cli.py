"""Bounded synthetic CLI. No Claude import, credentials, or networking."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def record():
    with open(os.environ['REVIEW_PIDS'], 'a') as f:
        f.write(json.dumps({'pid': os.getpid(),
                            'pgid': os.getpgrp() if hasattr(os, 'getpgrp') else None}) + '\n')


record()
if '--descendant' in sys.argv:
    time.sleep(20)
    sys.exit(0)
mode = os.environ.get('REVIEW_MODE', 'normal')
if mode in ('launcher_exit', 'launcher_live'):
    subprocess.Popen([sys.executable, '-I', __file__, '--descendant'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=dict(os.environ))
    if mode == 'launcher_live':
        time.sleep(20)
    sys.exit(0)
Path(os.environ['REVIEW_STARTED']).write_text('started')
if '--version' in sys.argv:
    print('2.1.263 (synthetic offline CLI)')
    sys.exit(0)
if sys.argv[1:3] == ['auth', 'status']:
    Path(os.environ['REVIEW_ENV']).write_text(json.dumps(dict(os.environ)))
    print(json.dumps({'loggedIn': True, 'subscriptionType': 'pro'}))
    sys.exit(0)
if mode == 'no_read':
    time.sleep(20)
    sys.exit(0)
for line in sys.stdin:
    row = json.loads(line)
    if row.get('shouldQuery') is False:
        print(json.dumps({'type': 'result', 'num_turns': 0, 'is_error': False}), flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_delta',
      'delta': {'type': 'text_delta', 'text': 'offline-ok'}}}), flush=True)
print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant',
      'content': [{'type': 'text', 'text': 'offline-ok'}], 'stop_reason': 'end_turn'}}), flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
      'num_turns': 1, 'usage': {'input_tokens': 1, 'output_tokens': 1}}), flush=True)
