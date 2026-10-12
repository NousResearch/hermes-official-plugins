"""Warm mode: one live native session across Hermes steps; parked hermes tool calls; rebuild on divergence."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

# A native that speaks the warm protocol: SDK MCP over control requests, one tool call at a time,
# a new upstream request after every answered batch, and an idle wait for the next user frame.
NATIVE = r'''
import json, os, sys, urllib.request
with open(os.environ['SPAWNS'], 'a') as f:
    f.write(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:]}) + '\n')
history, queue, rpc, answered = [], [], [0], {}
def out(row):
    print(json.dumps(row), flush=True)
def call_next():
    block = queue[0]
    rpc[0] += 1
    out({'type':'control_request','request_id':'c%d' % rpc[0],'request':{'subtype':'mcp_message','server_name':'hermes','message':{'jsonrpc':'2.0','id':rpc[0],'method':'tools/call','params':{'name':block['name'][len('mcp__hermes__'):],'arguments':block['input'],'_meta':{'claudecode/toolUseId':block['id']}}}}})
def wire():
    # Real native merges consecutive user messages; NATIVE_CONTEXT adds its per-request context the way 2.1.287+
    # does: a reminder before the first turn's frame or after (inside the last tool result of) a later one, a mark
    # on the last assistant block and a marked trailing system message the next request never replays.
    messages = []
    for message in json.loads(json.dumps(history)):
        if messages and message['role'] == messages[-1]['role'] == 'user':
            messages[-1]['content'] += message['content']
        else:
            messages.append(message)
    if not os.environ.get('NATIVE_CONTEXT'):
        return messages
    reminder, newest = '\n<system-reminder>userEmail: user@example.com</system-reminder>', messages[-1]['content']
    if len(messages) == 1:
        newest.insert(0, {'type':'text','text':reminder})
    elif newest[-1]['type'] == 'tool_result':
        if isinstance(newest[-1]['content'], str):
            newest[-1]['content'] += reminder
        else:
            newest[-1]['content'].append({'type':'text','text':reminder})
    else:
        newest.append({'type':'text','text':reminder})
    if len(messages) > 1:
        messages[-2]['content'][-1]['cache_control'] = {'type':'ephemeral'}
    return messages + [{'role':'system','content':[{'type':'text','text':'Today is 2026-10-05.','cache_control':{'type':'ephemeral'}}]}]
def query():
    body = json.dumps({'messages': wire()}).encode()
    try:
        raw = urllib.request.urlopen(urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', data=body, headers={'Content-Type':'application/json'}), timeout=30).read().decode()
    except urllib.error.HTTPError as error:
        out({'type':'assistant','error':'rate_limit' if error.code == 429 else 'unknown','message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':'API Error: %d' % error.code}]}})
        out({'type':'result','subtype':'success','is_error':True,'num_turns':1})
        return
    message = None
    for frame in raw.split('\n\n'):
        if frame.startswith('data: '):
            event = json.loads(frame[6:])
            if event['type'] == 'message_start':
                message = event['message']
            elif event['type'] == 'content_block_start':
                message['content'].append(event['content_block'])
            elif event['type'] == 'message_delta':
                message.update(event['delta'])
    for block in message['content']:
        if block['type'] == 'text':
            out({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':block['text']}}})
        out({'type':'assistant','message':dict(message, content=[block])})
    out({'type':'stream_event','event':{'type':'message_stop'}})
    history.append({'role':'assistant','content':message['content']})
    tools = [b for b in message['content'] if b['type'] == 'tool_use'] if message.get('stop_reason') == 'tool_use' else []
    if tools:
        queue.extend(tools)
        call_next()
    else:
        out({'type':'result','subtype':'success','is_error':False,'num_turns':1,'usage':message['usage']})
for line in sys.stdin:
    row = json.loads(line)
    if row['type'] == 'control_request':
        out({'type':'control_response','response':{'subtype':'success','request_id':row['request_id'],'response':{}}})
    elif row['type'] == 'control_response':
        result = row['response']['response']['mcp_response']['result']
        block = queue.pop(0)
        history.append({'role':'user','content':[{'type':'tool_result','tool_use_id':block['id'],'content':result['content']}]})
        if queue:
            call_next()
        else:
            query()
    elif row['type'] in ('user', 'assistant'):
        history.append(row['message'])
        if row.get('shouldQuery') is False:
            out({'type':'result','num_turns':0,'is_error':False})
        elif row['type'] == 'user' and os.environ.get('AUTH_FAIL'):
            # Logged out: native answers itself before any upstream request.
            out({'type':'assistant','error':'authentication_failed','message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':'Not logged in'}]}})
            out({'type':'result','subtype':'success','is_error':True,'num_turns':1,'result':'Not logged in'})
        elif row['type'] == 'user' and os.environ.get('NATIVE_ERROR'):
            # A plan limit native answers itself, with no upstream request.
            out({'type':'assistant','error':os.environ['NATIVE_ERROR'],'message':{'role':'assistant','model':'<synthetic>','content':[{'type':'text','text':'limit reached'}]}})
            out({'type':'result','subtype':'success','is_error':True,'num_turns':1})
        elif row['type'] == 'user':
            query()
'''

USAGE = {'input_tokens': 1, 'output_tokens': 2, 'cache_read_input_tokens': 3, 'cache_creation_input_tokens': 4}


def sse(message_id, blocks, stop, details=None):
    events = [{'type': 'message_start', 'message': {'id': message_id, 'role': 'assistant', 'model': 'sonnet', 'content': [], 'usage': dict(USAGE)}}]
    for index, block in enumerate(blocks):
        if block['type'] == 'tool_use':
            events += [{'type': 'content_block_start', 'index': index, 'content_block': dict(block, input={})},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}}]
        else:
            events += [{'type': 'content_block_start', 'index': index, 'content_block': {'type': 'text', 'text': ''}},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': block['text']}}]
        events.append({'type': 'content_block_stop', 'index': index})
    events += [{'type': 'message_delta', 'delta': {'stop_reason': stop, **({'stop_details': details} if details else {})}, 'usage': dict(USAGE)}, {'type': 'message_stop'}]
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()


class Upstream:
    """Answers the newest native message: a user text asks for two parallel probe calls, tool results get echoed."""
    def __init__(self, hang=None):
        self.bodies, self.hang = [], hang or threading.Event()
        outer = self
        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.bodies.append(body)
                if outer.hang.is_set():
                    time.sleep(30)
                    return
                # Native's trailing per-request context (NATIVE_CONTEXT) is not part of the conversation.
                messages = [m for m in body['messages'] if m['role'] != 'system']
                last = messages[-1]
                content = last['content'] if isinstance(last['content'], list) else [{'type': 'text', 'text': last['content']}]
                if 'slow' in content[0].get('text', ''):
                    time.sleep(1.5)
                results = [b for m in messages[-2:] for b in (m['content'] if isinstance(m['content'], list) else []) if b.get('type') == 'tool_result']
                n = len(outer.bodies)
                text = ''.join(b.get('text', '') for b in content if b.get('type') == 'text')
                if 'limit' in text:
                    error = json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'slow down'}}).encode()
                    self.send_response(429)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(error)))
                    self.end_headers()
                    self.wfile.write(error)
                    return
                if results and last['role'] == 'user' and content[0].get('type') == 'tool_result':
                    texts = [r['content'][0]['text'] if isinstance(r['content'], list) else r['content'] for r in results]
                    payload = sse(f'm{n}', [{'type': 'text', 'text': 'got ' + '+'.join(texts)}], 'end_turn')
                elif 'refuse' in text:
                    payload = sse(f'm{n}', [{'type': 'tool_use', 'id': f'toolu_{n}r', 'name': 'mcp__hermes__probe', 'input': {}}], 'refusal',
                                  {'category': 'cyber', 'explanation': 'The request was declined.'})
                elif 'ghost' in text or 'bare' in text:
                    name = 'mcp__hermes__ghost' if 'ghost' in text else 'probe'
                    payload = sse(f'm{n}', [{'type': 'tool_use', 'id': f'toolu_{n}g', 'name': name, 'input': {'key': 'g'}}], 'tool_use')
                elif 'parallel' in text:
                    payload = sse(f'm{n}', [{'type': 'tool_use', 'id': f'toolu_{n}a', 'name': 'mcp__hermes__probe', 'input': {'key': 'a'}},
                                            {'type': 'tool_use', 'id': f'toolu_{n}b', 'name': 'mcp__hermes__probe', 'input': {'key': 'b'}}], 'tool_use')
                else:
                    payload = sse(f'm{n}', [{'type': 'text', 'text': 'echo ' + content[0].get('text', '')}], 'end_turn')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


TOOLS = [{'type': 'function', 'function': {'name': 'probe', 'description': 'probe', 'parameters': {'type': 'object', 'properties': {'key': {'type': 'string'}}}}}]


@pytest.fixture
def warm(tmp_path):
    upstream = Upstream()
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    spawns = tmp_path / 'spawns.jsonl'
    env = {'PATH': os.defpath, 'HOME': str(tmp_path), 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{upstream.server.server_port}',
           'CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM': '1', 'SPAWNS': str(spawns)}
    clients = []
    def client():
        clients.append(directsdk.Client(command=[sys.executable, str(native)], env=env, timeout=20))
        return clients[-1]
    def spawned():
        return [json.loads(line) for line in spawns.read_text().splitlines()] if spawns.exists() else []
    yield SimpleFixture(client, spawned, upstream)
    for c in clients:
        c.close()
    directsdk._warm_shutdown()
    upstream.close()


class SimpleFixture:
    def __init__(self, client, spawned, upstream):
        self.client, self.spawned, self.upstream = client, spawned, upstream


def step(client, messages, stream=False):
    result = client.create(model='sonnet', messages=messages, tools=TOOLS, stream=stream)
    if stream:
        chunks = list(result)
        final = chunks[-1]
        return final._response
    return result


def as_history(response):
    message = response.choices[0].message.model_dump()
    message['content'] = (message.get('content') or '').strip()
    message.pop('reasoning_content', None)
    return message


def alive(pid):
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_one_native_serves_tool_rounds_and_turns_with_hermes_results(warm):
    messages = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    calls = first.choices[0].message.tool_calls
    assert [json.loads(c.function.arguments)['key'] for c in calls] == ['a', 'b']
    assert first.usage.model_dump()['native_admission']['warm'] == 'cold'
    # Native already asked the hermes MCP server for the first call; it stays parked while Hermes runs tools.
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'BANANA'},
                 {'role': 'tool', 'tool_call_id': calls[0].id, 'content': 'APPLE'}]
    # Hermes builds request-local clients: a fresh client must still find the session by history.
    second = step(warm.client(), messages, stream=True)
    assert second.choices[0].message.content == 'got APPLE+BANANA'
    assert second.usage.model_dump()['native_admission'] == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None, 'warm': 'reused'}
    messages += [as_history(second), {'role': 'user', 'content': 'next turn'}]
    third = step(warm.client(), messages)
    assert third.choices[0].message.content == 'echo next turn'
    assert third.usage.model_dump()['native_admission']['warm'] == 'reused'
    spawned = warm.spawned()
    assert len(spawned) == 1 and len(warm.upstream.bodies) == 3
    argv = spawned[0]['argv']
    assert '--max-turns' not in argv and argv[argv.index('--allowedTools') + 1] == 'mcp__hermes'
    assert json.loads(argv[argv.index('--mcp-config') + 1]) == {'mcpServers': {'hermes': {'type': 'sdk', 'name': 'hermes'}}}
    # The carrier is the baseline one, so a later rebuild (or a cold client) replays it unchanged.
    assert second.choices[0].message.reasoning_details[0]['type'] == directsdk.CARRIER


def test_divergence_kills_the_session_and_replays_full_history(warm):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    calls = first.choices[0].message.tool_calls
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': f'R{i}' * 50} for i, c in enumerate(calls)]
    old = warm.spawned()[0]['pid']
    second = step(warm.client(), messages)
    messages += [as_history(second)]
    # Host compaction rewrites a tool result: the warm session no longer prefixes the request.
    messages[2] = dict(messages[2], content='[compacted] R0')
    messages.append({'role': 'user', 'content': 'after compaction'})
    third = step(warm.client(), messages)
    assert third.usage.model_dump()['native_admission']['warm'] == 'rebuild:prefix'
    assert third.choices[0].message.content == 'echo after compaction'
    spawned = warm.spawned()
    assert len(spawned) == 2 and not alive(old)
    # The rebuild replayed the full (edited) history: every tool result's text, not an excerpt.
    replayed = warm.upstream.bodies[-1]['messages']
    results = [b for m in replayed if isinstance(m['content'], list) for b in m['content'] if b.get('type') == 'tool_result']
    assert [r['content'] for r in results] == ['[compacted] R0', 'R1' * 50]
    # The rebuilt session is warm again for the next step.
    messages += [as_history(third), {'role': 'user', 'content': 'again'}]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'reused'
    assert len(warm.spawned()) == 2


def test_unexpected_suffix_rebuilds(warm):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    calls = first.choices[0].message.tool_calls
    # An interrupted step: Hermes answers one call only and appends a user row.
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': calls[0].id, 'content': 'A'},
                 {'role': 'tool', 'tool_call_id': calls[1].id, 'content': 'B'}, {'role': 'user', 'content': 'steer'}]
    result = step(warm.client(), messages)
    assert result.usage.model_dump()['native_admission']['warm'] == 'rebuild:suffix'
    assert len(warm.spawned()) == 2


def test_cancel_kills_the_warm_session(warm):
    warm.upstream.hang.set()
    client = warm.client()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(step, client, [{'role': 'user', 'content': 'call probe in parallel'}])
        deadline = time.monotonic() + 10
        while not warm.upstream.bodies and time.monotonic() < deadline:
            time.sleep(.05)
        client.cancel()
        with pytest.raises(RuntimeError, match='cancelled'):
            future.result(timeout=10)
    pid = warm.spawned()[0]['pid']
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._warm_pool


def test_tool_free_calls_and_unflagged_clients_stay_per_call(warm, tmp_path):
    client = warm.client()
    # Auxiliary one-shots carry no tools: they never start a warm session.
    result = client.create(model='sonnet', messages=[{'role': 'user', 'content': 'title please'}])
    assert result.choices[0].message.content == 'echo title please'
    assert result.usage.model_dump()['native_admission'] == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None}
    assert '--max-turns' in warm.spawned()[0]['argv']
    assert not directsdk._warm_pool


def test_shutdown_leaves_no_native_behind(warm):
    step(warm.client(), [{'role': 'user', 'content': 'call probe in parallel'}])
    pid = warm.spawned()[0]['pid']
    assert alive(pid) and len(directsdk._warm_pool) == 1
    directsdk._warm_shutdown()
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._warm_pool


def test_interleaved_conversations_keep_separate_natives(warm):
    left = [{'role': 'user', 'content': 'call probe in parallel'}]
    right = [{'role': 'user', 'content': 'call probe in parallel, please'}]
    for messages in (left, right):
        first = step(warm.client(), messages)
        messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': c.id[-2:] + '-' + messages[0]['content'][-6:]}
                                           for c in first.choices[0].message.tool_calls]
    answers = [step(warm.client(), messages) for messages in (right, left)]
    assert [a.usage.model_dump()['native_admission']['warm'] for a in answers] == ['reused', 'reused']
    assert answers[0].choices[0].message.content == 'got 2a-please+2b-please'
    assert answers[1].choices[0].message.content == 'got 1a-rallel+1b-rallel'
    assert len(warm.spawned()) == 2 and len(directsdk._warm_pool) == 2



# ---- Codex review (v2): each test reproduces one finding at 4f8016d ----

LAUNCHER = 'import subprocess, sys\nsys.exit(subprocess.call([sys.executable] + sys.argv[1:]))\n'


@pytest.mark.parametrize('launcher', [False, True])
def test_r2_identical_visible_histories_never_share_a_native(warm, tmp_path, launcher):
    """R2: two conversations with the same visible text differ only in native message identity."""
    def client():
        c = warm.client()
        if launcher:
            shim = tmp_path / 'launcher.py'
            shim.write_text(LAUNCHER)
            c.command = [sys.executable, str(shim)] + c.command[1:]
        return c
    a = [{'role': 'user', 'content': 'hi'}]
    b = [{'role': 'user', 'content': 'hi'}]
    sessions = []
    for messages in (a, b):
        messages.append(as_history(step(client(), messages)))
        sessions += [s for s in directsdk._warm_pool if s not in sessions]
    assert a[1]['content'] == b[1]['content'] == 'echo hi'
    # One identity throughout: the pool's own sessions and Popen pids. Behind a launcher (a Windows venv python)
    # the pid native reports is not the one Popen holds.
    session_a, session_b = sessions
    pids = session_a.pid, session_b.pid
    b.append({'role': 'user', 'content': 'b turn two'})
    assert step(client(), b).choices[0].message.content == 'echo b turn two'
    # B's own native continued; A's native was neither used nor killed; nothing replaced either.
    assert len(directsdk._warm_pool) == 2 and set(directsdk._warm_pool) == {session_a, session_b}
    assert (session_a.pid, session_b.pid) == pids and session_a.alive() and session_b.alive() and len(warm.spawned()) == 2
    assert len(session_b.consumed) == 4 and len(session_a.consumed) == 2


def test_r3_changed_native_carrier_forces_rebuild(warm):
    """R3: same visible text, different (here: removed) native carrier must not continue the old native state."""
    messages = [{'role': 'user', 'content': 'hi'}]
    messages.append(as_history(step(warm.client(), messages)))
    messages[1] = {k: v for k, v in messages[1].items() if k != 'reasoning_details'}
    messages.append({'role': 'user', 'content': 'again'})
    result = step(warm.client(), messages)
    assert result.usage.model_dump()['native_admission']['warm'] != 'reused'
    assert len(warm.spawned()) == 2


def test_r5_cancel_after_tool_calls_kills_the_parked_native(warm):
    """R5: create() returned tool calls, Hermes is running tools, the user interrupts: client.cancel()."""
    client = warm.client()
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    pid = warm.spawned()[0]['pid']
    client.cancel()
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._warm_pool
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'cold'


def test_r9_pool_cap_holds_with_busy_sessions(warm, monkeypatch):
    """R9: with every warm slot busy, an extra conversation takes the per-call path; nothing exceeds the cap."""
    monkeypatch.setattr(directsdk, 'WARM_POOL_SIZE', 1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        slow = pool.submit(step, warm.client(), [{'role': 'user', 'content': 'slow hi'}])
        deadline = time.monotonic() + 10
        while not warm.upstream.bodies and time.monotonic() < deadline:
            time.sleep(.02)
        other = step(warm.client(), [{'role': 'user', 'content': 'hi'}])
        assert slow.result(timeout=20).choices[0].message.content == 'echo slow hi'
    assert other.choices[0].message.content == 'echo hi'
    assert 'warm' not in other.usage.model_dump()['native_admission']
    assert '--max-turns' in warm.spawned()[1]['argv']
    assert len(directsdk._warm_pool) <= 1


def test_rauth_launch_identity_is_part_of_reuse(warm, tmp_path):
    """R-auth: a different native config dir (or command) with identical history must not reuse the old child."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    other = warm.client()
    other.env['CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR'] = str(tmp_path / 'other-config')
    assert step(other, messages).usage.model_dump()['native_admission']['warm'] != 'reused'
    assert len(warm.spawned()) == 2


