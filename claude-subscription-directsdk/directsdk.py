"""Request-scoped Claude Code transport with host-owned HTTP admission."""
from __future__ import annotations

import asyncio
import atexit
import copy
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
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from types import SimpleNamespace

try:
    from .admission import Admission
    from .model_catalog import accepts_thinking_disable, native_model, supports_adaptive_thinking
    from .directsdk_setup import INSTALL_HINT, LOGGED_OUT_HINT, QUIET_TRAFFIC, _resolve as resolve_claude, apply_traffic_policy
except ImportError:
    from admission import Admission
    from model_catalog import accepts_thinking_disable, native_model, supports_adaptive_thinking
    from directsdk_setup import INSTALL_HINT, LOGGED_OUT_HINT, QUIET_TRAFFIC, _resolve as resolve_claude, apply_traffic_policy


# Hermes picks retry vs fallback from an error's status_code (main loop and auxiliary ladder alike).
class ClaudeCodeMissing(RuntimeError):
    """The official Claude Code CLI this transport drives is not installed (or not on PATH)."""
    status_code = 503


class ClaudeCodeLoggedOut(RuntimeError):
    """Claude Code refused before any upstream request because it has no usable login where Hermes runs it."""
    status_code = 401


class ClaudeAPIError(RuntimeError):
    """A failed upstream or native-answered request; status_code is None when there is no HTTP equivalent."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


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
                raise ValueError('Only base64 data image_url inputs are supported')
            media, data = url[5:].split(';base64,', 1)
            result.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': data}})
        elif kind in ('image', 'document', 'tool_result'):
            result.append(copy.deepcopy(block))
        else:
            raise ValueError(f'Unsupported content block: {kind}')
    return result


def prepare_history(messages, names=None):
    system, frames = [], []
    for message in messages:
        role = message.get('role')
        if role in ('system', 'developer'):
            if frames:
                raise ValueError('System/developer messages must precede conversation history')
            if not isinstance(message.get('content'), str):
                raise ValueError('System content must be text')
            system.append(message['content'])
            continue
        if role == 'assistant':
            details = message.get('reasoning_details') or []
            carriers = [d for d in details if isinstance(d, dict) and d.get('type') == CARRIER]
            if carriers:
                if len(carriers) != 1 or carriers[0].get('version') != 1:
                    raise ValueError('Unsupported native assistant carrier version')
                carrier = carriers[0]
                expected = {**carrier['projection'], 'content': carrier['projection']['content'].strip()}
                if projection(message) == expected:
                    for native in carrier['messages']:
                        frames.append({'type': 'assistant', 'message': copy.deepcopy(native)})
                    continue
                # Host compaction/hooks own visible history. Never restore stale
                # pre-edit blocks or attach their signatures to rewritten content.
            blocks = content_blocks(message.get('content'))
            for call in projection(message)['tool_calls']:
                # Inverse of the result mapping: a call outside the inventory (#39) replays as the model named it.
                name = PREFIX + call['name'] if names is None or call['name'] in names else call['name']
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
            raise ValueError(f'Unsupported message role: {role}')
        if frames and frames[-1]['type'] == role and role == 'user':
            frames[-1]['message']['content'].extend(blocks)
        else:
            frames.append({'type': role, 'message': {'role': role, 'content': blocks}})
    if not frames or frames[-1]['type'] != 'user' or not frames[-1]['message']['content']:
        raise ValueError('History must end in a nonempty user/tool-result message; assistant prefill is unsupported')
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


def request_body(kwargs):
    allowed = {'model', 'messages', 'tools', 'stream', 'stream_options', 'max_tokens', 'max_completion_tokens',
               'temperature', 'top_p', 'stop', 'extra_body', 'extra_headers', 'timeout', 'tool_choice', 'parallel_tool_calls', 'n', 'response_format'}
    unknown = set(kwargs) - allowed
    if unknown:
        raise ValueError('Unsupported request parameters: ' + ', '.join(sorted(unknown)))
    # Hermes Relay attaches host tracing to chat-completions clients. Keep it host-local:
    # never project it into generation fields or override the official CLI's identity.
    headers = kwargs.get('extra_headers')
    if headers is not None and (not isinstance(headers, dict) or any(
            not isinstance(key, str) or key.lower() != 'traceparent' or not isinstance(value, str)
            for key, value in headers.items())):
        raise ValueError('extra_headers supports host traceparent metadata only')
    if kwargs.get('n', 1) != 1 or kwargs.get('tool_choice', 'auto') not in ('auto', None):
        raise ValueError('Only n=1 and tool_choice=auto are supported')
    if kwargs.get('parallel_tool_calls') is False:
        raise ValueError('parallel_tool_calls=False is unsupported')
    if kwargs.get('stream_options') not in (None, {}, {'include_usage': True}, {'include_usage': False}):
        raise ValueError('Unsupported stream_options')
    extra = kwargs.get('extra_body')
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise ValueError('extra_body must be an object')
    unknown_extra = set(extra) - {'max_tokens', 'temperature', 'top_p', 'stop_sequences', 'reasoning', 'response_format'}
    if unknown_extra:
        raise ValueError('Unsupported extra_body fields: ' + ', '.join(sorted(unknown_extra)))
    body = copy.deepcopy(extra)
    reasoning = body.pop('reasoning', None)
    if reasoning is not None:
        if not isinstance(reasoning, dict) or set(reasoning) - {'enabled', 'effort'}:
            raise ValueError('reasoning supports enabled and effort only')
        if 'enabled' in reasoning and type(reasoning['enabled']) is not bool:
            raise ValueError('reasoning.enabled must be boolean')
        from agent.reasoning_effort import clamp_effort
        effort = clamp_effort(reasoning.get('effort'), ('none', 'low', 'medium', 'high', 'xhigh', 'max'))
        if effort not in (None, 'none', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise ValueError('Unsupported native reasoning effort')
        if reasoning.get('enabled') is False or effort == 'none':
            if accepts_thinking_disable(kwargs.get('model')):
                body['thinking'] = {'type': 'disabled'}
                # Native clear-thinking context edits are invalid when thinking is disabled.
                body['context_management'] = {'edits': []}
        else:
            # Routes without adaptive thinking (Haiku 4.5) 400 on the block; their own
            # default thinking plus the effort signal below stand in for it.
            if reasoning.get('enabled') is True and supports_adaptive_thinking(kwargs.get('model')):
                body['thinking'] = {'type': 'adaptive'}
            if effort:
                body['output_config'] = {'effort': effort}
    response_format = kwargs.get('response_format', body.pop('response_format', None))
    if response_format and response_format.get('type') != 'text':
        if response_format.get('type') != 'json_schema':
            raise ValueError('Only json_schema structured output is supported')
        schema = response_format.get('json_schema', {}).get('schema')
        if not isinstance(schema, dict):
            raise ValueError('response_format requires a JSON Schema object')
        body.setdefault('output_config', {})['format'] = {'type': 'json_schema', 'schema': schema}
    for key in ('max_tokens', 'temperature', 'top_p'):
        if kwargs.get(key) is not None:
            body[key] = kwargs[key]
    if kwargs.get('max_completion_tokens') is not None:
        if 'max_tokens' in body:
            raise ValueError('Specify only one output-token limit')
        body['max_tokens'] = kwargs['max_completion_tokens']
    if kwargs.get('stop') is not None:
        stop = kwargs['stop']
        body['stop_sequences'] = [stop] if isinstance(stop, str) else stop
    for key in ('temperature', 'top_p'):
        if key in body and (isinstance(body[key], bool) or not isinstance(body[key], (int, float)) or not math.isfinite(body[key]) or not 0 <= body[key] <= 1):
            raise ValueError(f'{key} must be finite and between zero and one')
        # Subscription models reject sampling controls, including Hermes' title
        # generator default. Match the host's sampling-forbidden model behavior.
        body.pop(key, None)
    if 'max_tokens' in body and (type(body['max_tokens']) is not int or body['max_tokens'] < 1):
        raise ValueError('max_tokens must be a positive integer')
    if 'stop_sequences' in body and (not isinstance(body['stop_sequences'], list) or not all(isinstance(x, str) and x for x in body['stop_sequences'])):
        raise ValueError('stop_sequences must be a list of nonempty strings')
    manifest, tools, names = [], [], set()
    for tool in kwargs.get('tools') or []:
        if tool.get('type') != 'function':
            raise ValueError('Only function tools are supported')
        f = tool['function']
        name = f['name']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', name) or name in names:
            raise ValueError('Tool names must be unique ASCII identifiers of at most 50 characters')
        if f.get('strict'):
            raise ValueError('Strict function schemas are unsupported')
        names.add(name)
        schema, description = f.get('parameters', {'type': 'object'}), f.get('description', '')
        if not isinstance(schema, dict) or not isinstance(description, str):
            raise ValueError('Tool schema must be an object and description a string')
        # The manifest (inert MCP server) and the request body must advertise the same shape.
        schema = normalize_input_schema(schema)
        manifest.append({'name': name, 'description': description, 'inputSchema': schema})
        tools.append({'name': PREFIX + name, 'description': description, 'input_schema': schema})
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
            self.process = subprocess.Popen(command, stdin=stdin, **kwargs, **_own_process_group())
        return self.process


def _own_process_group():
    """Popen kwargs that put native (and the node/cmd children it spawns) in a group we can kill as one."""
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def kill_process_tree(process):
    """Kill native and every descendant: the npm shim is cmd.exe -> node on Windows, and a plain
    Popen.kill() would orphan the node child that holds the real request open."""
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)  # windows-footgun: ok — the nt branch above never reaches this line
    except (ProcessLookupError, PermissionError):
        # ESRCH: already gone. EPERM: macOS answers killpg with EPERM once the group leader is a zombie.
        pass


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
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def cancel(self):
        """Fast cross-thread cancellation: signal owned groups; never close caller-thread FDs."""
        with self._lock:
            requests = tuple(self._requests)
        for request in requests:
            request.cancel()
        # A conversation parked on tool calls this client returned is cancelled too, never reused.
        _warm_cancel_owned(self)

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
        task = asyncio.create_task(asyncio.to_thread(self._create, **kwargs))
        try:
            result = await task
            return AsyncStream(result) if kwargs.get('stream') else result
        except asyncio.CancelledError:
            self.cancel()
            raise

    def _create(self, **kwargs):
        body, manifest, names = request_body(kwargs)
        system, frames = prepare_history(kwargs.get('messages', []), names)
        if not isinstance(kwargs.get('model'), str) or not kwargs['model']:
            raise ValueError('model is required')
        request = Request(self)
        with self._lock:
            if self._closed:
                raise RuntimeError('Claude client is closed')
            self._requests.add(request)
        # Warm mode serves tool-bearing agent steps only; auxiliary one-shots (titles, summaries) stay per-call.
        warm = kwargs.get('tools') and _warm_enabled(self.env if self.env is not None else os.environ)
        run = self._run_warm if warm else self._run
        stream = Stream(run(request, kwargs, body, manifest, names, system, frames), request)
        if kwargs.get('stream'):
            return stream
        try:
            for chunk in stream:
                if hasattr(chunk, '_response'):
                    return chunk._response
            raise RuntimeError('Native response missing')
        finally:
            stream.close()

    def _native_env(self):
        env = _with_windows_essentials(dict(self.env if self.env is not None else os.environ))
        if self.env is None:
            conflicts = [key for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_FOUNDRY_API_KEY') if env.get(key)]
            conflicts += [key for key in ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY') if env.get(key, '').lower() not in ('', '0', 'false', 'no', 'off')]
            if conflicts:
                raise ValueError('OAuth provider refuses conflicting native auth/backend overrides: ' + ', '.join(conflicts))
        # Fail with the install hint, not a Popen FileNotFoundError, when Claude Code is absent.
        resolved = resolve_claude(self.command, env)
        if resolved is None:
            raise ClaudeCodeMissing(INSTALL_HINT)
        config = env.pop('CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR', None)
        if config:
            env['CLAUDE_CONFIG_DIR'] = config
        # An inherited effort level would override the --effort Hermes passes below.
        for key in ('CLAUDE_CODE_EXTRA_BODY', 'CLAUDE_CODE_EFFORT_LEVEL'):
            env.pop(key, None)
        env.update(ENABLE_TOOL_SEARCH='false', CLAUDE_CODE_MAX_RETRIES='0', DISABLE_AUTO_COMPACT='1', DISABLE_COMPACT='1')
        # Telemetry and feature flags follow the user's claude_code_telemetry setting, read per spawn.
        apply_traffic_policy(env)
        # Hermes owns budgets; native's replayed reminder invalidates cached history.
        env['CLAUDE_CODE_TOTAL_TOKENS_REMINDER'] = 'off'
        return env, resolved

    def _run(self, request, kwargs, body, manifest, names, system, frames):
        p = None
        reader = None
        try:
            timeout = kwargs.get('timeout', self.timeout)
            timeout = getattr(timeout, 'read', timeout)
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValueError('timeout must be positive seconds')
            # Per-request files only; native runs in the stable cwd from _workdir. Windows stragglers
            # can still hold these open for a moment, and cleanup must not fail the request.
            with tempfile.TemporaryDirectory(prefix='claude-directsdk-', ignore_cleanup_errors=True) as tmp:
                root = Path(tmp)
                (root / 'tools.json').write_text(json.dumps(manifest), encoding='utf-8')
                mcp = {'mcpServers': {'hermes': {'command': sys.executable, 'args': [str(Path(__file__).with_name('inert_mcp.py')), str(root / 'tools.json')]}}}
                env, resolved = self._native_env()
                # The queried frame lets the relay keep the cache breakpoint off native's per-request context.
                request.admission = Admission(env.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com'), timeout, queried=frames[-1]['message']['content'])
                env['ANTHROPIC_BASE_URL'] = request.admission.url
                # Native settings apply env inside the process, avoiding execve's
                # per-argument/environment-string limit for full Hermes schemas.
                (root / 'settings.json').write_text(json.dumps({'env': {'CLAUDE_CODE_EXTRA_BODY': body}}), encoding='utf-8')
                (root / 'system.md').write_text(system, encoding='utf-8')
                parsed = json.loads(body)
                if 'max_tokens' in parsed:
                    env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = str(parsed['max_tokens'])
                # The resolved path matters on Windows: CreateProcess finds claude.exe on PATH but not the npm claude.cmd shim.
                command = resolved + ['-p', '--model', native_model(kwargs['model']), '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--tools', '', '--system-prompt-file', str(root / 'system.md'), '--settings', str(root / 'settings.json'), '--setting-sources', '', '--strict-mcp-config', '--disable-slash-commands', '--max-turns', '1', '--permission-mode', 'dontAsk', '--no-session-persistence', '--mcp-config', json.dumps(mcp)]
                # Native appends a per-turn effort message at its own level (the CLI default unless
                # --effort is given), which overrides the top-level output_config.effort from the extra body.
                effort = parsed.get('output_config', {}).get('effort')
                if effort:
                    command += ['--effort', effort]
                p = request.spawn(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', cwd=self._workdir(), env=env)
                events = queue.Queue()
                def read():
                    try:
                        for line in p.stdout:
                            events.put(json.loads(line))
                    except Exception as error:
                        events.put(error)
                    finally:
                        # The consumer may close while paused at a yielded chunk.
                        # Reaping belongs to this owner thread, never cancel().
                        p.wait()
                        events.put(None)
                reader = threading.Thread(target=read, daemon=True)
                reader.start()
                deadline = time.monotonic() + timeout
                def receive():
                    nonlocal deadline
                    while True:
                        if request.cancelled.is_set():
                            raise RuntimeError('Claude request cancelled')
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Claude request timed out')
                        try:
                            event = events.get(timeout=min(remaining, .2))
                        except queue.Empty:
                            continue
                        if isinstance(event, Exception):
                            # The offending stdout line is the whole diagnosis (a shim banner, a stray print); keep it.
                            raise RuntimeError('Invalid native stream-json output: ' + repr((getattr(event, 'doc', None) or str(event))[:300])) from event
                        deadline = time.monotonic() + timeout
                        return event
                for index, frame in enumerate(frames):
                    frame = copy.deepcopy(frame)
                    if frame['type'] == 'user' and index < len(frames) - 1:
                        frame['shouldQuery'] = False
                    p.stdin.write(json.dumps(frame, allow_nan=False) + '\n')
                    p.stdin.flush()
                    if frame.get('shouldQuery') is False:
                        while True:
                            ack = receive()
                            if ack is None:
                                raise RuntimeError('Native exited before replay acknowledgment')
                            if ack.get('type') == 'result':
                                if ack.get('num_turns') != 0 or ack.get('is_error'):
                                    raise RuntimeError('Native history replay not supported: expected zero-turn acknowledgment')
                                break
                p.stdin.close()
                assistants, results, stopped, emitted = [], [], False, ''
                native_error = native_error_code = None
                while True:
                    event = receive()
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
                        results.append(event)
                    elif kind == 'stream_event':
                        native = event['event']
                        if native['type'] == 'message_stop':
                            stopped = True
                        delta = native.get('delta', {})
                        if delta.get('type') == 'text_delta':
                            emitted += delta['text']
                            yield self._chunk(kwargs['model'], {'content': delta['text']})
                        elif delta.get('type') == 'thinking_delta':
                            yield self._chunk(kwargs['model'], {'reasoning_content': delta['thinking']})
                p.wait(timeout=max(.1, deadline-time.monotonic()))
                reader.join(timeout=1)
                if request.cancelled.is_set():
                    raise RuntimeError('Claude request cancelled')
                admission = request.admission
                if admission.used:
                    if admission.status != 200 or not admission.capture.complete:
                        # Native's last error is the admission denial; name the first attempt's outcome so reports are diagnosable.
                        first = f'first upstream attempt: status {admission.status}, capture ' + ('complete' if admission.capture.complete else 'incomplete') + (f', relay failure {admission.failure}' if admission.failure else '') + f', native retries denied: {admission.denied}'
                        if admission.error_text():
                            first += ', upstream said: ' + admission.error_text()[:500]
                        # The first attempt's real HTTP status; a 200 cut short or a relay failure has none.
                        status = admission.status if isinstance(admission.status, int) and admission.status >= 400 else None
                        raise ClaudeAPIError(f'Incomplete upstream response ({first})' + (': ' + native_error if native_error else ''), status)
                    assistants = [admission.capture.message]
                    stopped = True
                native_failure_handled = admission.denied or (admission.used and assistants[0].get('stop_reason') == 'refusal')
                if native_error and not native_failure_handled:
                    if native_error_code == 'authentication_failed' and not admission.used:
                        # No usable login where Hermes runs native: it refuses before any upstream request; only its /api/hello pre-flight reaches the relay.
                        raise ClaudeCodeLoggedOut(f'{LOGGED_OUT_HINT} (native: {native_error})')
                    raise ClaudeAPIError('Native API error: ' + native_error, NATIVE_ERROR_STATUS.get(str(native_error_code)))
                if len(results) != 1 or not assistants or not stopped:
                    raise RuntimeError('Incomplete native response: assistant, message_stop and one result required')
                final = results[0]
                blocks = [b for a in assistants for b in a['content']]
                calls = []
                for block in blocks:
                    if block.get('type') == 'tool_use':
                        # Native built-ins are off (--tools '', inert MCP, dontAsk), so any other name is
                        # the model's, e.g. a Tool Search-deferred tool called directly (#39). Hermes owns
                        # validation and answers an unknown name with a recoverable error.
                        if block['name'] in names:
                            # Native sometimes drops the prefix on a tool it was offered (#62); carry the
                            # offered name so replayed history does not teach the slip back to the model.
                            block['name'] = PREFIX + block['name']
                        name = block['name'].removeprefix(PREFIX)
                        calls.append({'id': block['id'], 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(block['input'], separators=(',', ':'), allow_nan=False)}})
                boundary = bool(calls) and final.get('subtype') == 'error_max_turns' and p.returncode == 1
                if not boundary and not native_failure_handled and (p.returncode != 0 or final.get('is_error') or final.get('subtype') != 'success'):
                    raise RuntimeError('Native request failed: ' + str(final.get('subtype')))
                usage = assistants[0]['usage'] if admission.used else final.get('usage')
                if not isinstance(usage, dict) or not all(isinstance(usage.get(k), (int, float)) for k in ('input_tokens', 'output_tokens')):
                    raise RuntimeError('Native result missing complete token usage')
                text = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
                if emitted != text:
                    if text.startswith(emitted):
                        yield self._chunk(kwargs['model'], {'content': text[len(emitted):]})
                    else:
                        raise RuntimeError('Native final text differs from incremental stream')
                message = {'role': 'assistant', 'content': text or None, 'tool_calls': calls or None,
                           'reasoning_content': ''.join(b.get('thinking', '') for b in blocks if b.get('type') == 'thinking') or None}
                carrier = {'type': CARRIER, 'version': 1, 'messages': assistants, 'projection': projection(message)}
                message['reasoning_details'] = [carrier]
                # An upstream refusal is Hermes' content_filter, as core's Anthropic transport maps it (even with a tool
                # call cut off by the classifier, which must not run): terminal, one fallback try, never retried as an
                # empty reply. Its reason rides stop_details, not a content block.
                refused = any(a.get('stop_reason') == 'refusal' for a in assistants)
                if refused:
                    details = next((a['stop_details'] for a in assistants if isinstance(a.get('stop_details'), dict)), {})
                    explanation, category = details.get('explanation'), details.get('category')
                    message['refusal'] = (explanation.strip() if isinstance(explanation, str) and explanation.strip() else
                                          f'provider refusal category: {category}' if isinstance(category, str) and category else None)
                    # The cut-off call stays out of what Hermes acts on: Hermes promotes a refusal to the text it shows
                    # only when nothing else is in the message, so with the call kept the user read "no explanation".
                    # The carrier above still holds native's turn as sent, refused tool_use included.
                    message['tool_calls'], calls = None, []
                inp = usage['input_tokens'] + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
                normalized_usage = {'prompt_tokens': inp, 'completion_tokens': usage['output_tokens'], 'total_tokens': inp + usage['output_tokens'], 'prompt_tokens_details': {'cached_tokens': usage.get('cache_read_input_tokens', 0)}, 'cache_creation_input_tokens': usage.get('cache_creation_input_tokens', 0), 'native_usage': usage,
                                    'completion_tokens_details': {'reasoning_tokens': usage.get('output_tokens_details', {}).get('thinking_tokens', 0)},
                                    'native_cost': {'total_cost_usd': final.get('total_cost_usd'), 'modelUsage': final.get('modelUsage')}}
                normalized_usage['native_admission'] = {'upstream_requests': int(admission.used), 'blocked_requests': admission.denied, 'request_id': admission.request_id}
                if admission.unrestored:
                    normalized_usage['native_admission']['unrestored'] = admission.unrestored
                finish = 'content_filter' if refused else 'tool_calls' if calls else ('length' if any(a.get('stop_reason') in ('max_tokens', 'model_context_window_exceeded') for a in assistants) else 'stop')
                response = obj({'id': assistants[-1].get('id', 'claude-native'), 'model': kwargs['model'], 'object': 'chat.completion', 'choices': [{'index': 0, 'finish_reason': finish, 'message': message}], 'usage': normalized_usage})
                chunk = self._chunk(kwargs['model'], {'content': None, 'tool_calls': [dict(tc, index=i) for i, tc in enumerate(calls)] or None, 'reasoning_details': [carrier], **({'refusal': message['refusal']} if refused else {})}, finish, normalized_usage)
                chunk._response = response
                yield chunk
        finally:
            request.cancel()
            if request.admission is not None:
                request.admission.close()
            if p is not None:
                p.wait(timeout=5)
                if reader is not None:
                    reader.join(timeout=5)
                for pipe in (p.stdin, p.stdout):
                    if pipe and not pipe.closed:
                        pipe.close()
            with self._lock:
                self._requests.discard(request)

    def _run_warm(self, request, kwargs, body, manifest, names, system, frames):
        """One Hermes step on a live native session; any mismatch rebuilds it from the full history."""
        started = time.monotonic()
        timeout = kwargs.get('timeout', self.timeout)
        timeout = getattr(timeout, 'read', timeout)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError('timeout must be positive seconds')
        messages = [m for m in kwargs['messages'] if m.get('role') not in ('system', 'developer')]
        keys = [_message_key(m) for m in messages]
        # Every step re-runs baseline's launch guards; the launch configuration is part of reuse identity.
        env, resolved = self._native_env()
        # Only what decides native auth/config; per-process values (CLAUDE_PID, session ids) must not force rebuilds.
        launch = [resolved, sorted((k, v) for k, v in env.items() if k in _WARM_LAUNCH_KEYS or k.startswith(_WARM_LAUNCH_PREFIXES))]
        ident = hashlib.sha256(json.dumps([kwargs['model'], body, system, launch]).encode('utf-8')).hexdigest()
        first = next((k for k, m in zip(keys, messages) if m.get('role') == 'assistant'), None)
        session, how = _warm_checkout(ident, keys, messages, first)
        if session is None:
            session = WarmSession(ident, first)
            if not _warm_admit(session):
                # Every warm slot is busy: this step takes the per-call path instead of exceeding the cap.
                yield from self._run(request, kwargs, body, manifest, names, system, frames)
                return
            spawn = True
        else:
            spawn = False
        done = yielded = False
        try:
            if not spawn:
                n = len(session.consumed)
                session.attach(request)
                if session.expect == 'user':
                    session.admission.rearm(timeout, frames[-1]['message']['content'])
                    session.send(frames[-1])
                else:
                    results = {m['tool_call_id']: (m['content'], bool(m.get('is_error'))) for m in messages[n:]}
                    # Native replays the MCP results it holds, in its own call order, not Hermes' tool messages: anchor on that.
                    called = [c.get('id') for c in messages[n - 1].get('tool_calls') or []]
                    order = [i for i in called if i in results] + [i for i in results if i not in called]
                    session.admission.rearm(timeout, [{'type': 'tool_result', 'tool_use_id': tool_id, 'content': [{'type': 'text', 'text': results[tool_id][0]}], **({'is_error': True} if results[tool_id][1] else {})}
                                                      for tool_id in order])
                    session.deliver(results)
            else:
                self._warm_spawn(session, request, kwargs, body, manifest, system, frames, timeout, env, resolved)
            emitted, native_error, native_error_code, final = '', None, None, None
            while True:
                event = session.receive(request, timeout)
                if event is None:
                    raise RuntimeError('Native exited during a warm step')
                kind = event.get('type')
                if kind == 'assistant' and (event.get('error') or event.get('message', {}).get('error')):
                    native_error = '\n'.join(b.get('text', '') for b in event.get('message', {}).get('content', []) if b.get('type') == 'text')
                    native_error_code = event.get('error')
                elif kind == 'result':
                    final = event
                    break
                elif kind == 'stream_event':
                    native = event['event']
                    delta = native.get('delta', {})
                    if delta.get('type') == 'text_delta':
                        emitted += delta['text']
                        yielded = True
                        yield self._chunk(kwargs['model'], {'content': delta['text']})
                    elif delta.get('type') == 'thinking_delta':
                        yielded = True
                        yield self._chunk(kwargs['model'], {'reasoning_content': delta['thinking']})
                    elif native['type'] == 'message_stop':
                        capture = session.admission.capture
                        # Native now calls the hermes MCP tools; they park until the next step brings results.
                        # A refusal ends the step too: its session is never reused (below).
                        if capture.complete and capture.message.get('stop_reason') in ('tool_use', 'refusal'):
                            break
            admission = session.admission
            if native_error_code == 'authentication_failed' and not admission.used:
                raise ClaudeCodeLoggedOut(f'{LOGGED_OUT_HINT} (native: {native_error})')
            # Failures carry the status Hermes routes retry/fallback on, as in baseline (#58).
            if native_error and not admission.used:
                raise ClaudeAPIError('Native API error: ' + native_error, NATIVE_ERROR_STATUS.get(str(native_error_code)))
            if not admission.used or admission.status != 200 or not admission.capture.complete:
                first_attempt = f'upstream used: {admission.used}, status {admission.status}, capture ' + ('complete' if admission.capture.complete else 'incomplete') + (f', relay failure {admission.failure}' if admission.failure else '') + f', native retries denied: {admission.denied}'
                if admission.error_text():
                    first_attempt += ', upstream said: ' + admission.error_text()[:500]
                status = admission.status if isinstance(admission.status, int) and admission.status >= 400 else None
                raise ClaudeAPIError(f'Incomplete upstream response ({first_attempt})' + (': ' + native_error if native_error else ''), status)
            assistant = admission.capture.message
            refused = assistant.get('stop_reason') == 'refusal'
            # A denied native retry, a native error or a refusal leaves native's own state unknown: answer, then rebuild next step.
            poisoned = refused or bool(admission.denied or native_error) or (final is not None and (final.get('is_error') or final.get('subtype') != 'success'))
            if poisoned and not refused and not admission.denied:
                raise ClaudeAPIError('Native API error: ' + (native_error or str(final.get('subtype'))), NATIVE_ERROR_STATUS.get(str(native_error_code)))
            blocks = assistant['content']
            calls = []
            for block in blocks:
                if block.get('type') == 'tool_use':
                    # As baseline (#39, #62): a name outside the inventory goes to Hermes, a bare offered name carries
                    # the prefix. Native parks neither on the hermes MCP server, so the session cannot continue.
                    if block['name'] in names:
                        block['name'] = PREFIX + block['name']
                        poisoned = True
                    elif not block['name'].startswith(PREFIX) or block['name'][len(PREFIX):] not in names:
                        poisoned = True
                    calls.append({'id': block['id'], 'type': 'function', 'function': {'name': block['name'].removeprefix(PREFIX), 'arguments': json.dumps(block['input'], separators=(',', ':'), allow_nan=False)}})
            usage = assistant['usage']
            text = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
            if emitted != text:
                if text.startswith(emitted):
                    yield self._chunk(kwargs['model'], {'content': text[len(emitted):]})
                else:
                    raise RuntimeError('Native final text differs from incremental stream')
            message = {'role': 'assistant', 'content': text or None, 'tool_calls': calls or None,
                       'reasoning_content': ''.join(b.get('thinking', '') for b in blocks if b.get('type') == 'thinking') or None}
            carrier = {'type': CARRIER, 'version': 1, 'messages': [assistant], 'projection': projection(message)}
            message['reasoning_details'] = [carrier]
            # A refusal is Hermes' content_filter with its reason, and a cut-off call stays out of tool_calls (baseline).
            if refused:
                details = assistant['stop_details'] if isinstance(assistant.get('stop_details'), dict) else {}
                explanation, category = details.get('explanation'), details.get('category')
                message['refusal'] = (explanation.strip() if isinstance(explanation, str) and explanation.strip() else
                                      f'provider refusal category: {category}' if isinstance(category, str) and category else None)
                message['tool_calls'], calls = None, []
            inp = usage['input_tokens'] + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
            thinking = usage.get('output_tokens_details', {}).get('thinking_tokens', 0)
            normalized_usage = {'prompt_tokens': inp, 'completion_tokens': usage['output_tokens'], 'total_tokens': inp + usage['output_tokens'], 'prompt_tokens_details': {'cached_tokens': usage.get('cache_read_input_tokens', 0)}, 'cache_creation_input_tokens': usage.get('cache_creation_input_tokens', 0), 'native_usage': usage,
                                'completion_tokens_details': {'reasoning_tokens': thinking},
                                # Native reports cost only per whole native turn; a step has no list-price figure of its own.
                                'native_cost': {'total_cost_usd': None, 'modelUsage': None}}
            normalized_usage['native_admission'] = {'upstream_requests': int(admission.used), 'blocked_requests': admission.denied, 'request_id': admission.request_id, 'warm': how}
            if admission.unrestored:
                normalized_usage['native_admission']['unrestored'] = admission.unrestored
            finish = 'content_filter' if refused else 'tool_calls' if calls else ('length' if assistant.get('stop_reason') in ('max_tokens', 'model_context_window_exceeded') else 'stop')
            response = obj({'id': assistant.get('id', 'claude-native'), 'model': kwargs['model'], 'object': 'chat.completion', 'choices': [{'index': 0, 'finish_reason': finish, 'message': message}], 'usage': normalized_usage})
            chunk = self._chunk(kwargs['model'], {'content': None, 'tool_calls': [dict(tc, index=i) for i, tc in enumerate(calls)] or None, 'reasoning_details': [carrier], **({'refusal': message['refusal']} if refused else {})}, finish, normalized_usage)
            chunk._response = response
            _warm_log.info('warm step: session=%s mode=%s upstream=%d denied=%d wall=%.2fs in=%d cache_read=%d cache_write=%d out=%d thinking=%d calls=%d stop=%s id=%s',
                           session.ident[:8] + ':' + str(session.pid), how, int(admission.used), admission.denied, time.monotonic() - started,
                           usage['input_tokens'], usage.get('cache_read_input_tokens', 0), usage.get('cache_creation_input_tokens', 0), usage['output_tokens'], thinking, len(calls), assistant.get('stop_reason'), assistant.get('id'))
            # Settle before the final yield: a caller closing the stream after it must not kill a healthy session.
            session.detach(request)
            if not poisoned:
                session.consumed = keys + [_message_key(message)]
                session.first = session.first or session.consumed[len(messages)]
                session.owner = weakref.ref(self)
                session.expect = frozenset(c['id'] for c in calls) if calls else 'user'
                # False when cancel() landed during this handoff: the session was killed, never published.
                done = _warm_release(session, request)
            yield chunk
        except Exception as error:
            # A warm launch or session that fails before admission (a CLI without the warm protocol, a native that
            # died while parked) serves this step per-call. Admitted, streamed, cancelled or refused steps stay errors.
            session.detach(request)
            _warm_discard(session)
            done = True
            admission = session.admission
            if admission is not None:
                admission.abort()  # nothing is admitted after this, so `used` is final
            refused = isinstance(error, (ValueError, ClaudeCodeMissing, ClaudeCodeLoggedOut)) or (isinstance(error, ClaudeAPIError) and error.status_code is not None)
            if yielded or refused or request.cancelled.is_set() or (admission is not None and admission.used):
                raise
            _warm_log.info('warm fallback: session=%s per-call after %s', session.ident[:8] + ':' + str(session.pid), type(error).__name__)
            yield from self._run(request, kwargs, body, manifest, names, system, frames)
        finally:
            if session is not None and not done:
                session.detach(request)
                _warm_discard(session)
            with self._lock:
                self._requests.discard(request)

    def _warm_spawn(self, session, request, kwargs, body, manifest, system, frames, timeout, env, resolved):
        root = Path(tempfile.mkdtemp(prefix='claude-directsdk-warm-'))
        session.root, session.manifest = root, manifest
        # A parked hermes tool call lasts as long as Hermes runs the tool; Hermes already bounds its output.
        env.update(MCP_TOOL_TIMEOUT='86400000', MAX_MCP_OUTPUT_TOKENS='100000000')
        # The queried frame lets the relay keep the cache breakpoint off native's per-request context.
        session.admission = Admission(env.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com'), timeout, queried=frames[-1]['message']['content'])
        env['ANTHROPIC_BASE_URL'] = session.admission.url
        (root / 'settings.json').write_text(json.dumps({'env': {'CLAUDE_CODE_EXTRA_BODY': body}}), encoding='utf-8')
        (root / 'system.md').write_text(system, encoding='utf-8')
        parsed = json.loads(body)
        if 'max_tokens' in parsed:
            env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = str(parsed['max_tokens'])
        mcp = {'mcpServers': {'hermes': {'type': 'sdk', 'name': 'hermes'}}}
        command = resolved + ['-p', '--model', native_model(kwargs['model']), '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--tools', '', '--system-prompt-file', str(root / 'system.md'), '--settings', str(root / 'settings.json'), '--setting-sources', '', '--strict-mcp-config', '--disable-slash-commands', '--permission-mode', 'dontAsk', '--allowedTools', 'mcp__hermes', '--no-session-persistence', '--mcp-config', json.dumps(mcp)]
        effort = parsed.get('output_config', {}).get('effort')
        if effort:
            command += ['--effort', effort]
        with request.lock:
            if request.cancelled.is_set():
                raise RuntimeError('Claude request cancelled')
            session.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', cwd=self._workdir(), env=env, **_own_process_group())
            request.process, request.admission = session.process, session.admission
        session.pid = session.process.pid
        threading.Thread(target=session.read, daemon=True).start()
        session.send({'type': 'control_request', 'request_id': 'hermes-init', 'request': {'subtype': 'initialize', 'hooks': None}})
        for index, frame in enumerate(frames):
            frame = copy.deepcopy(frame)
            if frame['type'] == 'user' and index < len(frames) - 1:
                frame['shouldQuery'] = False
            session.send(frame)
            if frame.get('shouldQuery') is False:
                while True:
                    ack = session.receive(request, timeout)
                    if ack is None:
                        raise RuntimeError('Native exited before replay acknowledgment')
                    if ack.get('type') == 'result':
                        if ack.get('num_turns') != 0 or ack.get('is_error'):
                            raise RuntimeError('Native history replay not supported: expected zero-turn acknowledgment')
                        break

    @staticmethod
    def _chunk(model, delta, finish=None, usage=None):
        return obj({'id': 'claude-native', 'model': model, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': None, 'tool_calls': None, 'reasoning_details': None, **delta}, 'finish_reason': finish}], 'usage': usage})


# ---------------------------------------------------------------------------------------------
# Warm mode (opt-in, CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM=1): one live native session per Hermes
# conversation. Hermes still receives every tool call and runs it; native's call to the hermes
# SDK MCP server parks until the next step brings Hermes' result. Sessions are found by history,
# never by client identity (Hermes builds request-local clients and closes them freely).
# ---------------------------------------------------------------------------------------------
WARM_IDLE_SECONDS = float(os.environ.get('CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM_IDLE_SECONDS') or 600)
WARM_POOL_SIZE = int(os.environ.get('CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM_POOL') or 8)
_warm_log = logging.getLogger(__name__)
_warm_lock = threading.Lock()
_warm_pool = []
_warm_reaper = None
# The telemetry setting (applied per spawn) and the proxy route are fixed for a session's life: a change starts a new one.
_WARM_LAUNCH_KEYS = ('HOME', 'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN', 'DO_NOT_TRACK', *QUIET_TRAFFIC,
                     'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'NO_PROXY', 'no_proxy')
_WARM_LAUNCH_PREFIXES = ('ANTHROPIC_', 'CLAUDE_SUBSCRIPTION_DIRECTSDK_', 'CLAUDE_CODE_USE_')


def _warm_enabled(env):
    return env.get('CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM', '').lower() in ('1', 'true', 'yes', 'on')


def _message_key(message):
    role = message.get('role')
    if role == 'assistant':
        # The native carrier is replay-relevant: same visible text with other native blocks is another history.
        carriers = [d for d in message.get('reasoning_details') or [] if isinstance(d, dict) and d.get('type') == CARRIER]
        native = hashlib.sha256(json.dumps(carriers, sort_keys=True).encode('utf-8')).hexdigest() if carriers else None
        return json.dumps(['assistant', projection(message), native], sort_keys=True)
    if role == 'tool':
        return json.dumps(['tool', message.get('tool_call_id'), message.get('content'), message.get('is_error')], sort_keys=True)
    return json.dumps([role, message.get('content')], sort_keys=True)


def _warm_expects(expect, suffix):
    if not suffix:
        return False
    if expect == 'user':
        return all(m.get('role') == 'user' for m in suffix)
    return (len(suffix) == len(expect) and all(m.get('role') == 'tool' and isinstance(m.get('content'), str) for m in suffix)
            and {m.get('tool_call_id') for m in suffix} == expect)


class WarmSession:
    def __init__(self, ident, first):
        self.ident, self.first = ident, first
        self.consumed, self.expect = [], None
        self.process = self.admission = self.root = self.pid = None
        self.manifest = []
        self.events = queue.Queue()
        self.lock = threading.Lock()  # stdin writes and the parked/results tables
        self.parked, self.results = {}, {}
        self.busy, self.last_used, self.killed = True, time.monotonic(), False
        self.owner = lambda: None

    def alive(self):
        return not self.killed and self.process is not None and self.process.poll() is None

    def attach(self, request):
        with request.lock:
            if request.cancelled.is_set():
                raise RuntimeError('Claude request cancelled')
            request.process, request.admission = self.process, self.admission

    @staticmethod
    def detach(request):
        with request.lock:
            request.process = request.admission = None

    def send(self, row):
        with self.lock:
            self._write(row)

    def _write(self, row):
        self.process.stdin.write(json.dumps(row, allow_nan=False) + '\n')
        self.process.stdin.flush()

    def read(self):
        try:
            for line in self.process.stdout:
                event = json.loads(line)
                if event.get('type') == 'control_request' and event.get('request', {}).get('subtype') == 'mcp_message':
                    self._mcp(event['request_id'], event['request'].get('message') or {})
                elif event.get('type') != 'control_response':
                    self.events.put(event)
        except Exception as error:
            self.events.put(error)
        finally:
            self.process.wait()
            self.events.put(None)

    def _mcp(self, request_id, message):
        method = message.get('method')
        with self.lock:
            if method == 'tools/call':
                tool_id = ((message.get('params') or {}).get('_meta') or {}).get('claudecode/toolUseId')
                self.parked[tool_id] = (request_id, message.get('id'))
                self._answer(tool_id)
                return
            result = {}
            if method == 'initialize':
                result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'hermes', 'version': '1'}}
            elif method == 'tools/list':
                result = {'tools': self.manifest}
            self._reply(request_id, {'jsonrpc': '2.0', 'id': message['id'], 'result': result} if 'id' in message else {'jsonrpc': '2.0', 'result': {}})

    def _reply(self, request_id, mcp_response):
        self._write({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': request_id, 'response': {'mcp_response': mcp_response}}})

    def _answer(self, tool_id):
        if tool_id in self.parked and tool_id in self.results:
            request_id, rpc_id = self.parked.pop(tool_id)
            content, is_error = self.results.pop(tool_id)
            self._reply(request_id, {'jsonrpc': '2.0', 'id': rpc_id, 'result': {'content': [{'type': 'text', 'text': content}], 'isError': is_error}})

    def deliver(self, results):
        """Hermes' tool results, answered as native asks for each parked call (in native's order)."""
        with self.lock:
            self.results.update(results)
            for tool_id in results:
                self._answer(tool_id)

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
            if request.cancelled.is_set():
                raise RuntimeError('Claude request cancelled')
            if isinstance(event, Exception):
                raise RuntimeError('Invalid native stream-json output: ' + repr((getattr(event, 'doc', None) or str(event))[:300])) from event
            return event

    def kill(self, reason):
        with self.lock:
            if self.killed:
                return
            self.killed = True
        _warm_log.info('warm kill: session=%s reason=%s', self.ident[:8] + ':' + str(self.pid), reason)
        if self.process is not None:
            kill_process_tree(self.process)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self.admission is not None:
            self.admission.close()
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)


def _warm_checkout(ident, keys, messages, first):
    """The idle session whose consumed history prefixes this request and expects its suffix, marked busy."""
    now, stale, matches, how = time.monotonic(), [], [], 'cold'
    with _warm_lock:
        for session in list(_warm_pool):
            if session.busy:
                continue
            n = len(session.consumed)
            if not session.alive() or now - session.last_used > WARM_IDLE_SECONDS:
                stale.append(session)
            elif session.ident == ident and keys[:n] == session.consumed and _warm_expects(session.expect, messages[n:]):
                matches.append(session)
            elif first is not None and session.first == first:
                # This conversation diverged (compaction, edit, interrupt, changed prompt/tools/model).
                why = 'ident' if session.ident != ident else ('prefix' if keys[:n] != session.consumed else 'suffix')
                stale.append(session)
                how = 'rebuild:' + why
        found = matches[0] if len(matches) == 1 else None
        if found is not None:
            found.busy, how = True, 'reused'
        elif matches:
            how = 'rebuild:ambiguous'  # never pick by pool order; replay instead
        for session in stale:
            _warm_pool.remove(session)
    for session in stale:
        session.kill('stale-or-diverged')
    return found, how


def _warm_trim(room):
    """Evict least-recently-used idle sessions until ``room`` more fit under the cap (caller holds the lock)."""
    idle = sorted((s for s in _warm_pool if not s.busy), key=lambda s: s.last_used)
    evict = idle[:max(0, len(_warm_pool) + room - WARM_POOL_SIZE)]
    for s in evict:
        _warm_pool.remove(s)
    return evict


def _warm_admit(session):
    """Reserve a pool slot for a new busy session; False when every slot is busy."""
    global _warm_reaper
    with _warm_lock:
        # Start the reaper before reserving anything: a failed start leaves no reservation and allows a retry.
        if _warm_reaper is None:
            reaper = threading.Thread(target=_warm_reap, daemon=True)
            try:
                reaper.start()
            except Exception as error:
                _warm_log.warning('warm reaper did not start (%s); this step runs per-call', error)
                return False
            _warm_reaper = reaper
        evict = _warm_trim(1)
        admitted = len(_warm_pool) < WARM_POOL_SIZE
        if admitted:
            _warm_pool.append(session)
    for s in evict:
        s.kill('lru')
    return admitted


def _warm_release(session, request):
    """Publish the settled session for reuse, unless its step was cancelled (then kill it). True when published.

    The owner is set before this runs, so a cancel() whose request flag lands after the check below still finds
    the session idle and owned in _warm_cancel_owned (cancel flags requests first, then sweeps owned sessions)."""
    with _warm_lock:
        cancelled = request.cancelled.is_set()
        if cancelled:
            if session in _warm_pool:
                _warm_pool.remove(session)
            evict = [session]
        else:
            session.busy, session.last_used = False, time.monotonic()
            evict = _warm_trim(0)
    for s in evict:
        s.kill('cancelled-during-handoff' if cancelled else 'lru')
    return not cancelled


def _warm_cancel_owned(client):
    with _warm_lock:
        owned = [s for s in _warm_pool if not s.busy and s.owner() is client]
        for s in owned:
            _warm_pool.remove(s)
    for s in owned:
        s.kill('cancelled-while-parked')


def _warm_discard(session):
    with _warm_lock:
        if session in _warm_pool:
            _warm_pool.remove(session)
    session.kill('step-failed-or-poisoned')


def _warm_reap():
    while True:
        time.sleep(min(30, WARM_IDLE_SECONDS / 2))
        now = time.monotonic()
        with _warm_lock:
            stale = [s for s in _warm_pool if not s.busy and (not s.alive() or now - s.last_used > WARM_IDLE_SECONDS)]
            for s in stale:
                _warm_pool.remove(s)
        for s in stale:
            s.kill('idle-reaper')


@atexit.register
def _warm_shutdown():
    with _warm_lock:
        sessions = list(_warm_pool)
        _warm_pool.clear()
    for session in sessions:
        session.kill('exit')
