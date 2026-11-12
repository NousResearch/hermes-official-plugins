"""Long-session bench: per-call against session mode on the real Claude Code CLI and a scripted loopback upstream.

No credentials, no inference. For each mode a Hermes-shaped loop runs one user turn of ``--rounds`` tool rounds, then a
compaction (the tool rounds replaced by a summary) and a second turn, then an edit to a tool result mid-history. It
measures what the long-session verdict waits on: wall time per model step as the history grows, Claude Code
processes started and alive at once, RSS of a kept process, and that a kept process restarts correctly after a
compaction or edit and survives a tool parked for ``--park`` seconds. Both modes see the same HOME and the same
deterministic replies, so their upstream bodies must be byte-identical.

Run: python evals/directsdk_session_bench.py /path/to/claude [--rounds 200] [--park 0] [--out bench.json]
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import directsdk

VERBOSE = False
MODEL = 'claude-sonnet-5-5'
TOOLS = [{'type': 'function', 'function': {'name': 'read_file', 'description': 'Read a file',
          'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}}]
USAGE = {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
SYSTEM = 'You are a bench agent. ' * 40


def opens_turn(message):
    """A user message that starts a turn: one without any tool result (native may add text beside tool results)."""
    content = message.get('content')
    return message.get('role') == 'user' and (isinstance(content, str) or not any(b.get('type') == 'tool_result' for b in content))


def extra_text(message):
    """Text native put beside tool results in a user message (a reminder the relay did not fold away)."""
    content = message.get('content')
    if message.get('role') != 'user' or isinstance(content, str) or not any(b.get('type') == 'tool_result' for b in content):
        return []
    return [b.get('text', '')[:120] for b in content if b.get('type') == 'text']


class Upstream(BaseHTTPRequestHandler):
    """Answers from the history alone: ``server.rounds`` tool calls per user turn, then text. Keeps every body."""

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        self.server.bodies.append(raw)
        body = json.loads(raw)
        rounds = total = 0
        opening = ''
        for message in body['messages']:
            if opens_turn(message):
                rounds = 0
                content = message['content']
                opening = content if isinstance(content, str) else ''.join(b.get('text', '') for b in content if b.get('type') == 'text')
            elif message.get('role') == 'assistant':
                rounds, total = rounds + 1, total + 1
            elif VERBOSE and extra_text(message):
                print(json.dumps({'extra_text_beside_tool_results': extra_text(message)}), file=sys.stderr, flush=True)
        # The turn opened by "go" is the long one; later turns (after compaction, after an edit) take three rounds each.
        # Consecutive user texts merge into one wire message, so the opening text is read from its end.
        tool = rounds < (self.server.rounds if opening.endswith('go') else 3)
        # Ids unique across the whole conversation: a duplicate tool_use id makes native drop the block and replace
        # the orphaned tool result with "(no content)" (2.1.322), which no real Hermes history produces.
        rounds = total
        if VERBOSE:
            kinds = {'text': 'x', 'thinking': 'h', 'tool_use': 'U', 'tool_result': 'R'}
            shape = [m['role'][0] + (':str' if isinstance(m.get('content'), str) else ''.join(kinds.get(b.get('type'), '?') for b in m['content']))
                     for m in body['messages']]
            print(json.dumps({'wire': shape, 'rounds': rounds}), file=sys.stderr, flush=True)
            if os.environ.get('BENCH_DUMP'):
                print(json.dumps(body['messages'][-4:])[:2500], file=sys.stderr, flush=True)
        blocks = [{'type': 'thinking', 'thinking': f'plan {rounds}', 'signature': f'sig{rounds:04d}'},
                  {'type': 'text', 'text': f'step {rounds}'}]
        if tool:
            blocks.append({'type': 'tool_use', 'id': f'toolu_{rounds:04d}', 'name': 'mcp__hermes__read_file',
                           'input': {'path': f'f{rounds}'}})
        events = [{'type': 'message_start', 'message': {'id': f'msg_{rounds}', 'type': 'message', 'role': 'assistant',
                                                       'model': body['model'], 'content': [], 'usage': USAGE}}]
        for i, block in enumerate(blocks):
            start = {**block, **({'thinking': '', 'signature': ''} if block['type'] == 'thinking' else
                                 {'input': {}} if block['type'] == 'tool_use' else {'text': ''})}
            events.append({'type': 'content_block_start', 'index': i, 'content_block': start})
            delta = ({'type': 'thinking_delta', 'thinking': block['thinking']}, {'type': 'signature_delta', 'signature': block['signature']}) \
                if block['type'] == 'thinking' else ({'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])},) \
                if block['type'] == 'tool_use' else ({'type': 'text_delta', 'text': block['text']},)
            events += [{'type': 'content_block_delta', 'index': i, 'delta': d} for d in delta]
            events.append({'type': 'content_block_stop', 'index': i})
        events += [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if tool else 'end_turn'}, 'usage': USAGE},
                   {'type': 'message_stop'}]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(''.join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode())
        self.wfile.flush()


class Spawns:
    """Exact count of Claude Code turn processes started: wraps the plugin's own spawn, not every Popen."""

    def __init__(self):
        self.pids, self.real = [], directsdk.Request.spawn

    def __enter__(self):
        bench = self

        def counting(request, *args, **kwargs):
            process = bench.real(request, *args, **kwargs)
            bench.pids.append(process.pid)
            return process
        directsdk.Request.spawn = counting
        return self

    def __exit__(self, *_):
        directsdk.Request.spawn = self.real


