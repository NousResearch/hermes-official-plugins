"""Request-scoped Claude Code transport with host-owned HTTP admission."""
from __future__ import annotations

import asyncio
import atexit
import copy
from collections import OrderedDict, deque
from contextlib import suppress
import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import weakref
from types import SimpleNamespace

try:
    from .model_catalog import accepts_thinking_disable, native_model, supports_adaptive_thinking
    from .process_tree import kill_process_tree, own_process_group, unreaped
    from .directsdk_setup import (
        ClaudeCodeLoggedOut, INSTALL_HINT, LOGGED_OUT_HINT, _resolve as resolve_claude, apply_config_dir, apply_profile_credential,
        apply_traffic_policy, check_extra_usage, drop_api_credentials, egress_env, observe_rate_limit, reap_orphaned_refresh_lock, relay,
        resolve_upstream, session_settings, Upstream)
except ImportError:
    from model_catalog import accepts_thinking_disable, native_model, supports_adaptive_thinking
    from process_tree import kill_process_tree, own_process_group, unreaped
    from directsdk_setup import (
        ClaudeCodeLoggedOut, INSTALL_HINT, LOGGED_OUT_HINT, _resolve as resolve_claude, apply_config_dir, apply_profile_credential,
        apply_traffic_policy, check_extra_usage, drop_api_credentials, egress_env, observe_rate_limit, reap_orphaned_refresh_lock, relay,
        resolve_upstream, session_settings, Upstream)


logger = logging.getLogger(__name__)


# Hermes picks retry vs fallback from an error's status_code (main loop and auxiliary ladder alike).
class ClaudeCodeMissing(RuntimeError):
    """The official Claude Code CLI this transport drives is not installed (or not on PATH)."""
    status_code = 503


def _request_id(value):
    """Only a bounded opaque ID may enter diagnostics, never arbitrary header contents."""
    return value if isinstance(value, str) and re.fullmatch(r'req_[A-Za-z0-9_-]{1,124}', value) else None


class ClaudeAPIError(RuntimeError):
    """A failed upstream or native-answered request; status_code is None when there is no HTTP equivalent."""

    def __init__(self, message, status_code=None, *, request_id=None):
        request_id = _request_id(request_id)
        if request_id:
            message += f' (request_id={request_id})'
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class RequestRejected(ValueError):
    """A request this transport cannot serve, refused before Claude Code starts: the same input fails every time."""


# Native's own error codes for errors it answers without a relayed upstream status.
NATIVE_ERROR_STATUS = {
    'rate_limit': 429,  # plan/session limit ("You've hit your session limit")
    'billing_error': 402,
    'authentication_failed': 401,
    'overloaded': 529,
    'server_error': 503,  # native's code for capacity, 5xx and OAuth-refresh races: transient
}


CARRIER = 'claude-subscription-directsdk-experimental.native_assistant'
PREFIX = 'mcp__hermes__'


class Object(SimpleNamespace):
    def model_dump(self, **_):
        def unpack(v):
            if isinstance(v, Object):
                return {k: unpack(x) for k, x in vars(v).items()}
            if isinstance(v, list):
                return [unpack(x) for x in v]
            return copy.deepcopy(v)
        return unpack(self)


def obj(value):
    if isinstance(value, dict):
        return Object(**{k: copy.deepcopy(v) if k in ('reasoning_details', 'native_usage') else obj(v) for k, v in value.items()})
    if isinstance(value, list):
        return [obj(v) for v in value]
    return value


def projection(message):
    calls = []
    for tc in message.get('tool_calls') or []:
        f = tc['function']
        args = f['arguments']
        calls.append({'id': tc['id'], 'name': f['name'],
                      'input': json.loads(args) if isinstance(args, str) else args})
    return {'content': (message.get('content') or '').strip(), 'tool_calls': calls}


def content_blocks(content):
    if content is None:
        return []
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    result = []
    for block in content:
        kind = block.get('type')
        if kind == 'text':
            result.append(copy.deepcopy(block))
        elif kind == 'image_url':
            url = block['image_url']['url']
            if url[:8].lower().startswith(('http://', 'https://')):
                # Native routing passes remote URLs through; the transport never fetches. A hint
                # keeps the turn alive where an error would fail every retry of the same history.
                result.append({'type': 'text', 'text': f'[Image not attached: remote URL {url}. '
                               'Call vision_analyze with this URL to see it.]'})
                continue
            if not url.startswith('data:') or ';base64,' not in url:
                raise RequestRejected('Only base64 data image_url inputs are supported')
            media, data = url[5:].split(';base64,', 1)
            result.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': data}})
        elif kind in ('image', 'document', 'tool_result'):
            result.append(copy.deepcopy(block))
        else:
            raise RequestRejected(f'Unsupported content block: {kind}')
    return result


def conversation_session_id(system, frames):
    """A native session id that stays the same for every request of one conversation.

    Derived from what the conversation starts with (the system prompt and the first frame), which every
    request replays unchanged until compaction rewrites the history (a cache boundary anyway)."""
    first = frames[0]['message'] if frames else None
    digest = hashlib.sha256(json.dumps([system, first], sort_keys=True, ensure_ascii=False).encode('utf-8')).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def _normalize_history_tool_ids(frames):
    """Map foreign IDs only on the outgoing wire; keep calls/results paired.

    Anthropic rejects IDs containing e.g. ':' from another provider's history.
    Preserve valid native IDs (and signed replay blocks), reserve them before
    assigning deterministic foreign IDs, and avoid collisions in this request.
    Never rewrite the source history, tool arguments, or result bodies.
    """
    references = []
    for frame in frames:
        for block in frame['message']['content']:
            field = {'tool_use': 'id', 'tool_result': 'tool_use_id'}.get(block.get('type'))
            if field is None:
                continue
            tool_id = block.get(field)
            if not isinstance(tool_id, str) or not tool_id:
                raise RequestRejected('Tool call/result IDs must be nonempty strings')
            references.append((block, field, tool_id))
    original_ids = {tool_id for _, _, tool_id in references}
    used = {tool_id for tool_id in original_ids if re.fullmatch(r'[a-zA-Z0-9_-]+', tool_id)}
    mapping = {}
    for tool_id in sorted(original_ids - used):
        base = 'toolu_hermes_' + hashlib.sha256(tool_id.encode('utf-8')).hexdigest()[:32]
        candidate, suffix = base, 0
        while candidate in used:
            suffix += 1
            candidate = f'{base}_{suffix}'
        mapping[tool_id] = candidate
        used.add(candidate)
    for block, field, tool_id in references:
        if tool_id in mapping:
            block[field] = mapping[tool_id]


def prepare_history(messages, names=None):
    system, frames = [], []
    for message in messages:
        role = message.get('role')
        if role in ('system', 'developer'):
            if frames:
                raise RequestRejected('System/developer messages must precede conversation history')
            if not isinstance(message.get('content'), str):
                raise RequestRejected('System content must be text')
            system.append(message['content'])
            continue
        if role == 'assistant':
            details = message.get('reasoning_details') or []
            carriers = [d for d in details if isinstance(d, dict) and d.get('type') == CARRIER]
            if carriers:
                if len(carriers) != 1 or carriers[0].get('version') != 1:
                    raise RequestRejected('Unsupported native assistant carrier version')
                carrier = carriers[0]
                expected = {**carrier['projection'], 'content': carrier['projection']['content'].strip()}
                if projection(message) == expected:
                    for native in carrier['messages']:
                        native = copy.deepcopy(native)
                        # Native sizes its local "Prompt is too long" gate from the newest assistant
                        # message's `usage`. A replayed frame keeps the usage of the request that made it,
                        # so after Hermes compresses the history it still reports the old size and native
                        # refuses locally without sending (#92). `usage` is local metadata, never on the wire.
                        native.pop('usage', None)
                        frames.append({'type': 'assistant', 'message': native})
                    continue
                # Host compaction/hooks own visible history. Never restore stale
                # pre-edit blocks or attach their signatures to rewritten content.
            blocks = content_blocks(message.get('content'))
            for call in projection(message)['tool_calls']:
                # Inverse of the result mapping: a call outside the inventory (#39) replays as the model named it.
                name = call['name']
                if names is None or name in names:
                    name = PREFIX + (names.get(name, name) if isinstance(names, dict) else name)
                blocks.append({'type': 'tool_use', 'id': call['id'], 'name': name, 'input': call['input']})
        elif role == 'tool':
            role = 'user'
            blocks = [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'],
                       'content': message.get('content') if isinstance(message.get('content'), str) else content_blocks(message.get('content'))}]
            if message.get('is_error') is not None:
                blocks[0]['is_error'] = bool(message['is_error'])
        elif role == 'user':
            blocks = content_blocks(message.get('content'))
        else:
            raise RequestRejected(f'Unsupported message role: {role}')
        if frames and frames[-1]['type'] == role and role == 'user':
            frames[-1]['message']['content'].extend(blocks)
        else:
            frames.append({'type': role, 'message': {'role': role, 'content': blocks}})
    if not frames or frames[-1]['type'] != 'user' or not frames[-1]['message']['content']:
        raise RequestRejected('History must end in a nonempty user/tool-result message; assistant prefill is unsupported')
    _normalize_history_tool_ids(frames)
    return '\n\n'.join(system), frames


