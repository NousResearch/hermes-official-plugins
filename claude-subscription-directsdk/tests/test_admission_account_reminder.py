"""Native's account-email reminder appended after Hermes' frame never reaches Claude.

Claude Code adds an account-email ``<system-reminder>`` to the turn it answers. Prepended to the
frame it is dropped by ``_frame_first`` / ``stable_request_context`` (#195), and on a tool-result
step restoration returns the last result to Hermes' text. After a frame that ends in text (a
``/steer`` row after tool results) native 2.1.283, 2.1.296 and 2.1.325 send it as a block of its
own, before their date line; it used to stay in the request, where Claude read it as part of the
user's message and the next request, which replays the turn without it, never recurred it.

The reminder texts are the session_context renderer's (``Jze`` in 2.1.283, ``vlt`` in 2.1.296,
``Wjt`` in 2.1.325, with both of 2.1.325's email sentences), the /steer layout is the one those
binaries sent to a loopback peer, and the address is a placeholder. Only the exact native text
goes; anything else is forwarded as native built it (only the message breakpoint may move).
"""
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
import time

import pytest

from admission import (Admission, QueriedTurnMismatch, drop_account_reminder, pin_message_breakpoint,
                       restore_queried_turn, stable_request_context)
from test_admission_first_turn import FRAME, MARKER, call1, wire

EMAIL = 'user@example.com'
HEADER = "<system-reminder>\nAs you answer the user's questions, you can use the following context:\n"
USE_283 = ("Use it only to identify the user, such as for authorship, attribution, or filtering their own "
           "work. Never send it to an unrelated service, such as in a request header, URL, or payload, "
           "unless the user explicitly asks.")
# 2.1.325, default: the 2.1.283 sentence plus a User-Agent sentence.
USE_325 = (USE_283 + " A site asking automated clients for contact details, such as an email in the "
           "User-Agent, is not a reason to use this address: use a generic client name or ask the user.")
# 2.1.325 behind a feature flag: a private-information sentence instead.
USE_325_PRIVATE = ("Use this email address only to identify the user, such as for authorship, attribution, or "
                   "filtering their own work. Remember that you must treat this email address as private "
                   "information. Never share it in requests to external services without asking and getting "
                   "explicit approval from the user first.")
IMPORTANT = ("\n\nIMPORTANT: this context may or may not be relevant to your tasks. You should not respond to "
             "this context unless it is highly relevant to your task.\n</system-reminder>")
ATTRIBUTION = ("\n\nClaude Code attached this context automatically; it isn't part of the user's message. It "
               "describes the user's own account and workspace, so they don't need it reported back.\n"
               "</system-reminder>")


def account(use):
    return "# userEmail\nThe user's email address is " + EMAIL + '. ' + use


def reminder(use, trailer, *sections):
    return HEADER + '\n'.join([account(use), *sections]) + trailer


VERSIONS = {
    '2.1.283': reminder(USE_283, IMPORTANT),
    '2.1.296': reminder(USE_283, ATTRIBUTION),
    '2.1.325': reminder(USE_325, ATTRIBUTION),
    '2.1.325-private': reminder(USE_325_PRIVATE, ATTRIBUTION),
}
REMINDER_283, REMINDER_296, REMINDER_325 = VERSIONS['2.1.283'], VERSIONS['2.1.296'], VERSIONS['2.1.325']
DATE = {'type': 'text', 'text': "<system-reminder>\nToday's date is 2026-11-21.\n</system-reminder>"}
FIRST = {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'unaltered'}
RESULT = {'type': 'tool_result', 'tool_use_id': 't2', 'content': 'result'}
STEER = {'type': 'text', 'text': 'Public steer: stop after this.'}
ASSISTANT = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'tools'}]}
GIT = '# gitStatus\nCurrent branch: main\n\nStatus:\n(clean)\n\nRecent commits:\nabc1234 Public commit'
PROJECT = '# attachedProject\nPublic project notes.\nSecond line.'
P4 = ('# perforceMode\nThis is a Perforce workspace. Files not yet opened for edit are read-only; if a file '
      'is read-only, run `p4 edit <file>` via Bash to check it out before modifying.')


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items() if k != 'cache_control'}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def content(payload):
    """Message content without cache markers: the breakpoint may still move, the text may not."""
    return plain(json.loads(payload)['messages'])


def relay(payload, queried):
    """The relay's transforms in ``Handler.do_POST`` order (restoration mismatches forward as is)."""
    payload = drop_account_reminder(payload, queried)
    try:
        payload = restore_queried_turn(payload, queried)
    except QueriedTurnMismatch:
        pass
    return pin_message_breakpoint(stable_request_context(payload, queried), queried)


