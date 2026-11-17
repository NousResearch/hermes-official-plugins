"""Any doubt about a kept process restarts it into per-call bytes: session mode against the real Claude Code CLI and
the scripted loopback upstream of ``test_directsdk_session_steer`` (no credentials, no inference).

The doubts are forced on what the real CLI emits: its own first resumed request with one row changed past the
opening turn, or one of its own output lines changed as the plugin reads it (a tool call without its id, with an id
the step never received or one already asked, a control request of an unknown kind, a line that is not an object).
Skipped without the CLI; ``CLAUDE_CODE_BIN`` picks a binary.
"""
import json
import threading
import time
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admission
import directsdk
from test_directsdk_session import MODEL, TOOLS, client, home, session_pids  # noqa: F401  (fixtures)
from test_directsdk_session_steer import ROUNDS, converse, per_call, upstream  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (os.environ.get('CLAUDE_CODE_BIN') or shutil.which('claude')), reason='needs the Claude Code CLI')


def restarts(caplog):
    return [r.getMessage() for r in caplog.records if 'session restart' in r.getMessage()]


def test_a_first_resumed_request_that_is_not_per_calls_is_refused_and_replayed(home, upstream, monkeypatch, caplog):
    """The kept process's first resumed request holds the opening turn, then its own record of the first reply and the
    results. Here its record of the reply differs from what a per-call replay sends (one tool argument changed in the
    relay, after the rows were lined up). The rows line up and the request extends the launch's, so only a comparison
    with per-call's own request for the same frames catches it: refused before upstream, replayed, kept after."""
    expected = per_call(home, upstream, monkeypatch, steer_at=None)
    canonical, drifted = admission.canonical_history, []

    def drift(payload, frames):
        out = canonical(payload, frames)
        body = json.loads(out)
        convo = [m for m in body['messages'] if m.get('role') != 'system']
        if len(convo) != 3 or drifted:
            return out
        drifted.append(True)
        convo[1]['content'][-1]['input']['path'] += ' (as native recalls it)'
        return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    monkeypatch.setattr(admission, 'canonical_history', drift)
    kept = client(home, upstream, monkeypatch, session=True)
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            pids = converse(kept, steer_at=None)
    finally:
        kept.close()
    assert drifted, 'the first resumed request never reached the relay'
    assert pids[1] != pids[0] and len(set(pids[1:])) == 1 and None not in pids, pids
    assert restarts(caplog) == ['Claude session restart (diverged): replaying into a new process']
    assert len(expected) == ROUNDS + 1 and upstream.bodies == expected  # the changed request never went upstream


FIRST, SECOND, THIRD = 'toolu_00_0', 'toolu_00_1', 'toolu_00_2'


def break_line(event, how):
    """The event native printed, as the plugin will read it after the forced change."""
    request = event.get('request') or {}
    meta = ((request.get('message') or {}).get('params') or {}).get('_meta') or {}
    if how == 'no_tool_id':
        del meta['claudecode/toolUseId']
    elif how in ('tool_id', 'duplicate'):
        meta['claudecode/toolUseId'] = FIRST if how == 'duplicate' else 'toolu_never_sent'
    elif how == 'control':
        request['subtype'] = 'a_new_subtype'
    else:
        return [event]
    return event


@pytest.mark.parametrize('how, which, why', [
    ('no_tool_id', FIRST, 'no_tool_id'),    # asked as the first reply streams: the reply stands, the process is not kept
    ('no_tool_id', SECOND, 'no_tool_id'),   # asked inside the next step, before its request: that step restarts
    ('tool_id', SECOND, 'tool_id'),         # an id the step never received
    ('duplicate', SECOND, 'tool_id'),       # an id native asked for before
    ('control', SECOND, 'control'),         # a control request the plugin does not know
    ('event', SECOND, 'event'),             # an output line that is not an object
])
def test_a_line_the_plugin_does_not_know_restarts_the_kept_process(home, upstream, monkeypatch, caplog, how, which, why):
    """Three calls in the first step; native asks for them one at a time, the first as soon as it has streamed, the
    others once Hermes brings the results. One of those asks is changed as the plugin reads it. The process is never answered
    or waited on again: the step restarts with the reason in the log, and every request is per-call's. Before, a call
    with no id or an id outside the step was parked and never answered (native waited forever), and an unknown
    control request was answered with an error while the process carried on."""
    upstream.calls = 3
    expected = per_call(home, upstream, monkeypatch, steer_at=None)
    loads, broken = json.loads, []

    def read(line, *args, **kwargs):
        event = loads(line, *args, **kwargs)
        if not broken and isinstance(event, dict) and event.get('type') == 'control_request' and \
                (((event.get('request') or {}).get('message') or {}).get('params') or {}).get('_meta', {}).get('claudecode/toolUseId') == which:
            broken.append(True)
            return break_line(event, how)
        return event
    monkeypatch.setattr(directsdk, 'json', SimpleNamespace(**{**{k: getattr(json, k) for k in dir(json) if not k.startswith('_')}, 'loads': read}))
    kept = client(home, upstream, monkeypatch, session=True)
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            pids = converse(kept, steer_at=None)
    finally:
        kept.close()
    assert broken, 'native never asked for the call'
    assert restarts(caplog) == [f'Claude session restart ({why}): replaying into a new process']
    if which == FIRST:
        # That step's answer stands without keeping its process; the next step keeps a new one.
        assert pids[0] is None and None not in pids[1:] and len(set(pids[1:])) == 1, pids
    else:
        # That step runs per-call; the next one keeps a new process.
        assert pids[1] is None and pids[2] not in (None, pids[0]) and len(set(pids[2:])) == 1, pids
    assert len(expected) == ROUNDS + 1 and upstream.bodies == expected