_BANNED_TOP_LEVEL = ('oneOf', 'allOf', 'anyOf')


def normalize_input_schema(schema):
    """Anthropic's validator hard-400s on top-level oneOf/allOf/anyOf and on the null branch of
    nullable unions. The host normalizes both in ``agent.anthropic_message_convert``, but only for
    ``api_mode='messages'``; this transport is ``chat_completions``, so mirror it here. The
    combinators are advisory (handlers re-validate their arguments); nested unions stay untouched."""
    from tools.schema_sanitizer import strip_nullable_unions
    normalized = strip_nullable_unions(schema, keep_nullable_hint=False)
    if any(key in normalized for key in _BANNED_TOP_LEVEL):
        normalized = {k: v for k, v in normalized.items() if k not in _BANNED_TOP_LEVEL}
        normalized.setdefault('type', 'object')
    if normalized.get('type') == 'object' and not isinstance(normalized.get('properties'), dict):
        normalized = {**normalized, 'properties': {}}
    return normalized


# Native structured output (`output_config.format`) 400s on these keywords (live-probed): numeric
# bounds on integer/number, size bounds on object, array bounds other than `minItems` 0/1, and an
# `enum` beside a `type` list (a nullable enum, whose members are checked against the whole list).
# The reply is validated against the caller's full schema by Hermes (agent/plugin_llm.py), so
# dropping them from the wire keeps them enforced, and the enum or the remaining keywords still
# steer the model. Without this, core retries the request without a schema and every structured
# call is paid twice.
_NUMERIC_BOUNDS = ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf')
_UNSUPPORTED_FORMAT_KEYS = {
    'integer': _NUMERIC_BOUNDS,
    'number': _NUMERIC_BOUNDS,
    'array': ('maxItems', 'uniqueItems'),
    'object': ('minProperties', 'maxProperties'),
}


