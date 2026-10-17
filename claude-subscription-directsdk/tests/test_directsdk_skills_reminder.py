"""Claude must be reminded to consult Hermes skills on each new user turn, without touching the cache.

Deep into a session, or after compaction, Claude stops consulting the skills index that sits at the
top of Hermes' system prompt. The provider restates the rule beside the queried user turn. It is
per-request context: Hermes history never contains it, and the cache breakpoint stays on host content.
"""
import json
import os
import sys

import directsdk
from admission import pin_message_breakpoint

NATIVE = r"""
import json, os, sys
if '--version' in sys.argv:
    print('2.1.263 (Claude Code)'); sys.exit()
rows = []
for line in sys.stdin:
    row = json.loads(line); rows.append(row)
    if row.get('shouldQuery') is False:
        print(json.dumps({'type': 'result', 'num_turns': 0, 'is_error': False}), flush=True)
open(os.environ['ROWS_CAPTURE'], 'w').write(json.dumps(rows))
print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'ok'}], 'id': 'msg', 'model': 'sonnet', 'stop_reason': 'end_turn'}}), flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
print(json.dumps({'type': 'result', 'num_turns': 1, 'subtype': 'success', 'is_error': False, 'usage': {'input_tokens': 1, 'output_tokens': 1}}), flush=True)
"""


def tool(name):
    return {'type': 'function', 'function': {'name': name, 'description': name, 'parameters': {'type': 'object', 'properties': {}}}}


def rows(tmp_path, messages, tools):
    script, capture = tmp_path / 'native.py', tmp_path / 'rows.json'
    script.write_text(NATIVE)
    client = directsdk.Client(command=[sys.executable, str(script)], env={'PATH': os.defpath, 'HOME': str(tmp_path), 'ROWS_CAPTURE': str(capture)})
    original = json.loads(json.dumps(messages))
    try:
        client.create(model='sonnet', messages=messages, tools=tools)
    finally:
        client.close()
    assert messages == original, 'caller history mutated'
    return json.loads(capture.read_text())


def sent(tmp_path, messages, tools):
    return rows(tmp_path, messages, tools)[-1]['message']['content']


def test_fresh_user_turn_carries_the_reminder_after_host_content(tmp_path):
    question = [{'role': 'user', 'content': 'earlier'}, {'role': 'assistant', 'content': 'reply'},
                {'role': 'user', 'content': 'open a PR for my branch'}]
    last = sent(tmp_path, question, [tool('terminal'), tool('skill_view')])
    assert last[0] == {'type': 'text', 'text': 'open a PR for my branch'}
    assert last[1:] == [{'type': 'text', 'text': directsdk.SKILLS_REMINDER}]

    # A tool round inside the turn, or an inventory without skills, is sent exactly as Hermes built it.
    round_ = question + [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 't1', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{}'}}]},
                         {'role': 'tool', 'tool_call_id': 't1', 'content': 'out'}]
    assert [b['type'] for b in sent(tmp_path, round_, [tool('terminal'), tool('skill_view')])] == ['tool_result']
    assert sent(tmp_path, question, [tool('terminal')]) == [{'type': 'text', 'text': 'open a PR for my branch'}]


def test_cache_breakpoint_stays_on_host_content_not_the_reminder():
    host = {'type': 'text', 'text': 'open a PR for my branch'}
    reminder = {'type': 'text', 'text': directsdk.SKILLS_REMINDER, 'cache_control': {'type': 'ephemeral'}}
    wire = {'model': 'm', 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'earlier'}]},
                                       {'role': 'assistant', 'content': [{'type': 'text', 'text': 'reply'}]},
                                       {'role': 'user', 'content': [host, reminder]}]}
    pinned = json.loads(pin_message_breakpoint(json.dumps(wire).encode(), [host]))
    newest = pinned['messages'][-1]['content']
    assert 'cache_control' in newest[0] and 'cache_control' not in newest[1]


def view(id_, name):
    return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': id_, 'type': 'function', 'function': {'name': 'skill_view', 'arguments': json.dumps({'name': name})}}]}


PRUNED = [{'role': 'user', 'content': 'load brand-voice'}, view('v1', 'creative/brand-voice'),
          {'role': 'tool', 'tool_call_id': 'v1', 'content': "[skill_view] name=creative/brand-voice (12,745 chars) "
           "[SKILL_PRUNED: content lost in compression; reload with skill_view(name='creative/brand-voice')]"},
          {'role': 'assistant', 'content': "[PRIOR CONTEXT: summary]\n## Pruned Skills\n"
           "[SKILL_PRUNED: content lost in compression; reload with skill_view(name='social-posting')]"}]
NAMED = ('<system-reminder>Before replying, scan the available skills in your instructions. If one matches or is even '
         'partially relevant to this message, load it with skill_view(name) first. Compaction removed the full text of '
         'these previously loaded skills: creative/brand-voice, social-posting. If any still applies to this '
         'conversation, reload it with skill_view(name) before replying.</system-reminder>')


def test_compaction_pruned_skills_are_named_until_reloaded(tmp_path):
    question = {'role': 'user', 'content': 'draft the X post'}
    tools = [tool('terminal'), tool('skill_view')]
    sent_rows = rows(tmp_path, PRUNED + [question], tools)
    assert sent_rows[-1]['message']['content'] == [{'type': 'text', 'text': 'draft the X post'}, {'type': 'text', 'text': NAMED}]
    # Only the queried frame carries it: every replayed frame is exactly what Hermes' history converts to.
    replayed = [row['message']['content'] for row in sent_rows[:-1]]
    assert replayed == [frame['message']['content'] for frame in directsdk.prepare_history(PRUNED + [question])[1][:-1]]

    reloaded = PRUNED + [view('v2', 'creative/brand-voice'),
                         {'role': 'tool', 'tool_call_id': 'v2', 'content': json.dumps({'success': True, 'name': 'brand-voice', 'content': 'body'})},
                         {'role': 'assistant', 'content': 'loaded'}, question]
    assert sent(tmp_path, reloaded, tools)[1]['text'] == NAMED.replace('creative/brand-voice, ', '')

    # The PR #89 gates still hold with markers in history.
    assert sent(tmp_path, PRUNED + [question], [tool('terminal')]) == [{'type': 'text', 'text': 'draft the X post'}]
    round_ = PRUNED + [question, view('v3', 'brand-voice'), {'role': 'tool', 'tool_call_id': 'v3', 'content': 'out'}]
    assert [b['type'] for b in sent(tmp_path, round_, tools)] == ['tool_result']


def test_reminder_text_without_and_beyond_the_name_cap():
    def reminder(body):
        frames = [{'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': body}]}}]
        return directsdk.skills_reminder(frames, {'skill_view'})['text']
    assert reminder('hi') == directsdk.SKILLS_REMINDER
    markers = ''.join(f"[SKILL_PRUNED: content lost in compression; reload with skill_view(name='s{i}')]" for i in range(10))
    assert 'skills: s2, s3, s4, s5, s6, s7, s8, s9, and 2 more. If any' in reminder(markers)