def test_rauth_conflicting_override_is_refused_on_reuse(warm, monkeypatch):
    """R-auth: the inherited-environment refusal runs on every warm step, not only at spawn."""
    url = f'http://127.0.0.1:{warm.upstream.server.server_port}'
    class Local(directsdk.Admission):
        def __init__(self, upstream, timeout, queried=None):
            super().__init__(url, timeout, queried)
    monkeypatch.setattr(directsdk, 'Admission', Local)
    fixture = warm.client()
    for key, value in fixture.env.items():
        if key != 'ANTHROPIC_BASE_URL':
            monkeypatch.setenv(key, value)
    monkeypatch.delenv('ANTHROPIC_BASE_URL', raising=False)
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_FOUNDRY_API_KEY', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        monkeypatch.delenv(key, raising=False)
    client = directsdk.Client(command=fixture.command, timeout=20)
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-not-a-key')
    with pytest.raises(ValueError, match='ANTHROPIC_API_KEY'):
        step(client, messages)
    client.close()


def test_logged_out_warm_native_raises_the_login_hint(warm):
    """NON_BLOCKING: warm keeps baseline's ClaudeCodeLoggedOut type and hint."""
    from directsdk_setup import LOGGED_OUT_HINT
    client = warm.client()
    client.env['AUTH_FAIL'] = '1'
    with pytest.raises(directsdk.ClaudeCodeLoggedOut, match='Not logged in'):
        step(client, [{'role': 'user', 'content': 'hi'}])
    assert LOGGED_OUT_HINT