def native_format_schema(node):
    """A copy of a JSON Schema without the keywords native structured output rejects."""
    if isinstance(node, list):
        return [native_format_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    declared = node.get('type')
    kinds = declared if isinstance(declared, list) else [declared]
    dropped = {key for kind in kinds if isinstance(kind, str) for key in _UNSUPPORTED_FORMAT_KEYS.get(kind, ())}
    if isinstance(declared, list) and 'enum' in node:
        dropped.add('type')
    out = {key: native_format_schema(value) for key, value in node.items() if key not in dropped}
    # Native takes `minItems` 0 or 1 only; 1 is the nearest constraint it accepts (validation keeps the real one).
    if 'array' in kinds and isinstance(out.get('minItems'), int) and out['minItems'] > 1:
        out['minItems'] = 1
    return out


def request_body(kwargs):
    allowed = {'model', 'messages', 'tools', 'stream', 'stream_options', 'max_tokens', 'max_completion_tokens',
               'temperature', 'top_p', 'stop', 'extra_body', 'extra_headers', 'timeout', 'tool_choice', 'parallel_tool_calls', 'n', 'response_format',
               'hermes_auxiliary'}
    unknown = set(kwargs) - allowed
    if unknown:
        raise RequestRejected('Unsupported request parameters: ' + ', '.join(sorted(unknown)))
    # Hermes Relay attaches host tracing to chat-completions clients. Keep it host-local:
    # never project it into generation fields or override the official CLI's identity.
    headers = kwargs.get('extra_headers')
    if headers is not None and (not isinstance(headers, dict) or any(
            not isinstance(key, str) or key.lower() != 'traceparent' or not isinstance(value, str)
            for key, value in headers.items())):
        raise RequestRejected('extra_headers supports host traceparent metadata only')
    if kwargs.get('n', 1) != 1 or kwargs.get('tool_choice', 'auto') not in ('auto', None):
        raise RequestRejected('Only n=1 and tool_choice=auto are supported')
    if kwargs.get('parallel_tool_calls') is False:
        raise RequestRejected('parallel_tool_calls=False is unsupported')
    if kwargs.get('stream_options') not in (None, {}, {'include_usage': True}, {'include_usage': False}):
        raise RequestRejected('Unsupported stream_options')
    extra = kwargs.get('extra_body')
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise RequestRejected('extra_body must be an object')
    unknown_extra = set(extra) - {'max_tokens', 'temperature', 'top_p', 'stop_sequences', 'reasoning', 'response_format'}
    if unknown_extra:
        raise RequestRejected('Unsupported extra_body fields: ' + ', '.join(sorted(unknown_extra)))
    body = copy.deepcopy(extra)
    reasoning = body.pop('reasoning', None)
    if reasoning is not None:
        if not isinstance(reasoning, dict) or set(reasoning) - {'enabled', 'effort'}:
            raise RequestRejected('reasoning supports enabled and effort only')
        if 'enabled' in reasoning and type(reasoning['enabled']) is not bool:
            raise RequestRejected('reasoning.enabled must be boolean')
        from agent.reasoning_effort import clamp_effort
        effort = clamp_effort(reasoning.get('effort'), ('none', 'low', 'medium', 'high', 'xhigh', 'max'))
        if effort not in (None, 'none', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise RequestRejected('Unsupported native reasoning effort')
        if reasoning.get('enabled') is False or effort == 'none':
            if accepts_thinking_disable(kwargs.get('model')):
                body['thinking'] = {'type': 'disabled'}
                # Native clear-thinking context edits are invalid when thinking is disabled.
                body['context_management'] = {'edits': []}
            else:
                # The disable is impossible here, and with no effort native thinks at its default
                # (hundreds to ~1k reasoning tokens on a short structured answer, 2x the latency).
                # The lowest effort is the nearest "off": it reaches --effort below.
                body['output_config'] = {'effort': 'low'}
        else:
            # Routes without adaptive thinking (Haiku 4.5) 400 on the block; their own
            # default thinking plus the effort signal below stand in for it.
            if reasoning.get('enabled') is True and supports_adaptive_thinking(kwargs.get('model')):
                # 4.7+ defaults thinking.display to "omitted" (empty thinking + signature only),
                # which leaves Hermes' reasoning panel blank. Mirror core's anthropic_adapter.
                body['thinking'] = {'type': 'adaptive', 'display': 'summarized'}
            if effort:
                body['output_config'] = {'effort': effort}
    response_format = kwargs.get('response_format', body.pop('response_format', None))
    if response_format and response_format.get('type') != 'text':
        if response_format.get('type') != 'json_schema':
            raise RequestRejected('Only json_schema structured output is supported')
        schema = response_format.get('json_schema', {}).get('schema')
        if not isinstance(schema, dict):
            raise RequestRejected('response_format requires a JSON Schema object')
        body.setdefault('output_config', {})['format'] = {'type': 'json_schema', 'schema': native_format_schema(schema)}
    for key in ('max_tokens', 'temperature', 'top_p'):
        if kwargs.get(key) is not None:
            body[key] = kwargs[key]
    if kwargs.get('max_completion_tokens') is not None:
        if 'max_tokens' in body:
            raise RequestRejected('Specify only one output-token limit')
        body['max_tokens'] = kwargs['max_completion_tokens']
    if kwargs.get('stop') is not None:
        stop = kwargs['stop']
        body['stop_sequences'] = [stop] if isinstance(stop, str) else stop
    for key in ('temperature', 'top_p'):
        if key in body and (isinstance(body[key], bool) or not isinstance(body[key], (int, float)) or not math.isfinite(body[key]) or not 0 <= body[key] <= 1):
            raise RequestRejected(f'{key} must be finite and between zero and one')
        # Subscription models reject sampling controls, including Hermes' title
        # generator default. Match the host's sampling-forbidden model behavior.
        body.pop(key, None)
    if 'max_tokens' in body and (type(body['max_tokens']) is not int or body['max_tokens'] < 1):
        raise RequestRejected('max_tokens must be a positive integer')
    if 'stop_sequences' in body and (not isinstance(body['stop_sequences'], list) or not all(isinstance(x, str) and x for x in body['stop_sequences'])):
        raise RequestRejected('stop_sequences must be a list of nonempty strings')
    manifest, tools, names = [], [], {}
    native_names = {}
    for tool in kwargs.get('tools') or []:
        if tool.get('type') != 'function':
            raise RequestRejected('Only function tools are supported')
        f = tool['function']
        name = f['name']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or name in names:
            raise RequestRejected('Tool names must be unique ASCII identifiers of at most 64 characters')
        if f.get('strict'):
            raise RequestRejected('Strict function schemas are unsupported')
        native_name = name if len(name) <= 50 else name[:41] + '_' + hashlib.sha256(name.encode()).hexdigest()[:8]
        if native_name in native_names:
            raise RequestRejected(f'Tool name collision: {name!r} and {native_names[native_name]!r} map to {native_name!r}')
        names[name] = native_name
        native_names[native_name] = name
        schema, description = f.get('parameters', {'type': 'object'}), f.get('description', '')
        if not isinstance(schema, dict) or not isinstance(description, str):
            raise RequestRejected('Tool schema must be an object and description a string')
        # The manifest (inert MCP server) and the request body must advertise the same shape.
        schema = normalize_input_schema(schema)
        manifest.append({'name': native_name, 'description': description, 'inputSchema': schema})
        tools.append({'name': PREFIX + native_name, 'description': description, 'input_schema': schema})
    body['tools'] = tools
    encoded = json.dumps(body, separators=(',', ':'), allow_nan=False)
    return encoded, manifest, names


class Request:
    def __init__(self, client):
        self.client, self.process = client, None
        self.stream = None
        self.cancelled = threading.Event()
        self.admission = None
        self.lock = threading.Lock()

    def cancel(self):
        self.cancelled.set()
        if self.admission is not None:
            self.admission.abort()
        with self.lock:
            if self.process is not None:
                kill_process_tree(self.process)

    def spawn(self, command, *, stdin=subprocess.DEVNULL, **kwargs):
        with self.lock:
            if self.cancelled.is_set():
                raise RuntimeError('Claude request cancelled')
            self.process = subprocess.Popen(command, stdin=stdin, **kwargs, **own_process_group())
        return self.process


DRAIN_GRACE_S = 10.0
_draining = set()


def _kill_draining():
    for process in list(_draining):
        kill_process_tree(process)
    with _live_lock:
        live = tuple(_live)
    for native in live:
        native.kill()


atexit.register(_kill_draining)


def _fast_eligible(result, admission, stopped, native_error):
    """A clean admitted answer returns at native's result event. Its exit code, any later stdout and a hang past
    the timeout are then never read: the answer is already complete, and the drain kills a native that will not exit."""
    return (admission.used and admission.status == 200 and admission.capture.complete
            and stopped and native_error is None and admission.denied == 0
            and admission.capture.message.get('stop_reason') != 'refusal'
            and ((result.get('subtype') == 'success' and not result.get('is_error'))
                 or (result.get('subtype') == 'error_max_turns' and any(
                     b.get('type') == 'tool_use' for b in admission.capture.message['content'])
                     or bool(admission.capture.unparsed))))


# Conversations whose streamed tool input came back invalid: their next requests ask the API to buffer and validate
# arguments (no eager_input_streaming), as core's Anthropic transport does on the same failure. Bounded; compaction
# starts a new conversation id, and with it streaming again.
_BUFFER_TOOL_INPUT = OrderedDict()


def _buffer_tool_input(session_id):
    _BUFFER_TOOL_INPUT[session_id] = True
    _BUFFER_TOOL_INPUT.move_to_end(session_id)
    while len(_BUFFER_TOOL_INPUT) > 256:
        _BUFFER_TOOL_INPUT.popitem(last=False)


def offered_tool_name(name, hermes_names):
    """``(name to carry in native's history, name Hermes runs)`` for a tool_use native streamed."""
    if name in hermes_names:
        # Native sometimes drops the prefix on a tool it was offered (#62); carry the offered name so replayed
        # history does not teach the slip back to the model.
        name = PREFIX + name
    elif name.startswith('mcp__') and not name.startswith(PREFIX) and name[5:] in hermes_names:
        # Or only the server segment (`mcp__tool_call`); restore it so core does not log an auto-repair per call.
        name = PREFIX + name[5:]
    # Any other name is the model's (a Tool Search-deferred tool called directly, #39); Hermes answers it.
    bare = name.removeprefix(PREFIX)
    return name, hermes_names.get(bare, bare)


def _json_suffix(prefix):
    """A suffix that makes a cut-off JSON text parse, or None. Used only for a refused call's streamed arguments:
    Hermes cannot take back a streamed fragment, and one it cannot repair turns the refusal into a truncation
    that it retries. The call is never run."""
    stack, in_string, escaped, unicode = [], False, False, 0
    for ch in prefix:
        if unicode:
            unicode = unicode - 1 if ch in '0123456789abcdefABCDEF' else 0
        elif in_string:
            escaped, in_string, unicode = (False, True, 4 if ch == 'u' else 0) if escaped else (ch == '\\', ch != '"', 0)
        elif ch == '"':
            in_string = True
        elif ch in '{[':
            stack.append('}' if ch == '{' else ']')
        elif ch in '}]' and stack:
            stack.pop()
    head = '0' * unicode + ('\\' if escaped else '') + ('"' if in_string else '')
    closers = ''.join(reversed(stack))
    for filler in ('', 'null', ':null', '"":null', '0', 'e', 'ue', 'rue', 'l', 'll', 'ull', 'se', 'lse', 'alse'):
        try:
            json.loads(prefix + head + filler + closers)
        except ValueError:
            continue
        return head + filler + closers
    return None


class _ToolStream:
    """Stream each tool call to Hermes as native streams it: the header (id and name) at ``content_block_start``,
    so Hermes shows the tool being prepared at once (#326), then each argument fragment."""

    def __init__(self, hermes_names):
        self.hermes_names, self.slots, self.raw = hermes_names, {}, {}

    def deltas(self, native):
        """The ``tool_calls`` deltas for one native stream event, or None when it is not a tool event."""
        index, block = native.get('index'), native.get('content_block') or {}
        if native['type'] == 'content_block_start' and block.get('type') == 'tool_use':
            self.slots[index], self.raw[index] = (len(self.slots), block.get('id')), ''
            name = offered_tool_name(block.get('name', ''), self.hermes_names)[1]
            return [{'index': self.slots[index][0], 'id': block.get('id'), 'type': 'function', 'function': {'name': name, 'arguments': ''}}]
        if index not in self.slots:
            return None
        if native.get('delta', {}).get('type') == 'input_json_delta':
            fragment = native['delta'].get('partial_json', '')
            self.raw[index] += fragment
            return [{'index': self.slots[index][0], 'function': {'arguments': fragment}}] if fragment else []
        if native['type'] == 'content_block_stop' and not self.raw[index].strip():
            self.raw[index] = '{}'  # A no-argument call streams nothing; Hermes runs '{}'.
            return [{'index': self.slots[index][0], 'function': {'arguments': '{}'}}]
        return None

    def streamed(self, call_id):
        return any(slot[1] == call_id for slot in self.slots.values())

    def close_refused(self):
        """Deltas that make every refused call's streamed arguments parse, so Hermes still sees one refusal."""
        out = []
        for index, (slot, _) in self.slots.items():
            raw = self.raw[index]
            try:
                json.loads(raw)
            except ValueError:
                suffix = _json_suffix(raw) if raw.strip() else '{}'
                if suffix:
                    out.append({'index': slot, 'function': {'arguments': suffix}})
        return out


def _hand_off(native):
    """Let a one-shot native exit on its own after its answer; the drain kills one that will not."""
    process = native.process
    _draining.add(process)

    def drain():
        try:
            try:
                # Kanban may reap native first; its exit code is irrelevant after result.
                process.wait(timeout=DRAIN_GRACE_S)
            except subprocess.TimeoutExpired:
                kill_process_tree(process)
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            native.reader.join(timeout=5)
            native.err_drain.join(timeout=5)
        finally:
            try:
                # Keep late native requests behind the consumed admission until it exits.
                native.release()
            finally:
                _draining.discard(process)

    threading.Thread(target=drain, daemon=True, name='directsdk-drain').start()


# Kept-alive natives of every client in this process, against the max_sessions cap; one session idles out after
# this long, the prompt cache's own lifetime.
_live, _live_lock = set(), threading.Lock()
SESSION_IDLE_S = 3600.0
_LAUNCH_DIR = re.compile(r'claude-directsdk-launch-(\d+)-')
_swept = []


def _sweep_launch_dirs():
    """Remove launch files a killed Hermes process left behind (they hold the system prompt and tool schemas)."""
    import psutil
    root = Path(tempfile.gettempdir())
    for path in root.glob('claude-directsdk-launch-*'):
        match = _LAUNCH_DIR.match(path.name)
        try:
            ours = not hasattr(os, 'getuid') or path.lstat().st_uid == os.getuid()
        except OSError:
            continue
        if match and ours and int(match.group(1)) != os.getpid() and not psutil.pid_exists(int(match.group(1))):
            shutil.rmtree(path, ignore_errors=True)


def _launch_dir():
    """Private files native reads once at startup; removed after its first step, swept after a hard kill."""
    if not _swept:
        _swept.append(True)
        _sweep_launch_dirs()
    return Path(tempfile.mkdtemp(prefix=f'claude-directsdk-launch-{os.getpid()}-'))


class Native:
    """One Claude Code process and the relay its requests go through.

    A session (``tools`` given) keeps stdin open and serves Hermes' tools as an SDK MCP server over the control
    protocol: native's ``tools/call`` waits until Hermes brings the result, so native never runs a tool. A one-shot
    gets end of input after its frames and answers one step."""

    def __init__(self, request, command, env, workdir, admission, launch_dir, tools=None):
        self.admission, self.launch_dir, self.tools = admission, launch_dir, tools
        self.events, self.stderr = queue.Queue(), deque(maxlen=40)
        self.lock, self.writing = threading.Lock(), threading.Lock()
        self.parked, self.results = {}, {}
        # A kept session: what it was launched with, the frames it holds, and what the next step must bring.
        self.ident = self.frames = self.expect = self.idle = None
        self.cost = 0
        self.process = request.spawn(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding='utf-8', cwd=workdir, env=env)
        # Native's stderr is diagnosis, not noise: a gateway-only failure (issue #44) vanished into DEVNULL.
        self.err_drain = threading.Thread(target=self._drain_err, daemon=True)
        self.err_drain.start()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _drain_err(self):
        with suppress(Exception):
            for line in self.process.stderr:
                self.stderr.append(line)
        with suppress(Exception):
            self.process.stderr.close()

    def _read(self):
        process = self.process
        try:
            for line in process.stdout:
                event = json.loads(line)
                if event.get('type') == 'control_request':
                    self._control(event)
                elif event.get('type') != 'control_response':
                    self.events.put(event)
        except Exception as error:
            self.events.put(error)
        finally:
            # The consumer may close while paused at a yielded chunk. Reaping belongs to this owner thread, never
            # cancel(). Clear the group first: a launcher that exits leaves children the reap would orphan.
            if os.name != 'nt' and hasattr(os, 'waitid') and unreaped(process, block=True):
                kill_process_tree(process)
            process.wait()
            self.events.put(None)

    def _write(self, row):
        with self.writing, suppress(OSError, ValueError):
            self.process.stdin.write(json.dumps(row, allow_nan=False) + '\n')
            self.process.stdin.flush()

    def _control(self, event):
        """Answer native's MCP traffic for the hermes server; park each tools/call until Hermes has its result."""
        request = event.get('request') or {}
        message = request.get('message') or {}
        if request.get('subtype') != 'mcp_message':
            # dontAsk with mcp__hermes allowed leaves native nothing else to ask; never let it act on its own.
            self._write({'type': 'control_response', 'response': {'subtype': 'error', 'request_id': event.get('request_id'),
                                                                  'error': 'Hermes runs every tool'}})
            return
        if message.get('method') == 'tools/call':
            tool_id = ((message.get('params') or {}).get('_meta') or {}).get('claudecode/toolUseId')
            with self.lock:
                self.parked[tool_id] = (event['request_id'], message.get('id'))
                self._answer(tool_id)
            return
        result = {}
        if message.get('method') == 'initialize':
            result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'hermes', 'version': '1'}}
        elif message.get('method') == 'tools/list':
            # Without the size hint native keeps a ~2 KB stub in its own history for a result over 50,000 chars.
            result = {'tools': [dict(tool, _meta={'anthropic/maxResultSizeChars': 500_000}) for tool in self.tools or ()]}
        self._reply(event['request_id'], {'jsonrpc': '2.0', 'id': message['id'], 'result': result} if 'id' in message else {'jsonrpc': '2.0', 'result': {}})

    def _reply(self, request_id, response):
        self._write({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': request_id,
                                                              'response': {'mcp_response': response}}})

    def _answer(self, tool_id):
        if tool_id in self.parked and tool_id in self.results:
            request_id, rpc_id = self.parked.pop(tool_id)
            content, is_error = self.results.pop(tool_id)
            self._reply(request_id, {'jsonrpc': '2.0', 'id': rpc_id, 'result': {'content': [{'type': 'text', 'text': content}], 'isError': is_error}})

    def deliver(self, results):
        """Hermes' results for the parked calls: each answered now, or when native asks (it asks one at a time)."""
        with self.lock:
            self.results.update(results)
            for tool_id in results:
                self._answer(tool_id)

    def send(self, request, data, timeout):
        if data is None and self.process.stdin.closed:
            return
        """Write one frame (None: end of input). A child that stops reading stdin blocks a plain write past the
        timeout: kill it then."""
        stalled = threading.Event()
        timer = threading.Timer(timeout, lambda: (stalled.set(), request.cancel()))
        timer.start()
        try:
            with self.writing:
                if data is None:
                    self.process.stdin.close()
                else:
                    self.process.stdin.write(json.dumps(data, allow_nan=False) + '\n')
                    self.process.stdin.flush()
        except OSError:
            if not request.cancelled.is_set():
                raise
        finally:
            timer.cancel()
        if stalled.is_set():
            raise TimeoutError('Claude request timed out')
        if request.cancelled.is_set():
            raise RuntimeError('Claude request cancelled')

    def receive(self, request, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if request.cancelled.is_set():
                raise RuntimeError('Claude request cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Claude request timed out')
            try:
                event = self.events.get(timeout=min(remaining, .2))
            except queue.Empty:
                continue
            if isinstance(event, Exception):
                # The offending stdout line is the whole diagnosis (a shim banner, a stray print); keep it.
                raise RuntimeError('Invalid native stream-json output: ' + repr((getattr(event, 'doc', None) or str(event))[:300]) + _stderr_tail(self.stderr)) from event
            return event

    def replay(self, request, frames, timeout):
        """Hand native the history before the newest frame, one acknowledged frame at a time, then the newest."""
        for index, frame in enumerate(frames):
            frame = copy.deepcopy(frame)
            if frame['type'] == 'user' and index < len(frames) - 1:
                frame['shouldQuery'] = False
            self.send(request, frame, timeout)
            while frame.get('shouldQuery') is False:
                ack = self.receive(request, timeout)
                if ack is None:
                    # cancel() kills native, so the reader's None can win the race with receive()'s check.
                    if request.cancelled.is_set():
                        raise RuntimeError('Claude request cancelled')
                    self.err_drain.join(timeout=1)
                    raise RuntimeError(f'Native exited before replay acknowledgment (exit={self.process.returncode}){_stderr_tail(self.stderr)}')
                if ack.get('type') == 'result':
                    if ack.get('num_turns') != 0 or ack.get('is_error'):
                        raise RuntimeError('Native history replay not supported: expected zero-turn acknowledgment' + _stderr_tail(self.stderr))
                    break

    def resumes(self, frames):
        """How this kept session continues ``frames``: ('user', frame), ('tools', results), or None (restart)."""
        n = len(self.frames)
        if len(frames) != n + 1 or frames[:n] != self.frames:
            return None
        blocks = frames[n]['message']['content']
        if self.expect == 'user':
            return ('user', frames[n]) if any(b.get('type') != 'tool_result' for b in blocks) else None
        results = {b.get('tool_use_id'): (b.get('content'), bool(b.get('is_error'))) for b in blocks if b.get('type') == 'tool_result'}
        if len(results) != len(blocks) or set(results) != self.expect or not all(isinstance(c, str) for c, _ in results.values()):
            return None
        return 'tools', results

    def settled(self):
        """Drop what the last step left queued (native's own echo of its turn); False once native has exited.
        Nothing new arrives meanwhile: native waits for a parked result or the next user frame."""
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return self.process.poll() is None
            if event is None or isinstance(event, Exception):
                return False

    def forget_launch(self):
        shutil.rmtree(self.launch_dir, ignore_errors=True)

    def kill(self):
        """Signal native's group and stop its relay; safe from any thread."""
        if self.idle is not None:
            self.idle.cancel()
        self.admission.abort()
        kill_process_tree(self.process)

    def release(self):
        """Owner-thread teardown after kill() or exit: relay, pipes, launch files, the cap slot."""
        with _live_lock:
            _live.discard(self)
        self.admission.close()
        with suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout):
            # Closing stdin re-flushes bytes a dead child never read; that BrokenPipe must not replace the error raised.
            with suppress(OSError, ValueError):
                pipe.close()
        self.forget_launch()


