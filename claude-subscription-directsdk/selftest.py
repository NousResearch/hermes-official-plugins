"""Session mode's own test of a Claude Code release that is not on the tested list (``session_unlisted_versions:
self-test``).

The release runs one Hermes-shaped conversation twice, per-call and kept, against a loopback upstream that answers
the same way every time, and every request of the kept run must be the per-call run's, byte for byte, on a process
that was kept. The conversation holds the shapes that broke kept mode before: several tool rounds, two calls in one
step, an error result, a whitespace-only result, a large result, a ``/steer`` after a round, and a compaction-style
rewrite of the history followed by a new turn.

Offline: the test runs in a child Python process with a throwaway home, Claude config directory and Hermes home, a
placeholder API key that no server ever sees, and a proxy that goes nowhere for anything but loopback. No login is
read, nothing leaves the machine, nothing is billed. The child is bounded in time and memory and killed with
everything it started; one runs at a time per Hermes process. The verdict is kept per (plugin version, Claude Code
version, platform) under Hermes' home, so a release is tested once.

What a pass shows: these shapes, on this machine, for this binary. Each step is still checked before upstream at run
time, as on a listed version. It does not stand in for the tested list, which names the releases the whole session
suite passed on.
"""
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType

_logger = logging.getLogger(__name__)

PLUGIN_ID = "claude-subscription-directsdk-experimental"
TIME_LIMIT_S = 240.0
MEMORY_LIMIT_BYTES = 3 * 1024 ** 3  # the child, its relay threads and at most two Claude Code processes
_PLUGIN_DIR = Path(__file__).resolve().parent
_CACHE_NAME = "session-selftest.json"

# Hermes imports this plugin once per profile home, one module each: one test at a time for the whole process.
_KEY = "_claude_subscription_directsdk_selftest_v1"
_state = ModuleType(_KEY)
_state.lock, _state.running, _state.logged = threading.Lock(), {}, set()
_state = sys.modules.setdefault(_KEY, _state)


def plugin_version():
    try:
        text = (_PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"^version:\s*(\S+)", text, re.M)
    return match.group(1).strip("'\"") if match else "unknown"


def verdict_key(version):
    return f"{plugin_version()}|{version}|{sys.platform}-{platform.machine().lower()}"


def cache_path():
    """Where verdicts are kept: under the Hermes process's home (one file for every profile), else nowhere."""
    try:
        from hermes_constants import get_process_hermes_home
        return Path(get_process_hermes_home()) / "cache" / PLUGIN_ID / _CACHE_NAME
    except Exception:
        return None


