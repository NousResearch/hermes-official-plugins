"""Session-mode paths that need no Claude Code process: failures before a launch and errors the reader thread hands over."""
from collections import deque
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk


def test_a_control_write_the_reader_thread_gave_up_on_reaches_the_step_as_a_timeout():
    """The reader thread answers native's later tool calls itself. When that bounded write gives up, the step must
    see the timeout error, not the bad-output error every other reader failure maps to."""
    native = object.__new__(directsdk.Native)
    native.events, native.stderr = queue.Queue(), deque()
    native.events.put(TimeoutError('Claude request timed out'))
    with pytest.raises(TimeoutError, match='Claude request timed out'):
        native.receive(SimpleNamespace(cancelled=threading.Event()), 1)