def drained(within=15):
    """Wait for one-shots handed off by the previous mode to exit, so they do not count as the next mode's processes."""
    deadline = time.monotonic() + within
    while directsdk._draining and time.monotonic() < deadline:
        time.sleep(.05)


class Sampler(threading.Thread):
    """How many Claude Code processes are alive at once (descendants of this process), sampled every 0.2 s."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop, self.max_alive, self.samples = threading.Event(), 0, 0

    def run(self):
        me = psutil.Process()
        while not self.stop.is_set():
            try:
                alive = [p for p in me.children(recursive=True) if p.status() != psutil.STATUS_ZOMBIE]
            except psutil.Error:
                alive = []
            self.max_alive, self.samples = max(self.max_alive, len(alive)), self.samples + 1
            time.sleep(.2)


def rss_of(pid):
    try:
        return psutil.Process(pid).memory_info().rss
    except psutil.Error:
        return None


def client(binary, home, upstream, session):
    directsdk.session_settings = lambda: (session, 4)
    return directsdk.Client(command=binary, timeout=60, env={
        'PATH': os.defpath, 'HOME': home, 'CLAUDE_CONFIG_DIR': home, 'ANTHROPIC_API_KEY': 'fixture-key',
        'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{upstream.server_port}', 'NO_PROXY': '127.0.0.1',
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1'})


def turn(c, messages, rows, *, phase, rounds, park=0, park_at=None):
    """Hermes' loop for one user turn: one create() per model step, the tool results appended for each call."""
    budget = len(messages) + 2 * (rounds + 2)
    while True:
        if len(messages) > budget:
            raise RuntimeError(f'{phase}: the turn did not end after {rounds} tool rounds')
        # The last known kept pid comes from the previous row, not from the client: once idle eviction has cleared
        # ``_session`` (during a park), the client no longer knows which process served the step before.
        previous = rows[-1]['kept_pid'] if rows else None
        started = time.perf_counter()
        message = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
        wall = time.perf_counter() - started
        kept = c._session.process.pid if c._session is not None else None
        row = {'phase': phase, 'history': len(messages), 'wall_s': round(wall, 4), 'tool': bool(message.get('tool_calls')),
               'kept_pid': kept, 'restarted': previous is not None and kept != previous, 'kept_rss': rss_of(kept) if kept else None}
        rows.append(row)
        if VERBOSE:
            print(json.dumps(row), file=sys.stderr, flush=True)
        messages.append({k: v for k, v in message.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None})
        if not message.get('tool_calls'):
            return
        if park and park_at is not None and len([r for r in rows if r['phase'] == phase and r['tool']]) == park_at:
            # The tool is parked on a human (an approval): the kept process must hold the call open meanwhile.
            time.sleep(park)
            row['parked_s'] = park
        messages += [{'role': 'tool', 'tool_call_id': call['id'], 'content': 'contents of ' + call['id'] + ' ' + 'x' * 64}
                     for call in message['tool_calls']]


def scenario(binary, home, upstream, session, rounds, park):
    """One full run of a mode. Returns the per-step rows, the upstream bodies and the process counts."""
    rows, sampler = [], Sampler()
    drained()
    c = client(binary, home, upstream, session)
    sampler.start()
    try:
        with Spawns() as spawns:
            messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': 'go'}]
            turn(c, messages, rows, phase='turn1', rounds=rounds, park=park, park_at=3 if park else None)
            # Hermes compaction: the finished tool loop becomes a summary; the kept process cannot continue this.
            compacted = messages[:2] + [{'role': 'user', 'content': f'Summary of earlier work: read f0..f{rounds - 1}.'}]
            compacted.append({'role': 'user', 'content': 'and now?'})
            turn(c, compacted, rows, phase='after_compaction', rounds=3)
            # An edited history (a changed tool result): refused by the relay before upstream, replayed into a new process.
            edited = list(compacted)
            index = next(i for i, m in enumerate(edited) if m.get('role') == 'tool')
            edited[index] = {**edited[index], 'content': edited[index]['content'] + ' (edited)'}
            edited.append({'role': 'user', 'content': 'once more'})
            turn(c, edited, rows, phase='after_edit', rounds=3)
    finally:
        c.close()
        sampler.stop.set()
        sampler.join(2)
    return rows, list(upstream.bodies), {'started': len(spawns.pids), 'max_alive': sampler.max_alive, 'samples': sampler.samples}


