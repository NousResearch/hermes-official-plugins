"""What closing the per-step relay costs, offline: no credentials, no inference.

Every model step starts a loopback relay (``admission.Admission``) and closes it when its Claude Code has exited.
``close()`` used to wait for the relay's ``serve_forever`` to notice the shutdown at its next 50 ms poll. This measures:

- ``close_ms``: ``Admission.close()`` right after the relay starts, ``--closes`` times (no Claude Code needed): the
  worst case, a full poll;
- ``close_after_idle_ms``: the same after the relay has idled a random 0 to 100 ms, as when Claude Code exits a while
  after its answer, so the close lands anywhere in a poll;
- with a Claude Code binary, ``step_s``: per-call steps of one tool loop at ``max_concurrent`` 1, where each step waits
  for the previous step's process to exit and its relay to close before it may start (the case a subagent fan-out
  at the cap is in), against a scripted loopback upstream.

Run: python evals/directsdk_relay_close.py [/path/to/claude] [--closes 200] [--steps 40] [--out x.json]
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import random
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import admission  # noqa: E402

MODEL = 'claude-sonnet-5-5'
TOOLS = [{'type': 'function', 'function': {'name': 'read_file', 'description': 'Read a file',
          'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}}]
USAGE = {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}


def dist(values):
    values = sorted(values)
    return {'n': len(values), 'min': round(values[0], 4), 'median': round(statistics.median(values), 4),
            'p95': round(values[int(round(.95 * (len(values) - 1)))], 4), 'max': round(values[-1], 4)}


def closes(n, idle=0.0):
    out, rng = [], random.Random(3889)
    for _ in range(n):
        gate = admission.Admission('http://127.0.0.1:9', 5)
        time.sleep(rng.uniform(0, idle))
        started = time.perf_counter()
        gate.close()
        out.append((time.perf_counter() - started) * 1000)
    return out


class Upstream(BaseHTTPRequestHandler):
    """A tool call per step (unique ids), from the history alone."""

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        n = sum(m['role'] == 'assistant' for m in body['messages'])
        block = {'type': 'tool_use', 'id': f'toolu_{n:04d}', 'name': 'mcp__hermes__read_file', 'input': {'path': f'f{n}'}}
        events = [{'type': 'message_start', 'message': {'id': f'msg_{n}', 'type': 'message', 'role': 'assistant',
                                                       'model': MODEL, 'content': [], 'usage': USAGE}},
                  {'type': 'content_block_start', 'index': 0, 'content_block': {**block, 'input': {}}},
                  {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}},
                  {'type': 'content_block_stop', 'index': 0},
                  {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': USAGE}, {'type': 'message_stop'}]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())


def steps(binary, n):
    import directsdk
    directsdk.max_concurrent = lambda: 1
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out = []
    try:
        # A finished one-shot's Claude Code can still write under HOME while its drain ends it (seen on macOS).
        with tempfile.TemporaryDirectory(prefix='directsdk-relay-close-', ignore_cleanup_errors=True) as home:
            c = directsdk.Client(command=binary, timeout=60, env={
                'PATH': os.defpath, 'HOME': home, 'CLAUDE_CONFIG_DIR': home, 'ANTHROPIC_API_KEY': 'fixture-key',
                'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}', 'NO_PROXY': '127.0.0.1',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'})
            messages = [{'role': 'system', 'content': 'You are a bench agent.'}, {'role': 'user', 'content': 'go'}]
            try:
                for _ in range(n):
                    started = time.perf_counter()
                    message = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
                    out.append(time.perf_counter() - started)
                    messages.append({k: v for k, v in message.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None})
                    messages += [{'role': 'tool', 'tool_call_id': call['id'], 'content': 'contents'} for call in message['tool_calls']]
            finally:
                c.close()
    finally:
        server.shutdown()
        server.server_close()
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('binary', nargs='?')
    parser.add_argument('--closes', type=int, default=200)
    parser.add_argument('--steps', type=int, default=40)
    parser.add_argument('--out')
    args = parser.parse_args()
    report = {'close_ms': dist(closes(args.closes)), 'close_after_idle_ms': dist(closes(args.closes, .1))}
    if args.binary:
        report['native_version'] = subprocess.check_output([args.binary, '--version'], text=True, stdin=subprocess.DEVNULL).strip()
        # The first step pays Claude Code's own first start (disk cache); the rest are steady state.
        walls = steps(args.binary, args.steps)
        report.update(first_step_s=round(walls[0], 4), step_s=dist(walls[1:]))
    print(json.dumps(report, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