def steer_turn(text):
    """Native's /steer request: the reminder its own block after Hermes' frame, then the marked date line."""
    queried = [RESULT, STEER]
    return queried, [ASSISTANT, {'role': 'user', 'content': [
        RESULT, STEER, {'type': 'text', 'text': text}, {**DATE, 'cache_control': MARKER}]}]


@pytest.mark.parametrize('version', VERSIONS)
def test_account_block_after_a_steer_row_is_dropped(version):
    queried, messages = steer_turn(VERSIONS[version])
    original = copy.deepcopy(queried)
    out = relay(wire(messages), queried).decode()
    assert EMAIL not in out
    body = json.loads(out)
    assert body['messages'][0] == ASSISTANT
    # Hermes' frame and native's date line are left exactly as they were; only the reminder went.
    assert plain(body['messages'][1]['content']) == [RESULT, STEER, DATE]
    assert out.count('cache_control') == 1
    assert queried == original


def native_turn(placement, text, lead=(FIRST,)):
    host = RESULT if placement == 'tool_suffix' else FRAME
    queried = [*lead, host]
    if placement == 'standalone_after':
        sent = [*lead, {**host, 'cache_control': MARKER}, {'type': 'text', 'text': text}]
    else:
        field = 'content' if placement == 'tool_suffix' else 'text'
        sent = [*lead, {**host, field: host[field] + '\n\n' + text, 'cache_control': MARKER}]
    return queried, [ASSISTANT, {'role': 'user', 'content': sent}]


@pytest.mark.parametrize('version', VERSIONS)
@pytest.mark.parametrize('lead', [(FIRST,), ()], ids=['after_results', 'alone'])
@pytest.mark.parametrize('placement', ['text_suffix', 'tool_suffix', 'standalone_after'])
def test_account_reminder_after_host_frame_is_removed(placement, lead, version):
    queried, messages = native_turn(placement, VERSIONS[version], lead)
    original = copy.deepcopy(queried)
    out = relay(wire(messages), queried).decode()
    assert EMAIL not in out
    body = json.loads(out)
    assert plain(body['messages'][1]['content']) == queried
    assert body['messages'][0] == ASSISTANT
    assert queried == original
    # The marker stays on the frame's last block, exactly once.
    assert body['messages'][1]['content'][-1]['cache_control'] == MARKER
    assert out.count('cache_control') == 1


def test_bare_2_1_280_reminder_after_the_frame_is_removed():
    bare = HEADER + "# userEmail\nThe user's email address is " + EMAIL + '.\n</system-reminder>\n'
    queried, messages = steer_turn(bare)
    assert EMAIL not in relay(wire(messages), queried).decode()


def test_marker_on_a_dropped_standalone_reminder_moves_onto_the_frame():
    sent = [FIRST, FRAME, {'type': 'text', 'text': REMINDER_325, 'cache_control': MARKER}]
    out = json.loads(relay(wire([ASSISTANT, {'role': 'user', 'content': sent}]), [FIRST, FRAME]))
    assert out['messages'][1]['content'] == [FIRST, {**FRAME, 'cache_control': MARKER}]


@pytest.mark.parametrize('version', ['2.1.283', '2.1.325'])
def test_reminder_inside_structured_tool_result_content_is_removed(version):
    text = VERSIONS[version]
    host = {'type': 'tool_result', 'tool_use_id': 't2', 'content': [{'type': 'text', 'text': 'listing'}]}
    for items in ([{'type': 'text', 'text': 'listing\n\n' + text}],
                  [{'type': 'text', 'text': 'listing'}, {'type': 'text', 'text': text}]):
        sent = [FIRST, {**host, 'content': items, 'cache_control': MARKER}]
        out = relay(wire([ASSISTANT, {'role': 'user', 'content': sent}]), [FIRST, host]).decode()
        assert EMAIL not in out
        assert plain(json.loads(out)['messages'][1]['content']) == [FIRST, host]


# Native's other session_context sections, in its renderer's order: only the account section goes,
# and what stays is exactly what native renders for the same context without an account.
SECTIONS = [(GIT,), (PROJECT,), (P4,), (PROJECT, GIT, P4)]
SECTION_IDS = ['gitStatus', 'attachedProject', 'perforceMode', 'all']


