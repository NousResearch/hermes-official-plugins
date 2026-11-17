"""``session_unlisted_versions``: what session mode does on a Claude Code release the tested list does not name.

honor (the default) runs it per-call; force keeps processes on it with one warning; self-test keeps processes on it
once the plugin's own offline test of that binary has passed. The self-test cases that drive a binary use the real
Claude Code CLI (``CLAUDE_CODE_BIN``) or a stand-in script; no credentials, no inference.
"""
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import threading
import time

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk
import directsdk_setup
import selftest

CLAUDE = os.environ.get('CLAUDE_CODE_BIN') or shutil.which('claude')
needs_cli = pytest.mark.skipif(not CLAUDE, reason='needs the Claude Code CLI')
posix = pytest.mark.skipif(os.name == 'nt', reason='the stand-in binaries are POSIX shell scripts')
LISTED = min(directsdk_setup.session_versions())
UNLISTED = '9.9.9'
CMD = ['claude']


@pytest.fixture
def mode(monkeypatch):
    """Sets the profile's session_unlisted_versions; forgets which lines were already logged."""
    monkeypatch.setattr(directsdk_setup, '_said', set())
    monkeypatch.setattr(directsdk_setup, '_untested', set())
    value = {}
    monkeypatch.setattr(directsdk_setup, '_setting',
                        lambda key, default=None: value.get('mode', default) if key == directsdk_setup.UNLISTED_SETTING else default)
    return lambda m: value.__setitem__('mode', m)


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    path = tmp_path / 'hermes' / 'cache' / 'session-selftest.json'
    monkeypatch.setattr(selftest, 'cache_path', lambda: path)
    monkeypatch.setattr(selftest._state, 'logged', set())
    monkeypatch.setattr(selftest._state, 'verdicts', {})
    monkeypatch.setattr(directsdk, '_unmatched', set(), raising=False)  # releases marked per-call by an earlier test
    return path


def lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name in (directsdk_setup._logger.name, selftest._logger.name)]


@pytest.mark.parametrize('value, expected', [(None, 'honor'), ('honor', 'honor'), ('force', 'force'), ('self-test', 'self-test'),
                                             ('Self_Test', 'self-test'), ('yes', 'honor'), (1, 'honor')])
def test_an_unknown_value_is_honor(mode, caplog, value, expected):
    if value is not None:
        mode(value)
    with caplog.at_level('WARNING', logger=directsdk_setup._logger.name):
        assert directsdk_setup.session_unlisted_versions() == expected
        directsdk_setup.session_unlisted_versions()
    assert len(lines(caplog)) == (1 if expected == 'honor' and value not in (None, 'honor') else 0)


@pytest.mark.parametrize('setting', ['honor', 'self-test', 'force', 'nonsense'])
def test_a_listed_version_keeps_a_process_in_every_mode(mode, cache, monkeypatch, setting):
    mode(setting)
    monkeypatch.setattr(selftest, 'request', lambda *a: pytest.fail('a listed version is never self-tested'))
    assert directsdk_setup.session_tested(LISTED, ['claude'])


def test_honor_runs_an_unlisted_version_per_call_and_says_so_once(mode, cache, monkeypatch, caplog):
    monkeypatch.setattr(selftest, 'request', lambda *a: pytest.fail('honor never self-tests'))
    with caplog.at_level('INFO', logger=directsdk_setup._logger.name):
        assert not directsdk_setup.session_tested(UNLISTED, ['claude'])
        assert not directsdk_setup.session_tested(UNLISTED, ['claude'])
    assert len(lines(caplog)) == 1 and 'runs only on Claude Code' in lines(caplog)[0]


def test_force_keeps_a_process_on_an_unlisted_version_with_one_warning(mode, cache, caplog):
    mode('force')
    with caplog.at_level('INFO', logger=directsdk_setup._logger.name):
        assert directsdk_setup.session_tested(UNLISTED, ['claude'])
        assert directsdk_setup.session_tested(UNLISTED, ['claude'])
    assert [r.levelname for r in caplog.records if r.name == directsdk_setup._logger.name] == ['WARNING']
    assert UNLISTED in lines(caplog)[0] and 'force' in lines(caplog)[0]


def test_a_version_it_could_not_read_never_keeps_a_process(mode, cache, monkeypatch):
    for setting in ('force', 'self-test'):
        mode(setting)
        monkeypatch.setattr(selftest, 'request', lambda *a: pytest.fail('nothing to test'))
        assert not directsdk_setup.session_tested(None, ['claude'])


