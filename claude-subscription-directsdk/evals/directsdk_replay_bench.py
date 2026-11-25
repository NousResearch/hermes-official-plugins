"""Replay cost of the per-call path alone, on the synthetic CLI fixture: no Claude Code, no network, no credentials.

Session mode removes the history replay from every tool round; this measures what that replay costs on the plugin's
side as the history grows, with a CLI that acknowledges each frame at once (``tests/fixtures/lifecycle_cli.py``). The
real binary's own start-up and parsing are not in these numbers; ``directsdk_session_bench.py`` measures those.

Run: python evals/directsdk_replay_bench.py [--max-rounds 200] [--step 25] [--repeats 5]
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import directsdk

FAKE_CLI = ROOT / 'tests' / 'fixtures' / 'lifecycle_cli.py'
TOOLS = [{'type': 'function', 'function': {'name': 'read_file', 'description': 'Read a file',
          'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}}]


def history(rounds):
    """A finished tool loop of ``rounds`` rounds, then the user's next question (canonical Hermes shape, no carriers)."""
    messages = [{'role': 'system', 'content': 'You are a bench agent. ' * 40}, {'role': 'user', 'content': 'go'}]
    for i in range(rounds):
        messages.append({'role': 'assistant', 'content': f'step {i}', 'tool_calls': [
            {'id': f'call_{i:04d}', 'type': 'function', 'function': {'name': 'read_file', 'arguments': json.dumps({'path': f'f{i}'})}}]})
        messages.append({'role': 'tool', 'tool_call_id': f'call_{i:04d}', 'content': f'contents of f{i} ' + 'x' * 64})
    messages.append({'role': 'user', 'content': 'and now?'})
    return messages


def run(max_rounds, step, repeats):
    directsdk.session_settings = lambda: (False, 4)
    rows = []
    with tempfile.TemporaryDirectory(prefix='directsdk-replay-bench-') as home:
        env = {'PATH': os.defpath, 'HOME': home, 'CLAUDE_CONFIG_DIR': home, 'REVIEW_PIDS': os.path.join(home, 'pids'),
               'REVIEW_STARTED': os.path.join(home, 'started'), 'REVIEW_ENV': os.path.join(home, 'env'), 'REVIEW_MODE': 'normal'}
        c = directsdk.Client(command=[sys.executable, '-I', str(FAKE_CLI)], timeout=60, env=env)
        try:
            for rounds in range(0, max_rounds + 1, step):
                messages, walls = history(rounds), []
                for _ in range(repeats):
                    started = time.perf_counter()
                    reply = c.create(model='claude-sonnet-5-5', messages=messages, tools=TOOLS, max_tokens=1000)
                    walls.append(time.perf_counter() - started)
                    assert reply.choices[0].message.content == 'offline-ok'
                rows.append({'rounds': rounds, 'frames': 2 * rounds + 1, 'wall_s': {
                    'min': round(min(walls), 4), 'median': round(statistics.median(walls), 4), 'max': round(max(walls), 4)}})
                print(json.dumps(rows[-1]), file=sys.stderr, flush=True)
        finally:
            c.close()
    return {'cli': 'tests/fixtures/lifecycle_cli.py (synthetic, acknowledges each frame at once)', 'repeats': repeats, 'rows': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--max-rounds', type=int, default=200)
    parser.add_argument('--step', type=int, default=25)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(run(args.max_rounds, args.step, args.repeats), indent=2))
