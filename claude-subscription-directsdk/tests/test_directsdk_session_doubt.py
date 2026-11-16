"""Any doubt about a kept process restarts it into per-call bytes: session mode against the real Claude Code CLI and
the scripted loopback upstream of ``test_directsdk_session_steer`` (no credentials, no inference).

The doubts are forced on what the real CLI emits: its own first resumed request with one row changed past the
opening turn, or one of its own output lines changed as the plugin reads it (a tool call without its id, with an id
the step never received or one already asked, a control request of an unknown kind, a line that is not an object).
Skipped without the CLI; ``CLAUDE_CODE_BIN`` picks a binary.
"""
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admission
import directsdk
from test_directsdk_session import client, home, session_pids  # noqa: F401  (fixtures)
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
