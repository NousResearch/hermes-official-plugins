"""#329: one Hermes process runs at most ``max_concurrent`` Claude Code turn processes at once.

Each fake native records when it starts and exits and how many fakes were alive when it started.
"""
import json
import os
import sys
import threading
import time

import pytest

import directsdk as ds

NATIVE = r'''
import json, os, sys, time
from pathlib import Path
live, log = Path(os.environ['LIVE']), Path(os.environ['LOG'])
me = live / str(os.getpid())
me.touch()
def note(event, **extra):
    with open(log, 'a') as f:
        f.write(json.dumps({'pid': os.getpid(), 'event': event, 't': time.time(), 'live': len(list(live.iterdir())), **extra}) + '\n')
note('start', tag=os.environ['TAG'])
for line in sys.stdin:
    pass
mode = os.environ['MODE']
if mode == 'hang':
    while not Path(os.environ['RELEASE']).exists():
        time.sleep(.01)
time.sleep(float(os.environ.get('WORK', '0.3')))
if mode == 'crash':
    me.unlink(); note('end'); sys.exit(3)
usage = {'input_tokens': 1, 'output_tokens': 1}
print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'ok'}}}), flush=True)
print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn', 'usage': usage}}), flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'usage': usage}), flush=True)
me.unlink(); note('end')
'''


@pytest.fixture
def natives(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, 'apply_traffic_policy', lambda env: env)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    (tmp_path / 'live').mkdir()
    script = tmp_path / 'native.py'
    script.write_text(NATIVE)
    log, clients = tmp_path / 'log.jsonl', []

    def limit(n):
        # Where `hermes config set plugins.entries.<id>.settings.max_concurrent <n>` writes it.
        (home / 'config.yaml').write_text('plugins:\n  entries:\n    claude-subscription-directsdk-experimental:\n'
                                          f'      settings:\n        max_concurrent: {n}\n', encoding='utf-8')

    def client(tag, mode='ok', timeout=20, **env):
        made = ds.Client(command=[sys.executable, '-I', str(script)], timeout=timeout, env={
            'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9',
            'LIVE': str(tmp_path / 'live'), 'LOG': str(log), 'RELEASE': str(tmp_path / 'release'),
            'TAG': tag, 'MODE': mode, **env})
        clients.append(made)
        return made

    def events():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def release():
        (tmp_path / 'release').touch()

    yield type('Natives', (), {'limit': staticmethod(limit), 'client': staticmethod(client),
                               'events': staticmethod(events), 'release': staticmethod(release)})
    release()
    for made in clients:
        made.close()


def query():
    return dict(model='sonnet', messages=[{'role': 'user', 'content': 'go'}])


def run_all(calls):
    """Start every call at once on its own thread; return {tag: response or exception}."""
    results, threads = {}, []
    for tag, call in calls.items():
        def target(tag=tag, call=call):
            try:
                results[tag] = call()
            except Exception as error:
                results[tag] = error
        threads.append(threading.Thread(target=target))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


def test_calls_over_the_limit_wait_and_all_finish(natives):
    natives.limit(2)
    clients = {f'c{i}': natives.client(f'c{i}') for i in range(5)}
    results = run_all({tag: (lambda c=c: c.create(**query())) for tag, c in clients.items()})
    assert all(not isinstance(r, Exception) and r.choices[0].message.content == 'ok' for r in results.values()), results
    starts = [e for e in natives.events() if e['event'] == 'start']
    assert len(starts) == 5
    assert max(e['live'] for e in starts) == 2, 'more Claude Code processes ran at once than max_concurrent'


def test_cancelled_waiting_call_returns_without_spawning(natives):
    natives.limit(1)
    holder, waiter = natives.client('holder', mode='hang'), natives.client('waiter')
    held = threading.Thread(target=lambda: holder.create(**query()))
    held.start()
    deadline = time.monotonic() + 10
    while not natives.events() and time.monotonic() < deadline:
        time.sleep(.01)
    outcome = {}

    def wait():
        try:
            waiter.create(**query())
        except Exception as error:
            outcome['error'], outcome['at'] = error, time.monotonic()
    queued = threading.Thread(target=wait)
    queued.start()
    time.sleep(.5)
    assert [e['tag'] for e in natives.events() if e['event'] == 'start'] == ['holder'], 'the call over the limit started a process'
    cancelled_at = time.monotonic()
    waiter.cancel()
    queued.join(5)
    assert str(outcome.get('error')) == 'Claude request cancelled' and outcome['at'] - cancelled_at < 1
    natives.release()
    held.join(10)
    assert [e['tag'] for e in natives.events() if e['event'] == 'start'] == ['holder']


def test_a_crashed_native_frees_its_slot_for_the_next_call(natives):
    natives.limit(1)
    crashing, queued = natives.client('crash', mode='crash'), natives.client('next', timeout=5)
    results = run_all({'crash': lambda: crashing.create(**query()),
                       'next': lambda: (time.sleep(.1), queued.create(**query()))[1]})
    assert isinstance(results['crash'], RuntimeError) and 'Claude request cancelled' not in str(results['crash'])
    assert results['next'].choices[0].message.content == 'ok'
    by_tag = {}
    for e in natives.events():
        by_tag.setdefault(e['pid'], []).append(e)
    crash_end = next(e['t'] for evs in by_tag.values() if evs[0]['tag'] == 'crash' for e in evs if e['event'] == 'end')
    next_start = next(evs[0]['t'] for evs in by_tag.values() if evs[0]['tag'] == 'next')
    assert next_start >= crash_end, 'the next call started while the crashing native still held the only slot'