# ---- Codex re-review (v3) ----

def test_r5_cancel_during_handoff_is_not_published(warm, monkeypatch):
    """R5 race: the step detached its process, then cancel() arrives before release publishes the session."""
    client = warm.client()
    release = directsdk._warm_release
    def racing(*args):
        client.cancel()  # lands while the session is still busy and no process is attached
        return release(*args)
    monkeypatch.setattr(directsdk, '_warm_release', racing)
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages)
    monkeypatch.setattr(directsdk, '_warm_release', release)
    pid = warm.spawned()[0]['pid']
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(pid) and not directsdk._warm_pool
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'cold'


def test_routine_close_after_a_step_keeps_the_session(warm):
    client = warm.client()
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(client, messages, stream=True)  # the stream is drained and closed by step()
    client.close()
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'reused'


def test_reaper_start_failure_leaks_no_reservation(warm, monkeypatch):
    """A failing reaper Thread.start must not leave a busy reservation or a dead reaper object behind."""
    import threading
    monkeypatch.setattr(directsdk, 'WARM_POOL_SIZE', 1)
    monkeypatch.setattr(directsdk, '_warm_reaper', None)
    real = threading.Thread
    class Failing(real):
        def start(self):
            if getattr(self, '_target', None) is directsdk._warm_reap:
                raise RuntimeError('injected: cannot start thread')
            return super().start()
    monkeypatch.setattr(threading, 'Thread', Failing)
    result = step(warm.client(), [{'role': 'user', 'content': 'hi'}])
    assert result.choices[0].message.content == 'echo hi'
    assert not directsdk._warm_pool and directsdk._warm_reaper is None
    monkeypatch.setattr(threading, 'Thread', real)
    again = step(warm.client(), [{'role': 'user', 'content': 'hi again'}])
    assert again.usage.model_dump()['native_admission']['warm'] == 'cold'
    assert len(directsdk._warm_pool) == 1 and directsdk._warm_reaper.is_alive()