# Node, cmd.exe and Claude Code's own config lookup need these even when the caller hands us a
# deliberately minimal environment; without SystemRoot a Windows child cannot even open a socket.
_WINDOWS_ESSENTIALS = ('SYSTEMROOT', 'SYSTEMDRIVE', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA')


def _with_windows_essentials(env):
    if os.name != 'nt':
        return env
    present = {key.upper() for key in env}
    for key, value in os.environ.items():
        if key.upper() in _WINDOWS_ESSENTIALS and key.upper() not in present:
            env[key] = value
    return env


def _with_login_identity(env):
    """Carry the login identity Claude Code's local check reads into the child env.

    Without ``USER`` a perfectly logged-in CLI reports ``loggedIn: false`` - instantly, with
    empty stderr and no upstream request (#44; confirmed with ``claude auth status`` in the
    issue thread). A supervised gateway (launchd background, containers, cron) or a filtered
    env dict can omit it even when the process itself has one, so derive it before spawn:
    from the parent environment, else from the running uid on POSIX.
    """
    if os.name == 'nt':
        name = env.get('USERNAME') or env.get('USER') or os.environ.get('USERNAME') or ''
        if name:
            env.setdefault('USERNAME', name)
            env.setdefault('USER', name)
        return env
    name = env.get('USER') or os.environ.get('USER') or ''
    if not name:
        try:
            import pwd
            name = pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError):
            name = ''
    if name:
        env.setdefault('USER', name)
        env.setdefault('LOGNAME', name)
    return env


