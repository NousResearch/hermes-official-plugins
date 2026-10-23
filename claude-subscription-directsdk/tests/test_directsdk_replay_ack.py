"""#122: a failure while native acknowledges replayed history must say what happened."""
import importlib.util
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("directsdk_replay_ack", ROOT / "directsdk.py")
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)

# A prior turn makes the first user frame a replayed one (shouldQuery false) that native must acknowledge.
HISTORY = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "now"}]


def _client(tmp_path, body):
    script = tmp_path / "native.py"
    script.write_text("import sys, time\nsys.stdin.readline()\n" + body)
    return native.Client(command=[sys.executable, str(script)], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})


def _create(client):
    return list(client.create(model="sonnet", messages=HISTORY, timeout=SimpleNamespace(read=30), stream=True))


def test_native_exit_before_replay_ack_reports_stderr_and_exit_code(tmp_path):
    client = _client(tmp_path, "sys.stderr.write('replay frame rejected by native\\n'); sys.stderr.flush(); sys.exit(3)\n")
    try:
        with pytest.raises(RuntimeError) as raised:
            _create(client)
    finally:
        client.close()
    message = str(raised.value)
    assert "Native exited before replay acknowledgment" in message
    assert "exit=3" in message
    assert "replay frame rejected by native" in message


def test_cancel_while_waiting_for_replay_ack_reports_cancellation(tmp_path):
    client = _client(tmp_path, "time.sleep(60)\n")
    timer = threading.Timer(1.0, client.cancel)
    timer.start()
    try:
        with pytest.raises(RuntimeError, match="^Claude request cancelled$"):
            _create(client)
    finally:
        timer.cancel()
        client.close()