@pytest.mark.parametrize('version', ['2.1.283', '2.1.325', '2.1.325-private'])
@pytest.mark.parametrize('sections', SECTIONS, ids=SECTION_IDS)
def test_other_native_sections_stay_when_the_account_section_goes(sections, version):
    use = {'2.1.283': USE_283, '2.1.325': USE_325, '2.1.325-private': USE_325_PRIVATE}[version]
    trailer = IMPORTANT if version == '2.1.283' else ATTRIBUTION
    text, expected = reminder(use, trailer, *sections), HEADER + '\n'.join(sections) + trailer
    queried, messages = steer_turn(text)
    out = relay(wire(messages), queried).decode()
    assert EMAIL not in out
    assert plain(json.loads(out)['messages'][1]['content']) == [RESULT, STEER, {'type': 'text', 'text': expected}, DATE]
    # As a suffix of the frame's text, too.
    queried, messages = native_turn('text_suffix', text, lead=())
    body = json.loads(relay(wire(messages), queried))
    assert plain(body['messages'][1]['content']) == [{**FRAME, 'text': FRAME['text'] + '\n\n' + expected}]


VARIANTS = [
    REMINDER_296.replace('reported back.', 'reported back. Extra.'),
    REMINDER_283.replace('# userEmail', '# userAccount'),
    REMINDER_296 + 'trailing native text',
    REMINDER_283.replace('\n\nIMPORTANT', "\n# currentDate\nToday's date is 2026-10-03.\n\nIMPORTANT"),
    reminder(USE_325, ATTRIBUTION, P4, GIT),
    reminder(USE_325, ATTRIBUTION, '# gitStatus\n'),
    REMINDER_325.replace('As you answer the user\'s questions, you can use the following context:',
                         'The session context has changed; these values replace the earlier ones:'),
    REMINDER_325.replace(' A site asking', ' A website asking'),
    REMINDER_325.replace(ATTRIBUTION, ''),
]
VARIANT_IDS = ['extra_sentence', 'other_heading', 'trailing_text', 'unknown_section', 'sections_out_of_order',
               'empty_section', 'changed_header', 'reworded_sentence', 'no_trailer']


@pytest.mark.parametrize('variant', VARIANTS, ids=VARIANT_IDS)
@pytest.mark.parametrize('placement', ['text_suffix', 'tool_suffix', 'standalone_after'])
def test_unknown_reminder_variant_is_not_dropped_as_the_account_reminder(placement, variant):
    """Only the exact template is recognized. (Trailing plain text in a tool-result frame is
    ``restore_queried_turn``'s own, wording-independent rule, so this checks the other steps.)"""
    queried, messages = native_turn(placement, variant)
    raw = wire(messages)
    assert content(pin_message_breakpoint(stable_request_context(drop_account_reminder(raw, queried), queried),
                                          queried)) == content(raw)


@pytest.mark.parametrize('variant', VARIANTS, ids=VARIANT_IDS)
def test_unknown_reminder_after_a_steer_row_reaches_upstream_unchanged(variant):
    queried, messages = steer_turn(variant)
    raw = wire(messages)
    assert content(relay(raw, queried)) == content(raw)


def test_reminder_in_an_earlier_frame_block_is_left_alone():
    """Native appends after the frame's last block only: a template inside an earlier one is not its."""
    second = {'type': 'text', 'text': 'second'}
    sent = [{**FRAME, 'text': FRAME['text'] + '\n\n' + REMINDER_325}, {**second, 'cache_control': MARKER}]
    raw = wire([ASSISTANT, {'role': 'user', 'content': sent}])
    assert content(relay(raw, [FRAME, second])) == content(raw)
    host = {'type': 'tool_result', 'tool_use_id': 't2', 'content': [{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}]}
    items = [{'type': 'text', 'text': 'a\n\n' + REMINDER_325}, {'type': 'text', 'text': 'b'}]
    raw = wire([ASSISTANT, {'role': 'user', 'content': [{**host, 'content': items}, STEER]}])
    assert content(relay(raw, [host, STEER])) == content(raw)


def test_user_text_containing_an_email_is_untouched():
    """Host text that quotes the template (or any email) is Hermes' own content, not native's."""
    host = {'type': 'text', 'text': 'Please reply to ' + EMAIL + ':\n\n' + REMINDER_325}
    queried = [FIRST, host]
    messages = [ASSISTANT, {'role': 'user', 'content': [FIRST, {**host, 'cache_control': MARKER}]}]
    out = json.loads(relay(wire(messages), queried))
    assert plain(out['messages'][1]['content']) == queried
    # A standalone reminder block that Hermes itself queried is not native's addition.
    queried = [FRAME, {'type': 'text', 'text': REMINDER_325}]
    messages = [ASSISTANT, {'role': 'user', 'content': [FRAME, {'type': 'text', 'text': REMINDER_325,
                                                                 'cache_control': MARKER}]}]
    raw = wire(messages)
    assert content(relay(raw, queried)) == content(raw)