def _read_cache(path):
    """The verdicts on file; an unreadable or malformed file holds none (the release is tested again)."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("verdict") in ("pass", "fail")}


def verdict(version):
    """``{"verdict": "pass" | "fail", "reason", "seconds", "at"}`` for this release, or None (not tested yet)."""
    return _read_cache(cache_path()).get(verdict_key(version))


def _write(version, record):
    path = cache_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read_cache(path)
        data[verdict_key(version)] = record
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as error:
        _logger.warning("claude-subscription-directsdk: could not keep the self-test verdict for Claude Code %s: %s",
                        version, error)


def request(command, version):
    """Start this release's test in the background unless it has a verdict or a test is already running. Never
    blocks: until a verdict exists, calls run per-call."""
    if not version or verdict(version) is not None:
        return
    with _state.lock:
        if _state.running:
            return
        thread = threading.Thread(target=_test, args=(list(command), version), name="directsdk-selftest", daemon=True)
        _state.running[version] = thread
    thread.start()


def _hermes_paths():
    """The directories Hermes core imports from, for the child (it reads settings through Hermes like the plugin)."""
    paths = []
    for name in ("hermes_cli", "hermes_constants"):
        module = sys.modules.get(name)
        origin = getattr(module, "__file__", None)
        if origin:
            root = Path(origin).resolve().parent
            paths.append(str(root.parent if root.name == name else root))
    return list(dict.fromkeys(paths))


def _child_env():
    """The child's environment: what a process needs to start on this platform, nothing that names a login."""
    keep = ("PATH", "SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR", "TEMP", "TMP", "TMPDIR",
            "LANG", "LC_ALL", "PROGRAMDATA", "PROGRAMFILES", "APPDATA", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS")
    return {k: os.environ[k] for k in keep if k in os.environ}


def _tree_rss(pid):
    try:
        import psutil
    except ImportError:
        return 0
    try:
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return 0
    total = 0
    for proc in procs:
        try:
            total += proc.memory_info().rss
        except psutil.Error:
            pass
    return total


def _descendants(pid):
    try:
        import psutil
        return psutil.Process(pid).children(recursive=True)
    except Exception:
        return []


def _kill_all(child, kill_process_tree, seen=()):
    """The child, and every process under it now or seen under it before (a child that crashed leaves its Claude Code
    to init): Claude Code runs in a process group of its own (the plugin owns each one that way), so the child's own
    group does not reach it. psutil refuses to signal a pid that was reused since."""
    found = {p.pid: p for p in list(seen) + _descendants(child.pid)}.values()
    kill_process_tree(child)
    for proc in found:
        try:
            if os.name != "nt":
                try:
                    if os.getpgid(proc.pid) == proc.pid:
                        os.killpg(proc.pid, 9)  # windows-footgun: ok — POSIX only, guarded above
                except (ProcessLookupError, PermissionError):
                    pass
            proc.kill()
        except Exception:
            pass


def run_child(command, version, time_limit=None, memory_limit=None):
    """Run the test in a child process and return ``(passed, reason, seconds)``; the child and everything it started
    are gone when this returns."""
    try:
        from .process_tree import kill_process_tree, own_process_group
    except ImportError:
        from process_tree import kill_process_tree, own_process_group
    time_limit = TIME_LIMIT_S if time_limit is None else time_limit
    memory_limit = MEMORY_LIMIT_BYTES if memory_limit is None else memory_limit
    spec = {"command": command, "version": version, "sys_path": _hermes_paths()}
    started = time.monotonic()
    try:
        child = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), json.dumps(spec)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=_child_env(), cwd=tempfile.gettempdir(), **own_process_group())
    except OSError as error:
        return False, f"the test could not start: {error}", 0.0
    out = {}
    reader = threading.Thread(target=lambda: out.update(stdout=child.stdout.read(), stderr=child.stderr.read()), daemon=True)
    reader.start()
    reason, seen = None, {}
    try:
        while child.poll() is None:
            seen.update((p.pid, p) for p in _descendants(child.pid))
            if time.monotonic() - started > time_limit:
                reason = f"it did not finish in {time_limit:.0f} s"
                break
            if memory_limit and _tree_rss(child.pid) > memory_limit:
                reason = f"it used more than {memory_limit // 1024 ** 2} MB"
                break
            time.sleep(0.25)
    finally:
        _kill_all(child, kill_process_tree, seen.values())
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=5)
    seconds = round(time.monotonic() - started, 1)
    if reason is not None:
        return False, reason, seconds
    lines = (out.get("stdout") or b"").decode("utf-8", "replace").strip().splitlines()
    try:
        result = json.loads(lines[-1])
    except (IndexError, ValueError):
        tail = (out.get("stderr") or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
        return False, "the test ended without a verdict" + (f" ({tail[0][:200]})" if tail else ""), seconds
    passed = result.get("passed") is True
    return passed, str(result.get("reason") or ("" if passed else "no reason given")), seconds


def _test(command, version):
    try:
        try:
            passed, reason, seconds = run_child(command, version)
        except Exception as error:  # a test that cannot run is a fail
            passed, reason, seconds = False, f"the test could not run: {error}", 0.0
        record = {"verdict": "pass" if passed else "fail", "reason": reason, "seconds": seconds,
                  "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        _write(version, record)
        _state.logged.add(version)
        if passed:
            _logger.warning("claude-subscription-directsdk: Claude Code %s is not on the tested list; its self-test "
                            "passed (%.0f s), so session mode keeps processes on it, each step still checked before "
                            "upstream", version, seconds)
        else:
            _logger.warning("claude-subscription-directsdk: Claude Code %s is not on the tested list and its "
                            "self-test failed (%s), so every call runs per-call", version, reason)
    finally:
        with _state.lock:
            _state.running.pop(version, None)


# The child: the conversation, run per-call and kept.

MODEL = "claude-sonnet-5-5"
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
STEER = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool "
         "output and not a new delivery when replayed from conversation history]\nread g1 instead\n"
         "[/OUT-OF-BAND USER MESSAGE]")
LARGE = 120_000
STEPS = 8  # tool steps in the first turn
# By step: what Hermes sends back. Step 0 launches the kept process and step 1 is its first resumed request, which a
# per-call build proves; the shapes that broke kept mode come after it, where only the per-step checks stand.
RESULTS = {1: [{"content": "first of two"}, {"content": "second of two"}], 2: [{"content": "oops", "is_error": True}],
           3: [{"content": " \n\t"}], 5: [{"content": "x" * LARGE}], 6: [{"content": "", "is_error": True}]}
STEER_AFTER = {3, 5, 6}  # a steer after a whitespace-only, a large and an empty error result
WHITESPACE_REPLY = 4  # this reply leads with a whitespace-only text block
EXPECTED_RESTARTS = 2  # that reply refused once (diverged), and the compacted turn on a new process
MIN_KEPT_STEPS = 4


def _reply(body):
    """The upstream's answer: a function of the request alone, so both runs are answered alike."""
    messages = body.get("messages") or []
    steps = sum(m.get("role") == "assistant" for m in messages)
    opening = json.dumps(messages[0].get("content") if messages else "")
    prefix = "b" if "Summary of the work" in opening else "a"
    tool = steps < (STEPS if prefix == "a" else 3)
    blocks = [{"type": "thinking", "thinking": f"plan {steps}", "signature": f"sig{prefix}{steps:04d}"},
              {"type": "text", "text": " \n\t" if steps == WHITESPACE_REPLY else f"step {steps}"}]
    if tool:
        for k in range(2 if steps == 1 else 1):
            blocks.append({"type": "tool_use", "id": f"toolu_{prefix}{steps:04d}{k}", "name": "mcp__hermes__read_file",
                           "input": {"path": f"f{steps}.{k}"}, "caller": {"type": "direct"}})
    events = [{"type": "message_start", "message": {"id": f"msg_{prefix}{steps}", "type": "message", "role": "assistant",
                                                    "model": body.get("model"), "content": [], "usage": USAGE}}]
    for i, block in enumerate(blocks):
        start = {**block, **({"thinking": "", "signature": ""} if block["type"] == "thinking" else
                             {"input": {}} if block["type"] == "tool_use" else {"text": ""})}
        events.append({"type": "content_block_start", "index": i, "content_block": start})
        if block["type"] == "thinking":
            deltas = ({"type": "thinking_delta", "thinking": block["thinking"]},
                      {"type": "signature_delta", "signature": block["signature"]})
        elif block["type"] == "tool_use":
            deltas = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])},)
        else:
            deltas = ({"type": "text_delta", "text": block["text"]},)
        events += [{"type": "content_block_delta", "index": i, "delta": d} for d in deltas]
        events.append({"type": "content_block_stop", "index": i})
    events += [{"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn"}, "usage": USAGE},
               {"type": "message_stop"}]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _upstream():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.server.bodies.append(raw)
            try:
                payload = _reply(json.loads(raw))
            except ValueError:
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.bodies = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _keep(message):
    return {k: v for k, v in message.items() if k in ("role", "content", "tool_calls", "reasoning_details") and v is not None}