def _rate_limit_summary(event) -> "dict | None":
    """Secret-free summary of native's ``rate_limit_event`` (#43).

    ``overageStatus: "allowed"`` only means overage is permitted, not in use;
    paid use is ``isUsingOverage`` true or the ``overage`` window type.
    Subscription windows (``five_hour``, ``seven_day``, ``seven_day_opus``,
    ``seven_day_sonnet``, ``seven_day_overage_included``) are not paid use."""
    info = event.get('rate_limit_info') if isinstance(event, dict) else None
    if not isinstance(info, dict):
        return None
    window = str(info.get('rateLimitType') or '')
    summary = {'window': window, 'overage_status': str(info.get('overageStatus') or ''),
               'using_overage': bool(info.get('isUsingOverage')) or window == 'overage'}
    # Epoch seconds: when the plan window frees up, and when the extra-usage allowance does.
    for key, source in (('resets_at', 'resetsAt'), ('overage_resets_at', 'overageResetsAt')):
        value = info.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            summary[key] = value
    return summary


def _stderr_tail(native_stderr):
    """The first native stderr lines, for the raised error - a gateway-only failure with
    stderr sent to DEVNULL is undiagnosable (#44): the reporter had to patch the plugin to
    learn the stream was empty."""
    text = ' '.join(line.strip() for line in native_stderr if line.strip())
    return f' (native stderr: {text[:400]})' if text else ''


class Stream:
    def __init__(self, iterator, request):
        self.iterator, self.request = iterator, request
        self._advancing = threading.Lock()
        request.stream = weakref.ref(self)
    def __iter__(self):
        return self
    def __next__(self):
        with self._advancing:
            return next(self.iterator)
    def close(self):
        self.request.cancel()
        # An active consumer unwinds itself after cancellation. A paused/unstarted
        # generator has no active owner and can be finalized here.
        if self._advancing.acquire(blocking=False):
            try:
                self.iterator.close()
                with self.request.client._lock:
                    self.request.client._requests.discard(self.request)
            finally:
                self._advancing.release()
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.close()


class AsyncStream:
    def __init__(self, stream):
        self.stream = stream
    def __aiter__(self):
        return self
    async def __anext__(self):
        def advance():
            try:
                return True, next(self.stream)
            except StopIteration:
                return False, None
        try:
            present, item = await asyncio.to_thread(advance)
        except asyncio.CancelledError:
            self.stream.close()
            raise
        if not present:
            raise StopAsyncIteration
        return item
    async def aclose(self):
        self.stream.close()
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        await self.aclose()


def _private_dir(path):
    """Our own real directory, closed to others; hosts without a uid only get the type check."""
    info = os.lstat(path)
    return stat.S_ISDIR(info.st_mode) and (not hasattr(os, 'getuid') or (info.st_uid == os.getuid() and not info.st_mode & 0o077))


def _tighten(path):
    """Close a directory this process just created. Some filesystems hand back a fresh 0700 mkdir as
    0770, and _private_dir would then refuse our own directory on the next request, moving the cwd
    every time. One found open is never tightened or adopted. The mode changes through a descriptor
    opened without following links: a parent others can write lets them swap a symlink in after the
    mkdir, and chmod by name would then change whatever file of ours it points at. Best effort; the
    _private_dir check that follows decides."""
    if hasattr(os, 'getuid'):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return path
        try:
            os.fchmod(fd, 0o700)
        except OSError:
            pass
        finally:
            os.close(fd)
    return path


def _create_private(path):
    """Create path closed to others; False when something already sits there."""
    try:
        os.makedirs(path, mode=0o700)
    except FileExistsError:
        return False
    _tighten(path)
    return True


def shared_workdir():
    """One native cwd for every client of this OS user, or None to fall back to a private one.

    Native writes its cwd into the environment block of every request (message 1 on Opus 5.5), so a
    per-client directory moved the prompt-cache prefix whenever Hermes built a new client (#14, #43).
    The path sits in a tempdir others may share: it must be our own real directory, closed to others,
    in a parent nobody else can rename it out of. The utime keeps Hermes' 24h scratch prune off it.
    """
    if not hasattr(os, 'getuid'):
        return None
    path = Path(tempfile.gettempdir()) / f'claude-directsdk-cwd-{os.getuid()}'
    try:
        parent = path.parent.stat().st_mode
        if parent & 0o022 and not parent & stat.S_ISVTX:
            return None
        _create_private(path)
        if not _private_dir(path):
            return None
        os.utime(path)
    except OSError:
        return None
    return str(path)


_process_cwd = None
_process_cwd_lock = threading.Lock()


def process_workdir():
    """One private cwd for every client in this process when the shared one is refused.

    Hermes builds new clients for new chats, auxiliary calls and background review; a cwd per client
    moved message 1 for each of them, so none could reuse the conversation's cached prefix. The
    directory is as private as a per-client one (our mkdtemp, checked on every use); it only lives as
    long as the process. Recreated at the same path after a prune, never adopted when someone else
    recreated it.
    """
    global _process_cwd
    with _process_cwd_lock:
        if _process_cwd is not None:
            # A predictable path in a shared tempdir: never adopt one someone else recreated.
            try:
                _create_private(_process_cwd)
                ours = _private_dir(_process_cwd)
            except OSError:
                ours = False
            if ours:
                os.utime(_process_cwd)
                return _process_cwd
        _process_cwd = _tighten(tempfile.mkdtemp(prefix='claude-directsdk-cwd-'))
        atexit.register(shutil.rmtree, _process_cwd, ignore_errors=True)
        return _process_cwd