def test_reuse_key_ignores_volatile_env_but_not_config_dir(warm, tmp_path):
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = warm.client()
    first.env['CLAUDE_PID'] = '111'
    one = step(first, messages)
    messages += [as_history(one)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in one.choices[0].message.tool_calls]
    second = warm.client()
    second.env['CLAUDE_PID'] = '222'
    two = step(second, messages)
    assert two.usage.model_dump()['native_admission']['warm'] == 'reused'
    messages += [as_history(two), {'role': 'user', 'content': 'next'}]
    third = warm.client()
    third.env.update(CLAUDE_PID='333', CLAUDE_CONFIG_DIR=str(tmp_path / 'elsewhere'))
    assert step(third, messages).usage.model_dump()['native_admission']['warm'] != 'reused'
    assert len(warm.spawned()) == 2


# ---- v7: interplay with upstream main since ef73726 ----

@pytest.mark.parametrize('stream', [False, True])
def test_refusal_after_a_tool_call_is_content_filter_and_never_reused(warm, stream):
    """4c4cfbb/065edc7/31b591f: the warm step answers a refusal as baseline does, and the session is not kept."""
    messages = [{'role': 'user', 'content': 'please refuse'}]
    client = warm.client()
    if stream:
        chunks = list(client.create(model='sonnet', messages=messages, tools=TOOLS, stream=True))
        last = chunks[-1].choices[0]
        assert last.finish_reason == 'content_filter' and last.delta.refusal == 'The request was declined.'
        assert not any(c.choices[0].delta.tool_calls for c in chunks)
        response = chunks[-1]._response
    else:
        response = step(client, messages)
    message = response.choices[0].message
    assert response.choices[0].finish_reason == 'content_filter' and message.refusal == 'The request was declined.'
    assert not message.tool_calls
    # The carrier keeps native's turn as sent, cut-off call included.
    assert [b['type'] for b in message.reasoning_details[0]['messages'][0]['content']] == ['tool_use']
    assert not directsdk._warm_pool
    messages += [as_history(response), {'role': 'user', 'content': 'try again'}]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'cold'


