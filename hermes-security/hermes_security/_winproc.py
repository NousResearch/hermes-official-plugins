"""Windows process plumbing for the validation (test) runner.

POSIX code drains pipes with ``selectors`` and kills a session with
``killpg``. Windows ``select`` only accepts sockets, so pipes are drained by
reader threads into a queue, and the process tree lives in a kill-on-close Job
Object (see ``hermes_security._win32.Job``).
"""
import os
import queue
import threading
import time


class Pump:
    """Drain pipes concurrently; deliver chunks on the caller's thread."""

    def __init__(self, pipes, read_size=65536):
        self._queue = queue.Queue()
        self._open = len(pipes)
        self._threads = [threading.Thread(target=self._reader, args=(i, p, read_size), daemon=True)
                         for i, p in enumerate(pipes)]
        for thread in self._threads:
            thread.start()

    def _reader(self, index, pipe, size):
        try:
            while True:
                data = os.read(pipe.fileno(), size)
                if not data:
                    break
                self._queue.put((index, data))
        except OSError:
            pass
        self._queue.put((index, None))

    def run(self, deadline, on_chunk, on_tick=None):
        """Return 'eof', 'timeout', or 'stopped' (on_chunk/on_tick returned False)."""
        while self._open:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 'timeout'
            if on_tick is not None and on_tick() is False:
                return 'stopped'
            try:
                index, data = self._queue.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if data is None:
                self._open -= 1
                continue
            if on_chunk(index, data) is False:
                return 'stopped'
        return 'eof'

    def join(self, timeout=5):
        """After the job is killed every writer is gone, so readers reach EOF."""
        end = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, end - time.monotonic()))
