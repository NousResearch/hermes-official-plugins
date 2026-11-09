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


@pytest.mark.parametrize("probe", ["discover_models", "fetch_usage"])
def test_a_timed_out_probe_leaves_no_child_and_no_scratch_dir(profile, tmp_path, probe):
    import directsdk_setup
    script = tmp_path / "claude.py"
    script.write_text(HANG, encoding="utf-8")
    log = tmp_path / "probe.json"
    env = {**os.environ, "PATH": os.defpath, "HOME": str(tmp_path), "PROBE_LOG": str(log)}
    started = time.monotonic()
    result = getattr(directsdk_setup, probe)(command=[sys.executable, str(script)], env=env, timeout=2)
    assert time.monotonic() - started < 15
    assert result is None or result[0] is None
    seen = json.loads(log.read_text())
    assert not _alive(seen["pid"]), "the probe child outlived the call"
    assert not os.path.exists(seen["cwd"]), "the scratch cwd was left behind"


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


@pytest.mark.parametrize("age, contents, reaped", [(120, False, True), (5, False, False), (120, True, False)])
def test_only_a_stale_empty_refresh_lock_is_reaped_before_a_spawn(profile, tmp_path, age, contents, reaped):
    """Older than Claude Code's own 60 s stale window and empty: an orphan. A live holder bumps it every 5 s."""
    import directsdk_setup
    config = tmp_path / "login"
    lock = _lock(config, age)
    if contents:
        (lock / "owner").write_text("x", encoding="utf-8")
    env = {**os.environ, "PATH": os.defpath, "CLAUDE_CONFIG_DIR": str(config), "FAKE_STATE": "{}"}
    directsdk_setup.setup_status(command=[sys.executable, "-c", "print('{}')"], env=env)
    assert lock.exists() is not reaped


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