def test_failures_carry_the_status_hermes_routes_on(warm):
    """144bd1d (#58): a relayed upstream status and a native-answered limit both reach Hermes as status_code."""
    with pytest.raises(directsdk.ClaudeAPIError, match='Incomplete upstream response') as upstream:
        step(warm.client(), [{'role': 'user', 'content': 'hit the limit'}])
    assert upstream.value.status_code == 429
    client = warm.client()
    client.env['NATIVE_ERROR'] = 'rate_limit'
    with pytest.raises(directsdk.ClaudeAPIError, match='limit reached') as native:
        step(client, [{'role': 'user', 'content': 'hi'}])
    assert native.value.status_code == 429
    assert not directsdk._warm_pool


@pytest.mark.parametrize('trigger, offered', [('ghost', 'mcp__hermes__ghost'), ('bare', 'mcp__hermes__probe')])
def test_unparked_tool_names_go_to_hermes_and_rebuild(warm, trigger, offered):
    """4336364 (#39) / 218d5e4 (#62): Hermes gets the call; native never parked it, so the next step replays."""
    messages = [{'role': 'user', 'content': 'call ' + trigger}]
    first = step(warm.client(), messages)
    (call,) = first.choices[0].message.tool_calls
    assert first.choices[0].finish_reason == 'tool_calls' and call.function.name == offered[len(directsdk.PREFIX):]
    assert first.choices[0].message.reasoning_details[0]['messages'][0]['content'][0]['name'] == offered
    assert not directsdk._warm_pool
    messages += [as_history(first), {'role': 'tool', 'tool_call_id': call.id, 'content': 'unknown tool'}]
    second = step(warm.client(), messages)
    assert second.usage.model_dump()['native_admission']['warm'] == 'cold'
    assert second.choices[0].message.content == 'got unknown tool'


