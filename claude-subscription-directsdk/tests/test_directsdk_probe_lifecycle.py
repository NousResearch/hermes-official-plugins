"""Setup probes own their Claude Code child, and every spawn clears an orphaned OAuth refresh lock.

C#146288: `hermes status` refreshes the model catalog on a daemon thread; the interpreter could exit while that
thread was inside the picker handshake, and the scratch cwd was finalized while claude.exe still ran in it
(Windows WinError 32, a leaked dir, a claude.exe alive for seconds). anthropics/claude-code#95236: a Claude Code
that exits mid-refresh leaves `.oauth_refresh.lock`, which fails every later request until it is removed.
"""
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

# A probe CLI that records its pid and cwd, then hangs without answering.
HANG = textwrap.dedent("""
    import json, os, sys, time
    if sys.argv[1:3] == ["auth", "status"]:
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"})); sys.exit(0)
    open(os.environ["PROBE_LOG"], "w").write(json.dumps({"pid": os.getpid(), "cwd": os.getcwd()}))
    time.sleep(60)
""")


def _alive(pid):
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


# A shim that starts the real CLI in its own process group and exits first.
LAUNCHER = textwrap.dedent("""
    import json, os, subprocess, sys
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(os.environ["PROBE_LOG"], "w").write(json.dumps({"pid": child.pid, "cwd": os.getcwd()}))
""")
# A launcher that leaves a descendant holding the probe's stdout, outside its process group, and exits.
DETACHED_HOLDER = textwrap.dedent("""
    import json, os, subprocess, sys
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], stdout=sys.stdout,
                              **({} if os.name == "nt" else {"start_new_session": True}))
    open(os.environ["PROBE_LOG"], "w").write(json.dumps({"pid": os.getpid(), "cwd": os.getcwd(), "holder": holder.pid}))
""")


@pytest.mark.parametrize("probe, cli", [
    ("discover_models", HANG), ("fetch_usage", HANG),
    pytest.param("fetch_usage", LAUNCHER, marks=pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")),
    ("fetch_usage", DETACHED_HOLDER)], ids=["picker", "usage", "usage-launcher-exits-first", "usage-detached-holder"])
def test_a_timed_out_probe_leaves_no_child_and_no_scratch_dir(profile, tmp_path, probe, cli):
    """Cleanup reaches a descendant a launcher left in its group; a detached holder of stdout is out of the kill's
    reach (Greptile 4512444851), so the read has its own deadline."""
    import psutil
    import directsdk_setup
    script = tmp_path / "claude.py"
    script.write_text(cli, encoding="utf-8")
    log = tmp_path / "probe.json"
    env = {**os.environ, "PATH": os.defpath, "HOME": str(tmp_path), "PROBE_LOG": str(log)}
    started = time.monotonic()
    try:
        result = getattr(directsdk_setup, probe)(command=[sys.executable, str(script)], env=env, timeout=2)
        assert time.monotonic() - started < 10
        assert result is None or result[0] is None
        seen = json.loads(log.read_text())
        deadline = time.monotonic() + 3
        while _alive(seen["pid"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(seen["pid"]), "the probe child outlived the call"
        if cli is HANG:  # a live holder keeps its cwd (on Windows the dir cannot go while it runs)
            assert not os.path.exists(seen["cwd"]), "the scratch cwd was left behind"
    finally:
        left = json.loads(log.read_text()) if log.exists() else {}
        for pid in (left.get("holder"), left.get("pid")):
            if pid and _alive(pid):
                psutil.Process(pid).kill()


def test_an_exiting_interpreter_kills_a_probe_still_running_on_a_daemon_thread(tmp_path):
    """The `hermes status` shape: a daemon thread is inside discover_models when the main thread exits."""
    plugin = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = tmp_path / "claude.py"
    script.write_text(HANG, encoding="utf-8")
    log = tmp_path / "probe.json"
    driver = textwrap.dedent(f"""
        import os, sys, threading, time
        sys.path.insert(0, {plugin!r})
        import directsdk_setup
        threading.Thread(target=directsdk_setup.discover_models, daemon=True,
                         kwargs={{"command": [sys.executable, {str(script)!r}], "timeout": 60}}).start()
        while not os.path.exists({str(log)!r}):
            time.sleep(0.02)
    """)
    env = {**os.environ, "PATH": os.defpath, "HOME": str(tmp_path), "PROBE_LOG": str(log),
           "HERMES_HOME": str(tmp_path / "home")}
    run = subprocess.run([sys.executable, "-c", driver], env=env, capture_output=True, text=True, timeout=60)
    seen = json.loads(log.read_text())
    deadline = time.monotonic() + 5
    while _alive(seen["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(seen["pid"]), "the picker child outlived the interpreter"
    assert "Traceback" not in run.stderr, run.stderr[-2000:]


def _lock(config, age):
    lock = config / ".oauth_refresh.lock"
    lock.mkdir(parents=True)
    stamp = time.time() - age
    os.utime(lock, (stamp, stamp))
    return lock


def test_a_turn_reaps_the_orphaned_lock_of_its_own_login(profile, tmp_path, monkeypatch):
    import directsdk
    from test_directsdk import FAKE
    config = tmp_path / "login"
    lock = _lock(config, 120)
    native = tmp_path / "native.py"
    native.write_text(FAKE, encoding="utf-8")
    for key in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    client = directsdk.Client(command=[sys.executable, str(native)])
    try:
        client.create(model="sonnet", messages=[{"role": "user", "content": "go"}],
                      tools=[{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                                               "parameters": {"type": "object", "properties": {}}}}])
    finally:
        client.close()
    assert not lock.exists()