def test_a_doubt_while_a_new_process_replays_runs_that_step_per_call(home, upstream, monkeypatch, caplog):
    """A new process replays the history before its first request, and starts the hermes MCP server meanwhile. Its
    server's start is changed into a control request the plugin does not know: nothing has gone upstream, so the step
    runs on a fresh one-shot with per-call's bytes (before, the call failed)."""
    upstream.calls = 1
    one_shot = client(home, upstream, monkeypatch, session=False)
    try:
        messages = [{'role': 'system', 'content': 'You are a fixture agent.'}, {'role': 'user', 'content': 'go'}]
        reply = one_shot.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
        messages.append({k: v for k, v in reply.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None})
        messages.append({'role': 'tool', 'tool_call_id': reply['tool_calls'][0]['id'], 'content': 'contents'})
        one_shot.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000)
    finally:
        one_shot.close()
    expected, upstream.bodies[:] = upstream.bodies[-1], []
    loads, broken = json.loads, []

    def read(line, *args, **kwargs):
        event = loads(line, *args, **kwargs)
        if not broken and isinstance(event, dict) and event.get('type') == 'control_request' and \
                ((event.get('request') or {}).get('message') or {}).get('method') == 'initialize':
            broken.append(True)
            event['request']['subtype'] = 'a_new_subtype'
        return event
    monkeypatch.setattr(directsdk, 'json', SimpleNamespace(**{**{k: getattr(json, k) for k in dir(json) if not k.startswith('_')}, 'loads': read}))
    kept = client(home, upstream, monkeypatch, session=True)
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            assert kept.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.tool_calls
        assert broken and not session_pids(kept)
    finally:
        kept.close()
    assert restarts(caplog) == ['Claude session restart (control): replaying into a new process']
    assert upstream.bodies == [expected]


def test_a_per_call_request_that_cannot_be_built_restarts_the_kept_process(home, upstream, monkeypatch, caplog):
    """The one-shot that builds per-call's request for the first resumed step exits before its replay is acknowledged
    (forced once). Nothing proves the kept process's request, so it restarts (``unproven``); the new process is kept,
    and its own first resumed request is checked as usual."""
    expected = per_call(home, upstream, monkeypatch, steer_at=None)
    replay, failed = directsdk.Native.replay, []

    def flaky(self, request, frames, timeout):
        if self.tools is None and not failed:
            failed.append(True)
            raise RuntimeError('Native exited before replay acknowledgment (exit=1)')
        return replay(self, request, frames, timeout)
    monkeypatch.setattr(directsdk.Native, 'replay', flaky)
    kept = client(home, upstream, monkeypatch, session=True)
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            pids = converse(kept, steer_at=None)
    finally:
        kept.close()
    assert failed and pids[1] != pids[0] and len(set(pids[1:])) == 1 and None not in pids, pids
    assert restarts(caplog) == ['Claude session restart (unproven): replaying into a new process']
    assert len(expected) == ROUNDS + 1 and upstream.bodies == expected


class Filtered:
    """A kept process's stdout as the plugin reads it, through ``transform``."""

    def __init__(self, pipe, transform):
        self.pipe, self.transform = pipe, transform

    def __iter__(self):
        return iter(self.transform(self.pipe))

    def __getattr__(self, key):
        return getattr(self.pipe, key)


def filter_kept_stdout(monkeypatch, transform):
    spawn = directsdk.Request.spawn

    def wrapped(self, command, **kwargs):
        process = spawn(self, command, **kwargs)
        if '--allowedTools' in command:  # a kept process, not a one-shot
            process.stdout = Filtered(process.stdout, transform)
        return process
    monkeypatch.setattr(directsdk.Request, 'spawn', wrapped)


def waited(predicate, within=5):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline and not predicate():
        time.sleep(.01)
    return predicate()