def _conversation(client, pids):
    """Hermes' tool loop over the shapes above; ``pids`` gets the kept process (if any) after each step."""
    def loop(messages):
        for step in range(STEPS + 1):
            message = client.create(model=MODEL, messages=list(messages), tools=TOOLS, max_tokens=1000).choices[0].message.model_dump()
            session = client._session
            pids.append(session.process.pid if session is not None else None)
            messages.append(_keep(message))
            if not message.get("tool_calls"):
                return
            results = RESULTS.get(step, [])
            for k, call in enumerate(message["tool_calls"]):
                result = results[k] if k < len(results) else {"content": f"contents of {call['id']}"}
                messages.append({"role": "tool", "tool_call_id": call["id"], **result})
            if step in STEER_AFTER:
                messages.append({"role": "user", "content": STEER})
        raise RuntimeError("the conversation did not end")

    messages = [{"role": "system", "content": "You are a fixture agent."}, {"role": "user", "content": "go"}]
    loop(messages)
    # A compaction: Hermes replaces the history with a summary and carries on in a new turn.
    # Its opening turn holds an empty text block and a cache mark on a block that is not the last.
    loop([messages[0], {"role": "user", "content": [
        {"type": "text", "text": "Summary of the work so far: read f0 to f7.", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": ""}, {"type": "text", "text": "continue"}]}])


def child(spec):
    sys.path[:0] = [str(_PLUGIN_DIR)] + [p for p in spec.get("sys_path", []) if p not in sys.path]
    work = Path(tempfile.mkdtemp(prefix="directsdk-selftest-"))
    try:
        hermes_home = work / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
        # Settings read through Hermes come from this empty home, never the user's profile.
        os.environ["HERMES_HOME"] = str(hermes_home)
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(work)
        logging.basicConfig(level=logging.WARNING)
        import directsdk
        reasons = []

        class Restarts(logging.Handler):
            def emit(self, record):
                match = re.search(r"session restart \((\w+)\)", record.getMessage())
                if match:
                    reasons.append(match.group(1))
        directsdk.logger.addHandler(Restarts())
        directsdk.logger.setLevel(logging.DEBUG)
        directsdk.session_min_rounds = lambda: 0
        directsdk.max_concurrent = lambda: 4
        directsdk.check_extra_usage = lambda *a, **k: None
        directsdk.session_tested = lambda *a, **k: True  # this binary, under test
        upstream = _upstream()
        env = {**_child_env(), "HOME": str(work), "USERPROFILE": str(work), "CLAUDE_CONFIG_DIR": str(work / "claude"),
               "ANTHROPIC_API_KEY": "selftest-offline-placeholder",
               "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{upstream.server_port}",
               # Anything that is not loopback goes to a proxy that is not there.
               "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
               "NO_PROXY": "127.0.0.1,localhost", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
               "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"}
        (work / "claude").mkdir()
        # Claude Code's identity is on the wire; a fresh directory can make up one per process (two at once on
        # Windows), so it is written once, up front, for both runs.
        (work / "claude" / ".claude.json").write_text(json.dumps({"userID": "0" * 64}), encoding="utf-8")
        resolved = directsdk.resolve_claude(spec["command"], env)
        if resolved is None:
            return {"passed": False, "reason": "Claude Code was not found"}
        directsdk.note_version(resolved, spec["version"])
        runs = {}
        for mode in (False, True):
            directsdk.session_settings = (lambda m: lambda: (m, 1))(mode)
            client = directsdk.Client(command=resolved, timeout=60, env=env)
            pids = []
            try:
                _conversation(client, pids)
            finally:
                client.close()
            runs[mode], upstream.bodies[:] = (list(upstream.bodies), pids), []
        (expected, _), (actual, pids) = runs[False], runs[True]
        if len(actual) != len(expected):
            return {"passed": False, "reason": f"kept sent {len(actual)} requests, per-call {len(expected)}"}
        for n, (a, b) in enumerate(zip(actual, expected)):
            if a != b:
                at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
                return {"passed": False, "reason": f"request {n + 1} of {len(expected)} differs from per-call's at byte {at}"}
        kept = [p for p in pids if p is not None]
        longest = max((kept.count(p) for p in set(kept)), default=0)
        # Expected: the whitespace-led reply is refused once (Claude Code drops the block from its own record), and
        # the compacted turn starts a new process. Anything more means the release does not keep a process here.
        if (len(reasons) > EXPECTED_RESTARTS or not set(reasons) <= {"launch", "edited", "diverged"}
                or reasons.count("diverged") > 1 or longest < MIN_KEPT_STEPS):
            return {"passed": False, "reason": f"the process was not kept across the steps (restarts: "
                    f"{', '.join(reasons) or 'none'}; longest on one process: {longest} steps)"}
        return {"passed": True, "reason": f"{len(expected)} requests byte-identical, one process for {longest} steps, "
                f"restarts: {', '.join(reasons) or 'none'}"}
    except Exception as error:
        return {"passed": False, "reason": f"{type(error).__name__}: {str(error)[:300]}"}
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(child(json.loads(sys.argv[1]))), flush=True)