class Client:
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, command=None, args=None, env=None, timeout=180, **_):
        # Hermes snapshots routing metadata from client-shaped objects; this is not a credential.
        self.api_key = 'external-process'
        self.base_url = 'process://claude-subscription-directsdk-experimental'
        self.env = dict(env) if env is not None else None
        source_env = self.env if self.env is not None else os.environ
        command = command or source_env.get('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND') or 'claude'
        self.command = ([command] if isinstance(command, str) else list(command)) + list(args or [])
        self.timeout = timeout if isinstance(timeout, (int, float)) else 180
        self._lock, self._requests, self._closed = threading.Lock(), set(), False
        # The one native kept alive for this client (Hermes keeps a client per agent), and whether a step uses it.
        self._session, self._stepping = None, False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def cancel(self):
        """Fast cross-thread cancellation: signal owned groups; never close caller-thread FDs."""
        with self._lock:
            requests = tuple(self._requests)
        for request in requests:
            request.cancel()
        self._end_session()

    def close(self):
        with self._lock:
            self._closed = True
            requests = tuple(self._requests)
        for request in requests:
            stream = request.stream() if request.stream else None
            if stream is not None:
                stream.close()
            else:
                request.cancel()
        self._end_session()

    def _end_session(self, native=None):
        """Kill the idle kept session (only ``native`` when given: the idle timer's own)."""
        with self._lock:
            if self._session is None or (native is not None and self._session is not native):
                return
            native, self._session = self._session, None
        native.kill()
        native.release()

    def _keep(self, native, frames, expect):
        native.frames, native.expect = frames, expect
        native.idle = threading.Timer(SESSION_IDLE_S, self._end_session, args=(native,))
        native.idle.daemon = True
        native.idle.start()
        with self._lock:
            self._session, self._stepping = native, False

    def _resume(self, ident, frames):
        """The kept session and how this step continues it, else (None, None) after ending it."""
        with self._lock:
            native, self._session = self._session, None
        if native is not None:
            native.idle.cancel()
            how = native.resumes(frames) if native.ident == ident and native.settled() else None
            if how:
                return native, how
            native.kill()
            native.release()
        return None, None

    def _workdir(self):
        """The shared cwd, else the one private cwd of this process."""
        return shared_workdir() or process_workdir()

    def create(self, **kwargs):
        # Hermes' auxiliary seam returns this same object and awaits create. A running loop alone
        # does not mean the caller awaits: Hermes' NeMo Relay managed execution runs the agent's
        # *sync* provider callback inside asyncio.run, and a coroutine handed back there is never
        # awaited (TypeError in relay_llm._jsonable). Go async only for a coroutine caller.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            caller = sys._getframe(1).f_code.co_flags
            if caller & (inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE):
                return self._acreate(**kwargs)
        return self._create(**kwargs)

    async def _acreate(self, **kwargs):
        request = self._register()
        try:
            result = await asyncio.to_thread(self._create, request, **kwargs)
            return AsyncStream(result) if kwargs.get('stream') else result
        except asyncio.CancelledError:
            # Only this call: a sibling request on the shared client keeps running.
            request.cancel()
            with self._lock:
                self._requests.discard(request)
            raise

    def _register(self):
        request = Request(self)
        with self._lock:
            if self._closed:
                raise RuntimeError('Claude client is closed')
            self._requests.add(request)
        return request

    def _create(self, request=None, **kwargs):
        # Register before preparing, so a cancel during a slow history build reaches this call.
        request = request or self._register()
        try:
            body, manifest, names = request_body(kwargs)
            system, frames = prepare_history(kwargs.get('messages', []), names)
            if not isinstance(kwargs.get('model'), str) or not kwargs['model']:
                raise RequestRejected('model is required')
        except BaseException:
            with self._lock:
                self._requests.discard(request)
            raise
        stream = Stream(self._run(request, kwargs, body, manifest, names, system, frames), request)
        if kwargs.get('stream'):
            return stream
        try:
            for chunk in stream:
                if hasattr(chunk, '_response'):
                    return chunk._response
            raise RuntimeError('Native response missing')
        finally:
            stream.close()

    def _run(self, request, kwargs, body, manifest, names, system, frames):
        hermes_names = {native: hermes for hermes, native in names.items()}
        native, state, stepping = None, {}, False
        try:
            timeout = kwargs.get('timeout', self.timeout)
            timeout = getattr(timeout, 'read', timeout)
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise RequestRejected('timeout must be positive seconds')
            env = _with_login_identity(_with_windows_essentials(dict(self.env if self.env is not None else os.environ)))
            if self.env is None:
                # Explicit env= is a controlled fixture and keeps its login; only Hermes' own env is filtered.
                drop_api_credentials(env)
                conflicts = [key for key in ('ANTHROPIC_BASE_URL',) if env.get(key)]
                conflicts += [key for key in ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY') if env.get(key, '').lower() not in ('', '0', 'false', 'no', 'off')]
                if conflicts:
                    raise RequestRejected('OAuth provider refuses conflicting native auth/backend overrides: ' + ', '.join(conflicts))
            # Fail with the install hint, not a Popen FileNotFoundError, when Claude Code is absent.
            resolved = resolve_claude(self.command, env)
            if resolved is None:
                raise ClaudeCodeMissing(INSTALL_HINT)
            apply_config_dir(env)
            if self.env is None:
                # The serving profile's own token, or none when its claude_config_dir picks the login.
                apply_profile_credential(env)
            # A refresh lock an earlier native left mid-refresh would fail this request (claude-code#95236).
            reap_orphaned_refresh_lock(env)
            # Anthropic or the profile's opted-in gateway; an explicit Client(env=...) fixture may name its own.
            upstream = (Upstream(env['ANTHROPIC_BASE_URL'], None, None, False) if env.get('ANTHROPIC_BASE_URL')
                        else resolve_upstream())
            # The extra_usage setting refuses here, before anything is billed (#43).
            check_extra_usage(resolved, dict(env), upstream)
            # An inherited effort level would override the --effort Hermes passes below.
            for key in ('CLAUDE_CODE_EXTRA_BODY', 'CLAUDE_CODE_EFFORT_LEVEL'):
                env.pop(key, None)
            env.update(ENABLE_TOOL_SEARCH='false', CLAUDE_CODE_MAX_RETRIES='0', DISABLE_AUTO_COMPACT='1', DISABLE_COMPACT='1')
            # Telemetry and feature flags follow the user's claude_code_telemetry setting, read per spawn.
            apply_traffic_policy(env)
            egress_env(env, upstream)
            # Hermes owns budgets; native's replayed reminder invalidates cached history.
            env['CLAUDE_CODE_TOTAL_TOKENS_REMINDER'] = 'off'
            # Hermes owns memory and instructions. --setting-sources '' leaves auto memory and the managed
            # CLAUDE.md loading, and native would put them in the conversation as a reminder on the user's
            # turn, where Claude reads Claude Code's notes as text smuggled into the user's message (#195).
            env.update(CLAUDE_CODE_DISABLE_AUTO_MEMORY='1', CLAUDE_CODE_DISABLE_CLAUDE_MDS='1')
            # One native session per conversation: native names a per-session scratchpad directory in the
            # environment message near the top of every request, so a new session id per spawn moved the cache
            # prefix on every call while feature flags are on.
            session_id = conversation_session_id(system, frames)
            # Everything a native is launched with: a kept one serves a step only while this is unchanged.
            ident = hashlib.sha256(json.dumps([resolved, sorted(env.items()), upstream and [upstream.url, upstream.proxy],
                                               kwargs['model'], body, system, session_id], default=str).encode()).hexdigest()
            enabled, cap = session_settings()
            # An auxiliary call (summary, title) or a tool-less one is a single step: a one-shot, never kept.
            session = bool(enabled and manifest and kwargs.get('hermes_auxiliary') is not True)
            with self._lock:
                # One step at a time uses the kept native; a concurrent call on this client runs one-shot.
                session = stepping = session and not self._stepping
                self._stepping = self._stepping or session
            native, how = self._resume(ident, frames) if session else (None, None)
            while True:
                if native is None:
                    with _live_lock:
                        # At the process cap this step runs one-shot: the same bytes at the per-call cost.
                        session = session and len(_live) < cap
                    native = self._launch(request, env, resolved, upstream, kwargs, body, manifest, system, frames, timeout, session_id, session)
                    native.ident = ident
                    native.replay(request, frames, timeout)
                    if not session:
                        native.send(request, None, timeout)
                else:
                    with request.lock:
                        if request.cancelled.is_set():
                            raise RuntimeError('Claude request cancelled')
                        request.process, request.admission = native.process, native.admission
                    native.admission.rearm(timeout, frames[-1]['message']['content'], frames)
                    native.admission.eager = session_id not in _BUFFER_TOOL_INPUT
                    if how[0] == 'user':
                        native.send(request, how[1], timeout)
                    else:
                        native.deliver(how[1])
                if (yield from self._step(request, native, state, kwargs, hermes_names, session, session_id, resolved, env, frames, timeout)) != 'diverged':
                    break
                # The kept native's request would not extend its previous one; it was never forwarded. Replay instead.
                logger.info('Claude session diverged from its previous request; replaying into a new one')
                native.kill()
                native.release()
                native = None
        finally:
            request.cancel()
            outcome = state.get('outcome')
            if native is not None and outcome not in ('kept', 'handed_off'):
                native.kill()
                native.release()
            with self._lock:
                self._requests.discard(request)
                if stepping and outcome != 'kept':
                    self._stepping = False

    def _launch(self, request, env, resolved, upstream, kwargs, body, tools, system, frames, timeout, session_id, session):
        """Start native for one step: a session (kept for the next step) or a one-shot (ends after this one)."""
        queried = frames[-1]['message']['content']
        # The queried frame lets the relay keep the cache breakpoint off native's per-request context.
        admission = relay(upstream, timeout, queried=queried)
        # A Hermes auxiliary one-shot (summary, title: marked by the profile hook, one turn, no tools) is
        # never replayed, so the relay drops its message cache marks instead of moving them.
        admission.one_shot = kwargs.get('hermes_auxiliary') is True and not json.loads(body)['tools'] and len(frames) == 1
        admission.eager = session_id not in _BUFFER_TOOL_INPUT
        if session:
            # The relay keeps a kept native's requests byte-identical to what a fresh replay sends.
            admission.frames = frames
        with request.lock:
            request.admission = admission
        env = dict(env, ANTHROPIC_BASE_URL=admission.url)
        if session:
            # A parked call waits as long as Hermes runs the tool; Hermes bounds tool output itself.
            env.update(MCP_TOOL_TIMEOUT='86400000', MAX_MCP_OUTPUT_TOKENS='100000000')
        root = _launch_dir()
        try:
            # Native settings apply env inside the process, avoiding execve's
            # per-argument/environment-string limit for full Hermes schemas.
            (root / 'settings.json').write_text(json.dumps({'env': {'CLAUDE_CODE_EXTRA_BODY': body}}), encoding='utf-8')
            (root / 'system.md').write_text(system, encoding='utf-8')
            parsed = json.loads(body)
            if 'max_tokens' in parsed:
                env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = str(parsed['max_tokens'])
            # Native's built-ins are off. A session serves Hermes' tools as the SDK MCP server "hermes": native asks
            # for each call and waits for Hermes' result, so it never runs one. A one-shot mounts no server (the
            # tools still go out in the extra body, byte for byte the same), dontAsk denies the call locally, and
            # native stops after one model step, where it reports the step's list price.
            mcp = {'hermes': {'type': 'sdk', 'name': 'hermes'}} if session else {}
            limit = ['--allowedTools', 'mcp__hermes'] if session else ['--max-turns', '1']
            # The resolved path matters on Windows: CreateProcess finds claude.exe on PATH but not the npm claude.cmd shim.
            command = resolved + ['-p', '--model', native_model(kwargs['model']), '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--tools', '', '--system-prompt-file', str(root / 'system.md'), '--settings', str(root / 'settings.json'), '--setting-sources', '', '--strict-mcp-config', '--disable-slash-commands', *limit, '--permission-mode', 'dontAsk', '--no-session-persistence', '--mcp-config', json.dumps({'mcpServers': mcp})]
            # Native appends a per-turn effort message at its own level (the CLI default unless
            # --effort is given), which overrides the top-level output_config.effort from the extra body.
            effort = parsed.get('output_config', {}).get('effort')
            if effort:
                command += ['--effort', effort]
            command += ['--session-id', session_id]
            native = Native(request, command, env, self._workdir(), admission, root, tools if session else None)
            if session:
                with _live_lock:
                    _live.add(native)
            return native
        except BaseException:
            admission.close()
            shutil.rmtree(root, ignore_errors=True)
            raise

    def _step(self, request, native, state, kwargs, hermes_names, session, session_id, resolved, env, frames, timeout):
        """Stream one model step. ``state['outcome']`` is set before the final chunk: 'kept', 'handed_off' or 'done'.
        Returns 'diverged' when the relay refused a kept native's request before it went upstream."""
        admission = native.admission
        assistants, results, stopped, emitted = [], [], False, ''
        rate_limit = None
        tool_stream = _ToolStream(hermes_names)
        native_error = native_error_code = None
        parked = False
        while True:
            event = native.receive(request, timeout)
            if event is None:
                break
            kind = event.get('type')
            if kind == 'assistant':
                if event.get('error') or event.get('message', {}).get('error'):
                    detail = '\n'.join(b.get('text', '') for b in event.get('message', {}).get('content', []) if b.get('type') == 'text')
                    native_error, native_error_code = detail, event.get('error')
                else:
                    assistants.append(event['message'])
            elif kind == 'result':
                # Native's verdict on the step: the answer is complete, and its exit is never waited for.
                results.append(event)
                break
            elif kind == 'rate_limit_event':
                # Native reports the subscription window state in-stream;
                # dropping it leaves quota policy (#43) with no signal.
                summary = _rate_limit_summary(event)
                if summary:
                    rate_limit = summary
                    observe_rate_limit(env, summary)
            elif kind == 'stream_event':
                event = event['event']
                if event['type'] == 'message_stop':
                    stopped = True
                    # A session's tool step ends here: native now asks for each call over MCP and Hermes answers next
                    # step (a call it cannot parse it answers itself, then asks the relay again, which refuses).
                    capture = admission.capture
                    # Native dispatches every captured call, whatever the stop reason (max_tokens after a complete call).
                    if session and admission.used and capture.complete and (capture.unparsed or any(
                            b.get('type') == 'tool_use' for b in capture.message['content'])):
                        parked = True
                        break
                delta = event.get('delta', {})
                tool_deltas = tool_stream.deltas(event)
                if tool_deltas:
                    yield self._chunk(kwargs['model'], {'tool_calls': tool_deltas})
                elif delta.get('type') == 'text_delta':
                    emitted += delta['text']
                    yield self._chunk(kwargs['model'], {'content': delta['text']})
                elif delta.get('type') == 'thinking_delta':
                    yield self._chunk(kwargs['model'], {'reasoning_content': delta['thinking']})
                elif event['type'] not in ('message_stop', 'ping'):
                    # The model is working but has nothing to show: tool-call arguments (input_json_delta),
                    # thinking signatures, block boundaries. Hermes' stale detector only sees chunks, so
                    # without this a long tool call looks like a stall and the host kills a healthy request.
                    # A ping stays silent: it proves the connection is open, not that the model is working.
                    yield self._chunk(kwargs['model'], {})
        final = results[0] if results else {}
        fast = bool(results) and not session and _fast_eligible(final, admission, stopped, native_error)
        returncode = 0
        if not (session or fast):
            # Not a clean admitted answer: native's own exit decides, as it always has.
            with suppress(subprocess.TimeoutExpired):
                native.process.wait(timeout=timeout)
            native.reader.join(timeout=1)
            native.err_drain.join(timeout=1)
            returncode = native.process.returncode
        if request.cancelled.is_set():
            raise RuntimeError('Claude request cancelled')
        # A kept native refused by the relay, or gone before this step reached it: replay into a new one.
        if admission.diverged or (native.frames is not None and not admission.used and not assistants and not results):
            return 'diverged'
        if admission.used:
            if admission.status != 200 or not admission.capture.complete:
                # Native's last error is the admission denial; name the first attempt's outcome so reports are diagnosable.
                first = f'first upstream attempt: status {admission.status}, capture ' + ('complete' if admission.capture.complete else 'incomplete') + (f', relay failure {admission.failure}' if admission.failure else '') + f', native retries denied: {admission.denied}'
                if admission.error_text():
                    first += ', upstream said: ' + admission.error_text()[:500]
                # The first attempt's real HTTP status; a 200 cut short or a relay failure has none.
                status = admission.status if isinstance(admission.status, int) and admission.status >= 400 else None
                raise ClaudeAPIError(f'Incomplete upstream response ({first})' + (': ' + native_error if native_error else ''),
                                     status, request_id=admission.request_id)
            assistants = [admission.capture.message]
            stopped = True
        native_failure_handled = admission.denied or (admission.used and assistants[0].get('stop_reason') == 'refusal')
        if native_error and not native_failure_handled:
            if native_error_code == 'authentication_failed' and not admission.used:
                # No usable login where Hermes runs native: it refuses before any upstream request; only its /api/hello pre-flight reaches the relay.
                # Name what was tried: bot surfaces resolve a different environment than the user's shell (#47).
                # The child's own HOME, not Hermes': that is where native looked for ~/.claude.
                home = env.get('HOME') or env.get('USERPROFILE')
                login = env.get('CLAUDE_CONFIG_DIR') or str((Path(home) if home else Path.home()) / '.claude')
                native.err_drain.join(timeout=1)
                raise ClaudeCodeLoggedOut(f'{LOGGED_OUT_HINT} (native: {native_error}; cli: {resolved[0]}; login: {login}){_stderr_tail(native.stderr)}')
            raise ClaudeAPIError('Native API error: ' + native_error, NATIVE_ERROR_STATUS.get(str(native_error_code)))
        if not assistants or not stopped or not (parked or len(results) == 1):
            native.err_drain.join(timeout=1)
            raise RuntimeError('Incomplete native response: assistant, message_stop and one result required' + _stderr_tail(native.stderr))
        blocks = [b for a in assistants for b in a['content']]
        # Native routes only calls named as offered (mcp__hermes__<tool>) to Hermes over MCP; it answers any other itself.
        routed = all(b['name'].removeprefix(PREFIX) in hermes_names and b['name'].startswith(PREFIX) for b in blocks if b.get('type') == 'tool_use')
        calls = []
        for block in blocks:
            if block.get('type') == 'tool_use':
                block['name'], name = offered_tool_name(block['name'], hermes_names)
                calls.append({'id': block['id'], 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(block['input'], separators=(',', ':'), allow_nan=False)}})
        refused = any(a.get('stop_reason') == 'refusal' for a in assistants)
        parsed_calls = list(calls)
        # A call whose arguments did not parse (cut at max_tokens, or invalid streamed JSON) reaches Hermes with
        # its raw arguments and finish length: Hermes never runs a call from a length reply, it re-sends the
        # request with a larger output cap (#164, #221). A refused call stays out (below).
        unparsed = [] if refused or not admission.used else admission.capture.unparsed_calls
        for call_id, native_name, raw in unparsed:
            calls.append({'id': call_id, 'type': 'function', 'function': {'name': offered_tool_name(native_name or '', hermes_names)[1], 'arguments': raw}})
        if unparsed and not any(a.get('stop_reason') == 'max_tokens' for a in assistants):
            # Not a cut: the API streamed invalid JSON. This conversation's next requests buffer and validate.
            _buffer_tool_input(session_id)
        # A one-shot's tool step ends at --max-turns 1 (error_max_turns, exit 1) after native answers the call
        # locally (no such tool), or one it cannot parse; either way a complete step.
        boundary = (bool(calls) or bool(admission.capture.unparsed)) and final.get('subtype') == 'error_max_turns' and (fast or session or returncode == 1)
        if not (parked or boundary or native_failure_handled) and (returncode != 0 or final.get('is_error') or final.get('subtype') != 'success'):
            raise RuntimeError('Native request failed: ' + str(final.get('subtype')) + _stderr_tail(native.stderr))
        usage = assistants[0]['usage'] if admission.used else final.get('usage')
        if not isinstance(usage, dict) or not all(isinstance(usage.get(k), (int, float)) for k in ('input_tokens', 'output_tokens')):
            raise RuntimeError('Native result missing complete token usage')
        # Native reports list price per process, cumulatively, and only when a turn ends: a tool step has none of its
        # own, and the turn's last step carries what the process spent since it last reported.
        spent = final.get('total_cost_usd')
        if isinstance(spent, (int, float)) and not isinstance(spent, bool):
            spent, native.cost = spent - native.cost, spent
        text = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
        if emitted != text:
            if text.startswith(emitted):
                yield self._chunk(kwargs['model'], {'content': text[len(emitted):]})
            else:
                raise RuntimeError('Native final text differs from incremental stream')
        thinking = ''.join(b.get('thinking', '') for b in blocks if b.get('type') == 'thinking') or None
        # A turn of thinking alone (no text, no tool call) is Claude stalling, not answering. Hermes promotes a
        # reasoning-only `stop` to the final answer, which ends the tool loop; left empty, its empty-response
        # recovery nudges the model on, as before summarized display. The signed thinking still replays.
        message = {'role': 'assistant', 'content': text or None, 'tool_calls': calls or None,
                   'reasoning_content': thinking if text or calls else None}
        carrier = {'type': CARRIER, 'version': 1, 'messages': assistants, 'projection': projection({**message, 'tool_calls': parsed_calls or None})}
        message['reasoning_details'] = [carrier]
        # An upstream refusal is Hermes' content_filter, as core's Anthropic transport maps it (even with a tool
        # call cut off by the classifier, which must not run): terminal, one fallback try, never retried as an
        # empty reply. Its reason rides stop_details, not a content block.
        if refused:
            details = next((a['stop_details'] for a in assistants if isinstance(a.get('stop_details'), dict)), {})
            explanation, category = details.get('explanation'), details.get('category')
            reason = (explanation.strip() if isinstance(explanation, str) and explanation.strip() else
                      f'provider refusal category: {category}' if isinstance(category, str) and category else None)
            # The request id is what Anthropic support needs to look a refusal up (#341).
            request_id = _request_id(admission.request_id)
            logger.warning('Claude refused the request: model=%s request_id=%s category=%s', kwargs['model'], request_id, category)
            message['refusal'] = f'{reason or "provider refusal"} (request_id={request_id})' if request_id else reason
            # The cut-off call stays out of what Hermes acts on: Hermes promotes a refusal to the text it shows
            # only when nothing else is in the message, so with the call kept the user read "no explanation".
            # The carrier above still holds native's turn as sent, refused tool_use included.
            message['tool_calls'], calls = None, []
        inp = usage['input_tokens'] + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
        normalized_usage = {'prompt_tokens': inp, 'completion_tokens': usage['output_tokens'], 'total_tokens': inp + usage['output_tokens'], 'prompt_tokens_details': {'cached_tokens': usage.get('cache_read_input_tokens', 0)}, 'cache_creation_input_tokens': usage.get('cache_creation_input_tokens', 0), 'native_usage': usage,
                            'completion_tokens_details': {'reasoning_tokens': usage.get('output_tokens_details', {}).get('thinking_tokens', 0)},
                            'native_cost': {'total_cost_usd': spent, 'modelUsage': final.get('modelUsage')}}
        normalized_usage['native_admission'] = {'upstream_requests': int(admission.used), 'blocked_requests': admission.denied, 'request_id': admission.request_id}
        if rate_limit:
            normalized_usage['native_admission']['rate_limit'] = rate_limit
        if admission.unrestored:
            normalized_usage['native_admission']['unrestored'] = admission.unrestored
        finish = 'content_filter' if refused else 'length' if unparsed else 'tool_calls' if calls else ('length' if any(a.get('stop_reason') in ('max_tokens', 'model_context_window_exceeded') for a in assistants) else 'stop')
        response = obj({'id': assistants[-1].get('id', 'claude-native'), 'model': kwargs['model'], 'object': 'chat.completion', 'choices': [{'index': 0, 'finish_reason': finish, 'message': message}], 'usage': normalized_usage})
        # Calls already streamed are not repeated; a refused one gets only what makes its arguments parse.
        late = tool_stream.close_refused() if refused else [
            dict(tc, index=len(tool_stream.slots) + i) for i, tc in enumerate(c for c in calls if not tool_stream.streamed(c['id']))]
        chunk = self._chunk(kwargs['model'], {'content': None, 'tool_calls': late or None, 'reasoning_details': [carrier], **({'refusal': message['refusal']} if refused else {})}, finish, normalized_usage)
        chunk._response = response
        # Fast return removes these per-request files while native drains: safe on POSIX;
        # ignore_cleanup_errors already tolerates files still held open on Windows.
        # Kept only when native holds exactly the history Hermes will send next: a clean step through the relay, every
        # call parked on Hermes, nothing native answered itself.
        clean = (session and admission.used and not admission.denied and not native_error and not refused and not unparsed
                 and (routed if parked else final.get('subtype') == 'success' and not calls))
        state['outcome'] = 'kept' if clean else 'handed_off' if fast else 'done'
        if fast:
            # The answer is complete at native's result: its exit is never waited for; the drain kills a native
            # that will not exit.
            with request.lock:
                request.process = request.admission = None
            _hand_off(native)
        if clean:
            with request.lock:
                request.process = request.admission = None
            reply = {k: v for k, v in copy.deepcopy(assistants[0]).items() if k != 'usage'}
            self._keep(native, frames + [{'type': 'assistant', 'message': reply}],
                       frozenset(c['id'] for c in calls) if parked else 'user')
        native.forget_launch()
        yield chunk
    @staticmethod
    def _chunk(model, delta, finish=None, usage=None):
        return obj({'id': 'claude-native', 'model': model, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': None, 'tool_calls': None, 'reasoning_details': None, **delta}, 'finish_reason': finish}], 'usage': usage})