def test_traffic_policy_and_proxy_are_part_of_launch_identity(warm, monkeypatch):
    """72249fa / 7d2ae80: a session launched under one telemetry setting or proxy route is not reused under another."""
    import directsdk_setup
    monkeypatch.setattr(directsdk_setup, 'telemetry_enabled', lambda: True)
    messages = [{'role': 'user', 'content': 'hi'}]
    messages.append(as_history(step(warm.client(), messages)))
    messages.append({'role': 'user', 'content': 'same policy'})
    messages.append(as_history(step(warm.client(), messages)))
    assert len(warm.spawned()) == 1
    monkeypatch.setattr(directsdk_setup, 'telemetry_enabled', lambda: False)
    messages.append({'role': 'user', 'content': 'telemetry off'})
    response = step(warm.client(), messages)
    assert response.usage.model_dump()['native_admission']['warm'] != 'reused' and len(warm.spawned()) == 2
    messages += [as_history(response), {'role': 'user', 'content': 'via proxy'}]
    proxied = warm.client()
    proxied.env['HTTPS_PROXY'] = 'http://127.0.0.1:9'  # the loopback http upstream is never proxied
    response = step(proxied, messages)
    assert response.usage.model_dump()['native_admission']['warm'] != 'reused'
    assert len(warm.spawned()) == 3
    # A user-exported DO_NOT_TRACK reaches the child unchanged (apply_traffic_policy never removes it): a change starts a new native.
    messages += [as_history(response), {'role': 'user', 'content': 'do not track'}]
    untracked = warm.client()
    untracked.env.update(HTTPS_PROXY='http://127.0.0.1:9', DO_NOT_TRACK='1')
    assert step(untracked, messages).usage.model_dump()['native_admission']['warm'] != 'reused'
    assert len(warm.spawned()) == 4