def test_a_process_that_doubted_and_exited_as_the_step_handed_it_over_is_ended(home, upstream, monkeypatch, caplog):
    """The reader's doubt lands after the step decided to keep the process, the step logs it, and the process exits
    before the hand-over. The hand-over must still end it and give its place back, logging the reason once. Before,
    the hand-over saw the reason already logged and skipped the end too: the dead process stayed the session, with its
    place and its relay, and a parked call has no idle limit."""
    keep = directsdk.Client._keep

    def late(self, native, *args, **kwargs):
        native._doubt('event')  # still serving: queued for a step, which is past reading it
        native.end_doubted()  # what the step does with a doubt it sees after deciding to keep
        native.process.kill()
        native.process.wait(5)
        native.reader.join(5)
        return keep(self, native, *args, **kwargs)
    monkeypatch.setattr(directsdk.Client, '_keep', late)
    c = client(home, upstream, monkeypatch, session=True, cap=1)
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
            assert c._session is None and not directsdk._live, 'the exited process is still the kept session'
        assert restarts(caplog) == ['Claude session restart (event): replaying into a new process']
    finally:
        c.close()


@pytest.mark.parametrize('fault', ['no_tool_id', 'non_object', 'non_json', 'eof', 'partial_eof'])
def test_a_line_the_plugin_does_not_know_after_the_step_returned_ends_the_kept_process(home, upstream, monkeypatch, caplog, fault):
    """The bad line (or the end of native's output) arrives after the step has returned and the process holds its
    parked call, so no step reads the doubt. The process ends at once, with its reason in the log. Before, it stayed
    with its place and its relay, and no idle limit runs while a call is parked."""
    returned, loads = threading.Event(), json.loads

    def late(pipe):
        held = None
        for line in pipe:
            event = loads(line)
            message = (event.get('request') or {}).get('message') or {} if isinstance(event, dict) else {}
            if held is None and isinstance(event, dict) and event.get('type') == 'control_request' and message.get('method') == 'tools/call':
                held = event  # its first tools/call comes after the step returned, broken as the fault says
                continue
            yield line
            if held and isinstance(event, dict) and event.get('type') == 'stream_event' and event['event'].get('type') == 'message_stop':
                returned.wait(10)
                if fault == 'no_tool_id':
                    del held['request']['message']['params']['_meta']['claudecode/toolUseId']
                    yield json.dumps(held) + '\n'
                elif fault == 'non_object':
                    yield '[]\n'
                elif fault == 'non_json':
                    yield 'not json\n'
                elif fault == 'partial_eof':
                    yield '{"type":'
                    return
                if fault == 'eof':
                    return
                held = False
    filter_kept_stdout(monkeypatch, late)
    c = client(home, upstream, monkeypatch, session=True, cap=1)
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
            native = c._session
            assert native is not None
            returned.set()
            assert waited(lambda: c._session is None and native not in directsdk._live and native.process.poll() is not None), \
                dict(doubt=native.doubt, kept=c._session is native, alive=native.process.returncode is None)
        if fault != 'eof':
            assert restarts(caplog) == [f'Claude session restart ({"no_tool_id" if fault == "no_tool_id" else "event"}): replaying into a new process']
    finally:
        returned.set()
        c.close()


@pytest.mark.parametrize('fault', ['other_server', 'unknown_tool', 'missing_rpc_id'])
def test_a_tool_call_that_is_not_the_hermes_servers_is_never_parked(home, upstream, monkeypatch, caplog, fault):
    """The call carries an id the step did receive, but names another MCP server, a tool Hermes did not offer, or has
    no JSON-RPC id to answer under. It is doubted (``control``) and never parked; the step's answer stands and the
    next step runs on a new process. Before, it was parked; with no JSON-RPC id, the answer could never resolve it and
    the next step waited out its timeout."""
    changed, loads = [], json.loads

    def corrupt(pipe):
        for line in pipe:
            event = loads(line)
            request = event.get('request') or {} if isinstance(event, dict) else {}
            message = request.get('message') or {}
            if not changed and event.get('type') == 'control_request' and message.get('method') == 'tools/call':
                if fault == 'other_server':
                    request['server_name'] = 'some_other_server'
                elif fault == 'unknown_tool':
                    message['params']['name'] = 'tool_hermes_does_not_have'
                else:
                    del message['id']
                changed.append(True)
                line = json.dumps(event) + '\n'
            yield line
    filter_kept_stdout(monkeypatch, corrupt)
    c = client(home, upstream, monkeypatch, session=True)
    c.timeout = 10
    try:
        with caplog.at_level('DEBUG', logger=directsdk.logger.name):
            messages = [{'role': 'user', 'content': 'go'}]
            for _ in range(2):
                reply = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
                messages.append({k: v for k, v in reply.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None})
                messages += [{'role': 'tool', 'tool_call_id': t['id'], 'content': 'contents'} for t in reply['tool_calls']]
        assert changed
        assert restarts(caplog) == ['Claude session restart (control): replaying into a new process']
    finally:
        c.close()