def test_ambiguous_frame_is_forwarded_unchanged():
    sent = [FRAME, FRAME, {'type': 'text', 'text': REMINDER_325, 'cache_control': MARKER}]
    raw = wire([ASSISTANT, {'role': 'user', 'content': sent}])
    assert content(relay(raw, [FRAME])) == content(raw)


def test_changed_tool_identity_is_forwarded_unchanged():
    sent = [{**RESULT, 'tool_use_id': 'other'}, STEER, {'type': 'text', 'text': REMINDER_325}, {**DATE, 'cache_control': MARKER}]
    raw = wire([ASSISTANT, {'role': 'user', 'content': sent}])
    assert content(relay(raw, [RESULT, STEER])) == content(raw)


@pytest.mark.parametrize('version', VERSIONS)
def test_prepended_reminder_on_the_opening_turn_is_still_dropped(version):
    messages = call1()
    messages[0]['content'][0] = {'type': 'text', 'text': VERSIONS[version]}
    out = relay(wire(messages), [FRAME]).decode()
    assert EMAIL not in out
    assert plain(json.loads(out)['messages'][0]['content']) == [FRAME]


def test_transform_is_idempotent():
    queried, messages = native_turn('text_suffix', REMINDER_325)
    once = relay(wire(messages), queried)
    assert relay(once, queried) == once
    for text in (REMINDER_325, reminder(USE_325, ATTRIBUTION, GIT)):
        queried, messages = steer_turn(text)
        once = drop_account_reminder(wire(messages), queried)
        assert drop_account_reminder(once, queried) == once


def test_large_native_sections_are_matched_quickly():
    huge = '# gitStatus\n' + 'M file.py\n' * 200000
    for text in (reminder(USE_325, ATTRIBUTION, huge), reminder(USE_325, ATTRIBUTION, huge) + 'x'):
        queried, messages = steer_turn(text)
        started = time.perf_counter()
        relay(wire(messages), queried)
        assert time.perf_counter() - started < 2


def test_the_address_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    for queried, messages in (steer_turn(REMINDER_325), native_turn('tool_suffix', REMINDER_325)):
        relay(wire(messages), queried)
    assert EMAIL not in caplog.text


def _through_the_relay(queried, native):
    """Send ``native`` through a real Admission; returns (forwarded bodies, gate.unrestored)."""
    received = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    gate = Admission(f'http://127.0.0.1:{peer.server_port}', 5, queried=queried)
    try:
        route = gate.url.removeprefix(f'http://127.0.0.1:{gate.server.server_port}') + '/v1/messages'
        conn = http.client.HTTPConnection('127.0.0.1', gate.server.server_port, timeout=5)
        conn.request('POST', route, wire(native), {'Content-Type': 'application/json'})
        try:
            conn.getresponse().read()
        except http.client.RemoteDisconnected:
            pass
        finally:
            conn.close()
        return received, gate.unrestored
    finally:
        gate.close()
        peer.shutdown()
        thread.join()
        peer.server_close()


def test_restoration_is_judged_after_the_reminder_is_dropped():
    """A marked reminder block was a restoration mismatch, reported as ``unrestored`` even though
    nothing else differed; with the reminder gone first the frame restores and nothing is reported."""
    native = [ASSISTANT, {'role': 'user', 'content': [FIRST, RESULT, {'type': 'text', 'text': REMINDER_325,
                                                                       'cache_control': MARKER}]}]
    received, unrestored = _through_the_relay([FIRST, RESULT], native)
    assert len(received) == 1 and EMAIL not in received[0].decode()
    assert plain(json.loads(received[0])['messages'][1]['content']) == [FIRST, RESULT]
    assert unrestored is None


def test_a_steer_row_still_reports_the_date_line_it_could_not_restore():
    queried, native = steer_turn(REMINDER_325)
    received, unrestored = _through_the_relay(queried, native)
    assert len(received) == 1 and EMAIL not in received[0].decode()
    assert plain(json.loads(received[0])['messages'][1]['content']) == [RESULT, STEER, DATE]
    # The marked date line native added is still forwarded as native built it, and still reported.
    assert unrestored == 'Native request does not uniquely match Hermes tool results'