def test_each_step_runs_under_its_own_timeout_and_trace_header(warm):
    """67a6b05 / 522b25e (#85): rearm takes the step's timeout; a per-step traceparent never forces a rebuild."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = warm.client().create(model='sonnet', messages=messages, tools=TOOLS, timeout=20,
                                 extra_headers={'traceparent': '00-' + '1' * 32 + '-' + '1' * 16 + '-01'})
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    second = warm.client().create(model='sonnet', messages=messages, tools=TOOLS, timeout=17,
                                  extra_headers={'traceparent': '00-' + '2' * 32 + '-' + '2' * 16 + '-01'})
    assert second.usage.model_dump()['native_admission']['warm'] == 'reused'
    (session,) = directsdk._warm_pool
    assert session.admission.timeout == 17


def test_killing_a_warm_session_leaves_the_shared_cwd(warm):
    """f57ddbb/8a8338b: the cwd is shared across clients; a warm kill removes only its own per-session root."""
    client = warm.client()
    step(client, [{'role': 'user', 'content': 'call probe in parallel'}])
    (session,) = directsdk._warm_pool
    root, cwd = session.root, Path(client._workdir())
    assert root.name.startswith('claude-directsdk-warm-') and root != cwd
    directsdk._warm_shutdown()
    assert not root.exists() and cwd.is_dir()


REMINDER = '\n<system-reminder>userEmail: user@example.com</system-reminder>'
MARK = {'type': 'ephemeral'}


def context_client(warm):
    client = warm.client()
    client.env['NATIVE_CONTEXT'] = '1'
    return client


def message_marks(body):
    return [(i, j) for i, m in enumerate(body['messages']) for j, b in enumerate(m['content']) if 'cache_control' in b]


def prefix(body, at):
    """The messages through block ``at`` as the cache reads them; markers are not prompt content."""
    def plain(value):
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items() if k != 'cache_control'}
        return [plain(v) for v in value] if isinstance(value, list) else value
    i, j = at
    return json.dumps(plain(body['messages'][:i] + [dict(body['messages'][i], content=body['messages'][i]['content'][:j + 1])]))


def test_warm_steps_get_mains_restoration_and_breakpoint_pinning(warm):
    """456fdb0/fda48c3/f8630df: every warm request passes through main's admission rewrites, anchored on the frame
    native replays next (Hermes' frame on a launch or user turn, native's own MCP result shape on a tool round)."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(context_client(warm), messages)
    calls = first.choices[0].message.tool_calls
    # #77: the opening frame goes ahead of native's prepended reminder, so the next request replays it.
    assert [b['text'] for b in warm.upstream.bodies[0]['messages'][0]['content']] == ['call probe in parallel', REMINDER]
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': k} for c, k in zip(calls, ('APPLE', 'BANANA'))]
    second = step(context_client(warm), messages)
    assert second.usage.model_dump()['native_admission']['warm'] == 'reused'
    tool_round = warm.upstream.bodies[1]
    # Native's reminder is not forwarded as tool output; the trailing context mark moves onto the last result (#33).
    assert tool_round['messages'][2]['content'] == [
        {'type': 'tool_result', 'tool_use_id': calls[0].id, 'content': [{'type': 'text', 'text': 'APPLE'}]},
        {'type': 'tool_result', 'tool_use_id': calls[1].id, 'content': [{'type': 'text', 'text': 'BANANA'}], 'cache_control': MARK}]
    assert message_marks(tool_round) == [(1, 1), (2, 1)]
    messages += [as_history(second), {'role': 'user', 'content': 'next turn'}]
    third = step(context_client(warm), messages)
    assert third.usage.model_dump()['native_admission']['warm'] == 'reused' and len(warm.spawned()) == 1
    turn = warm.upstream.bodies[2]
    assert message_marks(turn) == [(3, 0), (4, 0)]
    # Each request replays the previous one's prefix through its last breakpoint, so that entry is read.
    for before, after in ((warm.upstream.bodies[0], tool_round), (tool_round, turn)):
        mark = message_marks(before)[-1] if message_marks(before) else (0, 0)
        assert prefix(after, mark) == prefix(before, mark)