def dist(values):
    values = sorted(values)
    if not values:
        return None
    return {'n': len(values), 'min': round(values[0], 4), 'median': round(statistics.median(values), 4),
            'p95': round(values[min(len(values) - 1, int(round(.95 * (len(values) - 1))))], 4), 'max': round(values[-1], 4)}


def summarize(rows, rounds):
    steps = [r for r in rows if r['phase'] == 'turn1' and r['tool']]
    buckets = {}
    for lo in range(0, rounds, 50):
        bucket = [r['wall_s'] for r in steps[lo:lo + 50]]
        if bucket:
            buckets[f'rounds_{lo}_{lo + len(bucket) - 1}'] = dist(bucket)
    rss = [r['kept_rss'] for r in rows if r.get('kept_rss')]
    parked = next((i for i, r in enumerate(rows) if r.get('parked_s')), None)
    # The process that answered the parked call is the one the next step ran on: it must be the one that made the call.
    same_after = parked is not None and parked + 1 < len(rows) and rows[parked + 1]['kept_pid'] is not None \
        and rows[parked + 1]['kept_pid'] == rows[parked]['kept_pid']
    return {'tool_round_wall_s': dist([r['wall_s'] for r in steps]), 'by_history': buckets,
            'first_step_wall_s': rows[0]['wall_s'], 'restarts': sum(r['restarted'] for r in rows),
            'restart_after_compaction': next((r['restarted'] for r in rows if r['phase'] == 'after_compaction'), None),
            'restart_after_edit': next((r['restarted'] for r in rows if r['phase'] == 'after_edit'), None),
            'kept_rss_mb': dist([v / 2**20 for v in rss]) if rss else None,
            'parked': None if parked is None else {'seconds': rows[parked]['parked_s'], 'same_process_after': same_after}}


def save(report, out):
    """The full report so far (every step), rewritten after each repeat so a cut run still leaves its data."""
    if out:
        Path(out).write_text(json.dumps(report, indent=1))


def run(binary, rounds, park, repeats, out=None):
    binary = shutil.which(binary) or str(Path(binary).absolute())  # a bare `claude` resolves on PATH
    version = subprocess.check_output([binary, '--version'], text=True, stdin=subprocess.DEVNULL,
                                      env={'PATH': os.defpath, 'HOME': tempfile.gettempdir()}).strip()
    report = {'native_version': version, 'rounds': rounds, 'park_s': park, 'repeats': repeats, 'runs': [], 'modes': {}}
    walls = {'per_call': [], 'session': []}
    for repeat in range(repeats):
        with tempfile.TemporaryDirectory(prefix='directsdk-bench-') as home:
            bodies = {}
            for mode, session in (('per_call', False), ('session', True)):
                server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
                server.bodies, server.rounds = [], rounds
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    started = time.perf_counter()
                    rows, bodies[mode], processes = scenario(binary, home, server, session, rounds, park if session else 0)
                    total = time.perf_counter() - started
                finally:
                    server.shutdown()
                    server.server_close()
                summary = summarize(rows, rounds)
                summary.update(processes=processes, total_wall_s=round(total, 2))
                walls[mode] += [r['wall_s'] for r in rows if r['phase'] == 'turn1' and r['tool']]
                report['runs'].append({'repeat': repeat, 'mode': mode, 'summary': summary, 'steps': rows})
                print(json.dumps({'repeat': repeat, 'mode': mode, **summary}), file=sys.stderr, flush=True)
            identical = bodies['per_call'] == bodies['session']
            report['runs'][-1]['bytes_identical_to_per_call'] = identical
            if not identical:
                diverging = next((i for i, (a, b) in enumerate(zip(bodies['per_call'], bodies['session'])) if a != b), min(map(len, bodies.values())))
                report['runs'][-1]['first_differing_request'] = diverging
            print(json.dumps({'repeat': repeat, 'bytes_identical_to_per_call': identical, 'requests': len(bodies['per_call'])}), file=sys.stderr, flush=True)
            for mode in walls:
                report['modes'][mode] = {'tool_round_wall_s': dist(walls[mode])}
            save(report, out)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('binary')
    parser.add_argument('--rounds', type=int, default=200, help='tool rounds in the first turn (default 200)')
    parser.add_argument('--park', type=float, default=0, help='seconds to park the third tool call on (session mode)')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--out', help='write the full report (every step) here; stdout gets the summary')
    parser.add_argument('--verbose', action='store_true', help='print every step to stderr as it completes')
    args = parser.parse_args()
    VERBOSE = args.verbose
    result = run(args.binary, args.rounds, args.park, args.repeats, args.out)
    if args.out:
        for r in result['runs']:
            r.pop('steps', None)
    print(json.dumps(result, indent=2))