def test_self_test_runs_per_call_until_a_verdict_and_follows_it(mode, cache, monkeypatch, caplog):
    mode('self-test')
    asked = []
    monkeypatch.setattr(selftest, 'request', lambda command, version: asked.append((command, version)))
    with caplog.at_level('INFO', logger=directsdk_setup._logger.name):
        assert not directsdk_setup.session_tested(UNLISTED, ['claude'])
        assert not directsdk_setup.session_tested(UNLISTED, ['claude'])
        assert asked == [(['claude'], UNLISTED)] * 2  # it asks each time; request() starts one test
        selftest._write(UNLISTED, CMD, {'verdict': 'pass', 'reason': 'fixture', 'seconds': 1.0, 'at': 'now'})
        assert directsdk_setup.session_tested(UNLISTED, ['claude']) and directsdk_setup.session_tested(UNLISTED, ['claude'])
        selftest._write('9.9.8', CMD, {'verdict': 'fail', 'reason': 'request 3 of 12 differs', 'seconds': 1.0, 'at': 'now'})
        assert not directsdk_setup.session_tested('9.9.8', ['claude'])
    assert len(asked) == 2
    said = lines(caplog)
    assert len(said) == 3, said  # testing (once), passed (once), failed (once)
    assert 'testing it offline' in said[0] and 'self-test passed' in said[1] and 'request 3 of 12 differs' in said[2]


def test_a_verdict_belongs_to_the_plugin_version_the_release_and_the_platform(cache, monkeypatch):
    selftest._write(UNLISTED, CMD, {'verdict': 'pass', 'reason': '', 'seconds': 1.0, 'at': 'now'})
    assert selftest.verdict(UNLISTED, CMD)['verdict'] == 'pass' and selftest.verdict('9.9.8', CMD) is None
    monkeypatch.setattr(selftest, 'plugin_version', lambda: '0.0.0-other')
    assert selftest.verdict(UNLISTED, CMD) is None
    monkeypatch.undo()
    monkeypatch.setattr(selftest, 'cache_path', lambda: cache)
    monkeypatch.setattr(selftest.platform, 'machine', lambda: 'other-machine')
    assert selftest.verdict(UNLISTED, CMD) is None


@pytest.mark.parametrize('content', ['{not json', '[]', '', '{KEY: "pass"}', '{KEY: {"verdict": "maybe"}}', '{KEY: ["pass"]}'])
def test_a_corrupt_cache_holds_no_verdict_and_is_rewritten(cache, content):
    cache.parent.mkdir(parents=True)
    cache.write_text(content.replace('KEY', json.dumps(selftest.verdict_key(UNLISTED, CMD))))
    assert selftest.verdict(UNLISTED, CMD) is None
    selftest._write(UNLISTED, CMD, {'verdict': 'fail', 'reason': 'r', 'seconds': 1.0, 'at': 'now'})
    assert selftest.verdict(UNLISTED, CMD)['verdict'] == 'fail'