def test_a_cold_launch_on_a_tool_result_frame_restores_hermes_results(warm):
    """456fdb0/85dacbf: a warm launch replaying Hermes' tool results forwards them without native's reminder."""
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(context_client(warm), messages)
    calls = first.choices[0].message.tool_calls
    directsdk._warm_shutdown()  # reaped meanwhile: the next step launches on the full history
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': k} for c, k in zip(calls, ('APPLE', 'BANANA'))]
    second = step(context_client(warm), messages)
    assert second.usage.model_dump()['native_admission']['warm'] == 'cold' and len(warm.spawned()) == 2
    assert warm.upstream.bodies[-1]['messages'][2]['content'] == [
        {'type': 'tool_result', 'tool_use_id': calls[0].id, 'content': 'APPLE'},
        {'type': 'tool_result', 'tool_use_id': calls[1].id, 'content': 'BANANA', 'cache_control': MARK}]


# ---- v8: review of PR #99 ----

# A native that refuses the warm launch before any upstream request; its per-call launch is the usual fake.
NO_WARM = "import sys\nif '--allowedTools' in sys.argv: print('fixture: warm protocol unavailable', flush=True); sys.exit(0)\n" + NATIVE


def no_warm_client(warm, tmp_path):
    native = tmp_path / 'no_warm.py'
    native.write_text(NO_WARM)
    client = warm.client()
    client.command = [sys.executable, str(native)]
    return client


@pytest.mark.parametrize('stream', [False, True])
def test_warm_protocol_failure_before_admission_falls_back_per_call(warm, tmp_path, stream):
    """A warm launch that dies before admission serves this step per-call: one upstream request, no kept session."""
    response = step(no_warm_client(warm, tmp_path), [{'role': 'user', 'content': 'hello'}], stream=stream)
    assert response.choices[0].message.content == 'echo hello'
    assert response.usage.model_dump()['native_admission'] == {'upstream_requests': 1, 'blocked_requests': 0, 'request_id': None}
    assert len(warm.upstream.bodies) == 1 and not directsdk._warm_pool
    assert [('--max-turns' in s['argv']) for s in warm.spawned()] == [True]
    # A healthy warm native beside it still reuses its process.
    messages = [{'role': 'user', 'content': 'call probe in parallel'}]
    first = step(warm.client(), messages)
    messages += [as_history(first)] + [{'role': 'tool', 'tool_call_id': c.id, 'content': 'X'} for c in first.choices[0].message.tool_calls]
    assert step(warm.client(), messages).usage.model_dump()['native_admission']['warm'] == 'reused'
    assert len(warm.spawned()) == 2 and len(directsdk._warm_pool) == 1


def test_cancelled_warm_failure_never_falls_back(warm, tmp_path, monkeypatch, caplog):
    """A cancel that lands while the warm launch fails ends the step: no per-call replay, no upstream request."""
    caplog.set_level('INFO', logger=directsdk.__name__)
    client = no_warm_client(warm, tmp_path)
    spawn = directsdk.Client._warm_spawn
    def cancelling(self, *args):
        try:
            spawn(self, *args)
        finally:
            self.cancel()
    monkeypatch.setattr(directsdk.Client, '_warm_spawn', cancelling)
    with pytest.raises(RuntimeError):
        step(client, [{'role': 'user', 'content': 'hello'}])
    assert not warm.upstream.bodies and not warm.spawned() and not directsdk._warm_pool
    assert 'warm fallback' not in caplog.text and 'warm kill' in caplog.text