def test_two_agents_asking_at_once_start_one_test(cache, monkeypatch):
    runs, gate = [], threading.Event()

    def run(command, version):
        runs.append(version)
        gate.wait(5)
        return True, 'fixture', 0.1
    monkeypatch.setattr(selftest, 'run_child', run)
    threads = [threading.Thread(target=selftest.request, args=(['claude'], UNLISTED)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    selftest.request(['claude'], '9.9.8')  # another release waits for the running test too
    gate.set()
    deadline = time.monotonic() + 5
    while selftest._state.running and time.monotonic() < deadline:
        time.sleep(.01)
    assert runs == [UNLISTED] and selftest.verdict(UNLISTED, CMD)['verdict'] == 'pass'
    selftest.request(['claude'], UNLISTED)  # a verdict exists: nothing runs again
    assert runs == [UNLISTED]


def test_a_test_that_cannot_run_is_a_fail(cache, monkeypatch):
    def broken(command, version):
        raise OSError('no loopback port')
    monkeypatch.setattr(selftest, 'run_child', broken)
    selftest.request(['claude'], UNLISTED)
    deadline = time.monotonic() + 5
    while selftest._state.running and time.monotonic() < deadline:
        time.sleep(.01)
    record = selftest.verdict(UNLISTED, CMD)
    assert record['verdict'] == 'fail' and 'no loopback port' in record['reason']


def test_a_missing_binary_fails_the_self_test(tmp_path):
    passed, reason, _ = selftest.run_child([str(tmp_path / 'no-such-claude')], UNLISTED, time_limit=60)
    assert not passed and reason


# The real binary.

@needs_cli
def test_the_real_binary_passes_its_self_test():
    """The listed release, run through the self-test as if it were not listed: every kept request is per-call's."""
    version = real_version()
    passed, reason, seconds = selftest.run_child([CLAUDE], version, time_limit=120)
    assert passed, reason
    assert 'byte-identical' in reason and seconds < 120


def real_version():
    import re
    import subprocess
    out = subprocess.run([CLAUDE, '--version'], capture_output=True, text=True, timeout=60).stdout
    return re.search(r'\d+\.\d+\.\d+', out).group(0)


def marker():
    """A sleep duration no other run on this machine is using: the tests find their sleepers by it."""
    import random
    return random.randint(600000, 699999)


def marked(seconds):
    """Processes on this machine sleeping exactly ``seconds`` (a duration only one test uses): found by command line,
    since inside the test's own pid namespace (Linux) a process's pid is not the one this side sees."""
    found = []
    for proc in psutil.process_iter(['cmdline', 'status']):
        try:
            if proc.info['cmdline'] and proc.info['cmdline'][-2:] == ['sleep', str(seconds)] and proc.info['status'] != psutil.STATUS_ZOMBIE:
                found.append(proc.pid)
        except psutil.Error:
            pass
    return found


def gone(seconds, within=15):
    deadline = time.monotonic() + within
    while marked(seconds) and time.monotonic() < deadline:
        time.sleep(.2)
    return marked(seconds)


def stand_in(tmp_path, body):
    script = tmp_path / 'claude'
    script.write_text('#!/bin/sh\n' + body)
    script.chmod(0o755)
    return str(script)


@needs_cli
@posix
def test_a_binary_whose_kept_requests_differ_by_one_byte_fails(tmp_path):
    """Kept processes of this stand-in see one more byte in their system prompt; per-call ones do not."""
    script = stand_in(tmp_path, f'''for a in "$@"; do
  case "$prev" in --system-prompt-file) prompt="$a";; esac
  case "$a" in --allowedTools) kept=1;; esac
  prev="$a"
done
[ -n "$kept" ] && [ -n "$prompt" ] && printf . >> "$prompt"
exec "{CLAUDE}" "$@"
''')
    version = real_version()
    passed, reason, _ = selftest.run_child([script], version, time_limit=120)
    assert not passed and 'differs from per-call' in reason, reason


@pytest.fixture(params=['namespaces', 'no-namespaces'])
def jail(request, monkeypatch):
    """Both ways a self-test runs: in namespaces of its own (Linux, where allowed) and without (elsewhere)."""
    if request.param == 'no-namespaces':
        monkeypatch.setattr(selftest, '_namespaces', lambda: None)
    elif not selftest._namespaces():
        pytest.skip('unprivileged namespaces are not available here')
    return request.param


@posix
def test_a_test_that_hangs_is_killed_with_everything_it_started(tmp_path, jail):
    seconds = marker()
    script = stand_in(tmp_path, f'touch {tmp_path}/started\nsleep {seconds} &\nexec sleep {seconds}\n')
    started = time.monotonic()
    passed, reason, _ = selftest.run_child([script], UNLISTED, time_limit=15)
    assert not passed and 'did not finish' in reason
    assert time.monotonic() - started < 45
    assert (tmp_path / 'started').exists(), 'the stand-in never ran: the test proves nothing'
    assert not gone(seconds)


@posix
def test_a_test_over_its_memory_bound_is_killed(tmp_path):
    script = stand_in(tmp_path, 'exec python3 -c "import time; b = bytearray(400 * 1024 * 1024); time.sleep(600)"\n')
    passed, reason, _ = selftest.run_child([script], UNLISTED, time_limit=60, memory_limit=200 * 1024 ** 2)
    assert not passed and 'more than 200 MB' in reason, reason


# A kept process when the setting changes, on the real binary.

@needs_cli
def test_a_kept_process_on_an_unlisted_version_ends_when_the_mode_goes_back_to_honor(mode, cache, monkeypatch, caplog):
    from test_directsdk_session import MODEL, TOOLS, client, upstream as _upstream  # noqa: F401
    import test_directsdk_session as base
    import tempfile
    from http.server import ThreadingHTTPServer
    with tempfile.TemporaryDirectory(prefix='directsdk-unlisted-') as home:
        server = ThreadingHTTPServer(('127.0.0.1', 0), base.Upstream)
        server.bodies, server.slow = [], 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            monkeypatch.setattr(directsdk_setup, 'session_versions', lambda: frozenset())
            mode('force')
            kept = client(home, server, monkeypatch, session=True)
            try:
                with caplog.at_level('DEBUG'):
                    messages = [{'role': 'user', 'content': 'go'}]
                    reply = kept.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
                    native = kept._session
                    assert native is not None, 'force kept no process on an unlisted version'
                    messages += [{k: v for k, v in reply.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None}]
                    messages += [{'role': 'tool', 'tool_call_id': t['id'], 'content': 'ok'} for t in reply['tool_calls']]
                    mode('honor')
                    kept.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000)
                    assert kept._session is None and native.process.poll() is not None
                assert any('session restart (version)' in r.getMessage() for r in caplog.records)
            finally:
                kept.close()
        finally:
            server.shutdown()
            server.server_close()


@posix
def test_a_child_that_dies_leaves_no_process_it_started(tmp_path, monkeypatch, jail):
    """The stand-in starts a sleeper in a session of its own, then the test's child process is killed from outside:
    what it started must still go."""
    seconds = marker()
    script = stand_in(tmp_path, f'touch {tmp_path}/started\n(setsid sleep {seconds} 2>/dev/null || sleep {seconds}) &\nexec sleep {seconds}\n')
    popen = selftest.subprocess.Popen

    def watched(*args, **kwargs):
        child = popen(*args, **kwargs)
        if '--version' in args[0] or 'true' in args[0][-1:]:
            return child

        def kill_later():
            deadline = time.monotonic() + 40
            while len(marked(seconds)) < 2 and time.monotonic() < deadline:
                time.sleep(.05)
            time.sleep(.6)  # the watch loop has seen the sleepers
            child.kill()
        threading.Thread(target=kill_later, daemon=True).start()
        return child
    monkeypatch.setattr(selftest.subprocess, 'Popen', watched)
    passed, reason, _ = selftest.run_child([script], UNLISTED, time_limit=60)
    assert not passed
    assert (tmp_path / 'started').exists(), 'the stand-in never ran: the test proves nothing'
    assert not gone(seconds)


@needs_cli
def test_self_test_mode_runs_per_call_until_the_real_binary_passes_then_keeps(mode, cache, monkeypatch, caplog):
    """No call waits for the test: the first steps run per-call while it runs in the background; once it has passed,
    a step keeps a process on the unlisted release."""
    from test_directsdk_session import MODEL, TOOLS, client
    import test_directsdk_session as base
    import tempfile
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr(directsdk_setup, 'session_versions', lambda: frozenset())
    mode('self-test')
    with tempfile.TemporaryDirectory(prefix='directsdk-unlisted-') as home:
        server = ThreadingHTTPServer(('127.0.0.1', 0), base.Upstream)
        server.bodies, server.slow = [], 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        c = client(home, server, monkeypatch, session=True)
        try:
            with caplog.at_level('INFO', logger=directsdk_setup._logger.name), caplog.at_level('INFO', logger=selftest._logger.name):
                started = time.monotonic()
                c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
                assert selftest._state.running, 'the first call waited for the self-test to end'
                assert time.monotonic() - started < 10 and c._session is None, 'the first call waited or kept a process'
                deadline = time.monotonic() + 120
                while selftest._state.running and time.monotonic() < deadline:
                    time.sleep(.2)
                record = selftest.verdict(directsdk_setup.claude_version([base.CLAUDE])[1], [base.CLAUDE])
                assert record and record['verdict'] == 'pass', record
                c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go again'}], tools=TOOLS, max_tokens=1000)
                assert c._session is not None, 'a passed self-test kept no process'
            said = lines(caplog)
            assert sum('testing it offline' in s for s in said) == 1 and sum('self-test passed' in s for s in said) == 1, said
        finally:
            c.close()
            server.shutdown()
            server.server_close()


def test_a_verdict_that_cannot_be_written_is_kept_for_this_process(cache, monkeypatch):
    """No cache file (no Hermes home, a read-only or full disk): the verdict still holds here, and the release is not
    tested again on every call."""
    monkeypatch.setattr(selftest, 'cache_path', lambda: None)
    runs = []
    monkeypatch.setattr(selftest, 'run_child', lambda command, version: (runs.append(version), (True, 'fixture', 0.1))[1])
    for _ in range(3):
        selftest.request(['claude'], UNLISTED)
        deadline = time.monotonic() + 5
        while selftest._state.running and time.monotonic() < deadline:
            time.sleep(.01)
    assert runs == [UNLISTED] and selftest.verdict(UNLISTED, CMD)['verdict'] == 'pass'


HERMES = """
import sys, time
sys.path[:0] = [{plugin!r}, {hermes!r}]
import selftest
selftest.cache_path = lambda: None
selftest.request([{script!r}], '9.9.9')
import os
deadline = time.time() + 40
while time.time() < deadline and not os.path.exists({started!r}):
    time.sleep(.1)
time.sleep(1.5)
{end}
"""


@posix
@pytest.mark.parametrize('end', ['sys.exit(0)', 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)'], ids=['exits', 'killed'])
def test_hermes_ending_mid_test_leaves_no_process_behind(tmp_path, end):
    """A Hermes process starts a self-test whose binary hangs, then exits (its test thread is abandoned) or is killed
    (nothing of it runs any more). The test's child and what it started must go too."""
    import subprocess
    seconds = marker()
    started = tmp_path / 'started'
    script = stand_in(tmp_path, f'touch {started}\n(setsid sleep {seconds} 2>/dev/null || sleep {seconds}) &\nexec sleep {seconds}\n')
    code = HERMES.format(plugin=str(Path(selftest.__file__).parent), hermes=os.environ.get('HERMES_AGENT_REPO', ''),
                         script=script, started=str(started), end=end)
    subprocess.run([sys.executable, '-c', code], timeout=120)
    assert started.exists(), 'the stand-in never ran: the test proves nothing'
    assert not gone(seconds)


def test_a_verdict_belongs_to_the_binary_not_to_every_one_reporting_that_version(tmp_path):
    one, other = tmp_path / 'one', tmp_path / 'other'
    one.write_text('a'), other.write_text('b')
    selftest._write(UNLISTED, [str(one)], {'verdict': 'pass', 'reason': '', 'seconds': 1.0, 'at': 'now'})
    assert selftest.verdict(UNLISTED, [str(one)])['verdict'] == 'pass'
    assert selftest.verdict(UNLISTED, [str(other)]) is None
    time.sleep(.01)
    one.write_text('a, updated in place')
    assert selftest.verdict(UNLISTED, [str(one)]) is None
    assert selftest.verdict(UNLISTED, None) is None


@pytest.mark.parametrize('pids, reasons, passed', [
    ([1] * 9 + [2] * 4, ['launch'], True),
    ([1] * 5 + [2] * 4 + [3] * 4, ['diverged', 'launch'], True),
    (list(range(13)), ['diverged'] * 12, False),            # a process per step
    ([1] * 5 + [2] * 4 + [3] * 4, ['diverged', 'diverged'], False),
    ([1] * 13, ['steer'], False),                           # a restart the conversation should not cause
    ([None] * 13, [], False)])                              # nothing kept at all
def test_the_verdict_needs_a_process_kept_across_the_steps(pids, reasons, passed):
    same = [b'x'] * 13
    assert selftest.judge(same, list(same), pids, reasons)['passed'] is passed
    assert selftest.judge(same, same[:-1] + [b'y'], pids, reasons)['passed'] is False


@needs_cli
def test_a_binary_that_reports_another_version_fails(tmp_path):
    passed, reason, _ = selftest.run_child([CLAUDE], '2.1.999', time_limit=120)
    assert not passed and 'reports' in reason and '2.1.999' in reason, reason


@posix
def test_a_killed_test_leaves_no_temporary_directory(tmp_path, monkeypatch):
    work = tmp_path / 'tmp'
    work.mkdir()
    monkeypatch.setattr(selftest.tempfile, 'tempdir', str(work))
    script = stand_in(tmp_path, 'exec sleep 600\n')
    passed, reason, _ = selftest.run_child([script], UNLISTED, time_limit=8)
    assert not passed and list(work.iterdir()) == []


@pytest.mark.parametrize('runs, verdict', [([False, True], 'pass'), ([False, False], 'fail'), ([True], 'pass')])
def test_a_fail_is_kept_only_when_a_second_run_fails_too(monkeypatch, runs, verdict):
    results = iter([(ok, 'fine' if ok else 'request 4 of 13 differs', 1.0) for ok in runs])
    calls = []
    monkeypatch.setattr(selftest, 'run_child', lambda command, version: (calls.append(1), next(results))[1])
    selftest.request(CMD, UNLISTED)
    deadline = time.monotonic() + 5
    while selftest._state.running and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(calls) == len(runs) and selftest.verdict(UNLISTED, CMD)['verdict'] == verdict


def test_another_hermes_process_testing_holds_this_one_off(monkeypatch):
    held = selftest._machine_lock()
    assert held not in (None, True)
    runs = []
    monkeypatch.setattr(selftest, 'run_child', lambda command, version: (runs.append(1), (True, 'fine', 1.0))[1])
    try:
        selftest.request(CMD, UNLISTED)
        deadline = time.monotonic() + 5
        while selftest._state.running and time.monotonic() < deadline:
            time.sleep(.01)
        assert runs == [] and selftest.verdict(UNLISTED, CMD) is None
    finally:
        held.close()
    selftest.request(CMD, UNLISTED)
    deadline = time.monotonic() + 5
    while selftest._state.running and time.monotonic() < deadline:
        time.sleep(.01)
    assert runs == [1] and selftest.verdict(UNLISTED, CMD)['verdict'] == 'pass'


@posix
def test_a_cache_path_that_is_not_a_regular_file_holds_no_verdict_and_never_blocks(cache):
    cache.parent.mkdir(parents=True)
    os.mkfifo(cache)
    out = []
    reader = threading.Thread(target=lambda: out.append(selftest.verdict(UNLISTED, CMD)), daemon=True)
    reader.start()
    reader.join(5)
    assert not reader.is_alive(), 'reading the verdict blocked the call'
    assert out == [None]


@needs_cli
@posix
def test_force_on_an_unlisted_release_holds_its_first_kept_request_to_per_calls(mode, monkeypatch, tmp_path, caplog):
    """A release that is not listed sends, kept, one byte more in its system prompt (this stand-in, on any model the
    self-test did not run). Its first kept request must not go upstream: it is held to per-call's own build, and the
    step runs per-call. Before, only a kept process's first resumed request was held to that."""
    from test_directsdk_session import MODEL, TOOLS, client
    import test_directsdk_session as base
    import tempfile
    from http.server import ThreadingHTTPServer
    script = stand_in(tmp_path, f'''for a in "$@"; do
  case "$prev" in --system-prompt-file) prompt="$a";; esac
  case "$a" in --allowedTools) kept=1;; esac
  prev="$a"
done
[ -n "$kept" ] && [ -n "$prompt" ] && printf . >> "$prompt"
exec "{CLAUDE}" "$@"
''')
    monkeypatch.setattr(base, 'CLAUDE', script)
    monkeypatch.setattr(directsdk_setup, 'session_versions', lambda: frozenset())
    monkeypatch.setattr(directsdk, 'session_versions', lambda: frozenset(), raising=False)
    mode('force')
    bodies = {}
    with tempfile.TemporaryDirectory(prefix='directsdk-unlisted-') as home:  # one home for both runs, as elsewhere
        for kept in (False, True):
            server = ThreadingHTTPServer(('127.0.0.1', 0), base.Upstream)
            server.bodies, server.slow = [], 0
            threading.Thread(target=server.serve_forever, daemon=True).start()
            c = client(home, server, monkeypatch, session=kept)
            try:
                with caplog.at_level('DEBUG'):
                    c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
            finally:
                c.close()
                server.shutdown()
                server.server_close()
            bodies[kept] = list(server.bodies)
    import test_directsdk_session_bytes as b
    assert bodies[True] == bodies[False], b.first_difference(bodies[True][0], bodies[False][0])
    assert any('session restart (diverged)' in r.getMessage() for r in caplog.records)


@posix
def test_inside_its_namespaces_the_test_reaches_nothing_but_loopback(tmp_path):
    """A binary that ignores the proxy settings tries a listener on this machine's own non-loopback address. In the
    test's network namespace (Linux, where allowed) it cannot reach it; this is what keeps a self-test offline
    whatever the binary does with its network."""
    import socket
    if not selftest._namespaces():
        pytest.skip('unprivileged namespaces are not available here')
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('192.0.2.1', 9))  # no packet is sent: this only picks the outward interface's address
        address = probe.getsockname()[0]
    finally:
        probe.close()
    if address.startswith('127.'):
        pytest.skip('this machine has no non-loopback address')
    listener = socket.socket()
    listener.bind((address, 0))
    listener.listen()
    listener.settimeout(20)
    port, outcome = listener.getsockname()[1], tmp_path / 'outcome'
    script = stand_in(tmp_path, f'''python3 - <<'PY'
import socket
try:
    socket.create_connection(({address!r}, {port}), timeout=3).close()
    result = 'reached'
except OSError as error:
    result = 'refused: ' + type(error).__name__
open({str(outcome)!r}, 'w').write(result)
PY
exec sleep 600
''')
    passed, reason, _ = selftest.run_child([script], UNLISTED, time_limit=20)
    listener.close()
    assert outcome.exists(), 'the stand-in never ran: the test proves nothing'
    assert outcome.read_text().startswith('refused'), outcome.read_text()
    assert not passed


def test_a_binary_replaced_during_its_test_gets_no_verdict(tmp_path, monkeypatch):
    """The binary is updated in place while it is tested: the result was the old one's, so neither gets a verdict,
    and the next call tests the new one."""
    binary = tmp_path / 'claude'
    binary.write_text('old')

    def run(command, version):
        time.sleep(.01)
        binary.write_text('new and longer')
        return True, 'fine', 1.0
    monkeypatch.setattr(selftest, 'run_child', run)
    selftest.request([str(binary)], UNLISTED)
    deadline = time.monotonic() + 5
    while selftest._state.running and time.monotonic() < deadline:
        time.sleep(.01)
    assert selftest.verdict(UNLISTED, [str(binary)]) is None


@posix
def test_a_process_left_by_claude_code_goes_when_hermes_is_killed_without_namespaces(tmp_path, monkeypatch):
    """No namespaces: a stand-in starts a sleeper in a session of its own and leaves it (its subshell exits, so it is
    init's and in no group the child kills), then Hermes is killed. The child, which saw it start, ends it too."""
    import subprocess
    seconds, other = marker(), marker()
    started = tmp_path / 'started'
    script = stand_in(tmp_path, f'(setsid sleep {seconds} & touch {started}; sleep 1)\nexec sleep {other}\n')
    code = HERMES.format(plugin=str(Path(selftest.__file__).parent), hermes=os.environ.get('HERMES_AGENT_REPO', ''),
                         script=script, started=str(started), end='time.sleep(2); import os, signal; os.kill(os.getpid(), signal.SIGKILL)')
    code = code.replace('import selftest\n', 'import selftest\nselftest._namespaces = lambda: None\n')
    subprocess.run([sys.executable, '-c', code], timeout=120)
    assert started.exists(), 'the stand-in never ran: the test proves nothing'
    assert not gone(seconds) and not gone(other)


@posix
def test_hermes_exit_ends_a_running_test_and_removes_its_directory(tmp_path, monkeypatch):
    """The exit hook alone (the stdin guard is not what ends it here): a running test's child, what it started and its
    directory all go."""
    work = tmp_path / 'tmp'
    work.mkdir()
    monkeypatch.setattr(selftest.tempfile, 'tempdir', str(work))
    seconds, started = marker(), tmp_path / 'started'
    script = stand_in(tmp_path, f'touch {started}\nexec sleep {seconds}\n')
    runner = threading.Thread(target=selftest.run_child, args=([script], UNLISTED), kwargs={'time_limit': 120}, daemon=True)
    runner.start()
    deadline = time.monotonic() + 40
    while not marked(seconds) and time.monotonic() < deadline:
        time.sleep(.1)
    assert started.exists(), 'the stand-in never ran: the test proves nothing'
    selftest._state.end_children()
    assert not gone(seconds)
    assert list(work.iterdir()) == []
    runner.join(30)


@posix
def test_a_pid_that_is_no_longer_the_process_seen_is_never_group_killed():
    """A process seen during a test has gone and its pid now leads another group: nothing may signal that group."""
    import subprocess
    bystander = subprocess.Popen(['sleep', '600'], start_new_session=True)

    class Stale:
        pid = bystander.pid

        def is_running(self):
            return False  # what psutil says of a pid whose process changed since it was seen

        def kill(self):
            raise AssertionError('a stale process was killed')

    child = subprocess.Popen(['sleep', '600'])  # the test's child stand-in: nothing under it
    try:
        selftest._kill_all(child, lambda child: None, [Stale()])
        try:
            bystander.wait(timeout=1)  # a signal sent takes a moment to show
        except subprocess.TimeoutExpired:
            pass
        assert bystander.poll() is None, 'the stale pid\'s group was killed'
    finally:
        for proc in (bystander, child):
            proc.kill()
            proc.wait()


@posix
def test_no_hermes_setting_reaches_the_test(tmp_path, monkeypatch):
    """A profile (or managed) claude_config_dir naming a real login must not become the test's config directory."""
    login = tmp_path / 'real-login'
    login.mkdir()
    seen = tmp_path / 'seen'
    script = stand_in(tmp_path, f'echo "$CLAUDE_CONFIG_DIR" > {seen}\nexit 1\n')
    monkeypatch.setattr(directsdk_setup, '_setting',
                        lambda key, default=None: str(login) if key == directsdk_setup.CONFIG_DIR_SETTING else default)
    for name in ('session_min_rounds', 'max_concurrent', 'check_extra_usage', 'session_tested', 'session_settings'):
        monkeypatch.setattr(directsdk, name, getattr(directsdk, name))
    for name in ('HOME', 'USERPROFILE', 'HERMES_HOME'):
        monkeypatch.setenv(name, os.environ.get(name, ''))
    monkeypatch.setattr(directsdk.logger, 'level', directsdk.logger.level)
    monkeypatch.setattr(directsdk.logger, 'handlers', list(directsdk.logger.handlers))
    monkeypatch.setattr(selftest, '_guard', lambda *a: None)  # in this process its watchdog would outlive the test
    result = selftest.child({'command': [script], 'version': UNLISTED, 'sys_path': [], 'time_limit': 30, 'work': str(tmp_path)})
    assert not result['passed']
    assert seen.exists(), 'the stand-in never ran: the test proves nothing'
    assert seen.read_text().strip() not in ('', str(login)), seen.read_text()


@needs_cli
@posix
def test_an_unlisted_release_whose_kept_launch_differs_runs_per_call_after_the_first_refusal(mode, monkeypatch, tmp_path, caplog):
    """Its first kept request is refused once; every later step on that release runs per-call, without another probe
    and a refused launch each time, and says so once."""
    from test_directsdk_session import MODEL, TOOLS, client
    import test_directsdk_session as base
    import tempfile
    from http.server import ThreadingHTTPServer
    script = stand_in(tmp_path, f'''for a in "$@"; do
  case "$prev" in --system-prompt-file) prompt="$a";; esac
  case "$a" in --allowedTools) kept=1;; esac
  prev="$a"
done
[ -n "$kept" ] && [ -n "$prompt" ] && printf . >> "$prompt"
exec "{CLAUDE}" "$@"
''')
    monkeypatch.setattr(base, 'CLAUDE', script)
    monkeypatch.setattr(directsdk_setup, 'session_versions', lambda: frozenset())
    monkeypatch.setattr(directsdk, 'session_versions', lambda: frozenset(), raising=False)
    monkeypatch.setattr(directsdk, '_unmatched', set(), raising=False)
    mode('force')
    probes, build = [], directsdk.Client._per_call_request
    monkeypatch.setattr(directsdk.Client, '_per_call_request', lambda self, *a: (probes.append(1), build(self, *a))[1])
    with tempfile.TemporaryDirectory(prefix='directsdk-unlisted-') as home:
        server = ThreadingHTTPServer(('127.0.0.1', 0), base.Upstream)
        server.bodies, server.slow = [], 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        c = client(home, server, monkeypatch, session=True)
        try:
            with caplog.at_level('WARNING', logger=directsdk.logger.name):
                messages = [{'role': 'user', 'content': 'go'}]
                for _ in range(3):
                    reply = c.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
                    assert c._session is None
                    messages += [{k: v for k, v in reply.items() if k in ('role', 'content', 'tool_calls', 'reasoning_details') and v is not None}]
                    messages += [{'role': 'tool', 'tool_call_id': t['id'], 'content': 'ok'} for t in reply['tool_calls']]
        finally:
            c.close()
            server.shutdown()
            server.server_close()
    assert probes == [1], f'{len(probes)} per-call probes in 3 steps'
    assert sum('runs per-call from now on' in r.getMessage() for r in caplog.records) == 1


@needs_cli
def test_a_probe_that_raises_on_an_unlisted_release_gives_its_place_back(mode, monkeypatch):
    """The per-call build that proves an unlisted release's first kept request times out: the step fails, and the
    max_sessions place it had reserved is free again, not held for the life of the Hermes process."""
    from test_directsdk_session import MODEL, TOOLS, client
    import test_directsdk_session as base
    import tempfile
    from http.server import ThreadingHTTPServer
    monkeypatch.setattr(directsdk_setup, 'session_versions', lambda: frozenset())
    monkeypatch.setattr(directsdk, 'session_versions', lambda: frozenset(), raising=False)
    mode('force')

    def stalled(self, *args):
        raise TimeoutError('Claude request timed out')
    monkeypatch.setattr(directsdk.Client, '_per_call_request', stalled)
    with tempfile.TemporaryDirectory(prefix='directsdk-unlisted-') as home:
        server = ThreadingHTTPServer(('127.0.0.1', 0), base.Upstream)
        server.bodies, server.slow = [], 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        c = client(home, server, monkeypatch, session=True, cap=1)
        try:
            with pytest.raises(TimeoutError):
                c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
            with directsdk._live_lock:
                assert not directsdk._live, 'the place stayed reserved'
        finally:
            c.close()
            server.shutdown()
            server.server_close()
