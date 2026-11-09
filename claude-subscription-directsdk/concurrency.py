"""The one admission point for Claude Code turn processes: at most ``max_concurrent`` live at once per Hermes process.

A turn holds its slot from just before its ``claude`` starts (its extra-usage check included) until that process has
exited and been reaped. The picker handshake, ``/usage`` and ``auth status`` take none: they send no Messages
request, end within seconds, and ``/usage`` must answer while a fan-out holds every slot.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import types

logger = logging.getLogger(__name__)

# Hermes imports this plugin once per profile home (one module per HERMES_HOME), so plain module globals would give
# each profile of a multiplexed gateway its own pool. Every copy finds the one pool under this name.
_KEY = '_claude_subscription_directsdk_slots_v1'
_pool = types.ModuleType(_KEY)
_pool.cond, _pool.held, _pool.waiting, _pool.warned_at = threading.Condition(), 0, [], None
_pool = sys.modules.setdefault(_KEY, _pool)

WAIT_NOTICE_S = 2.0
_WARN_EVERY_S = 60.0
_POLL_S = .1
_BEAT_S = 1.0


class _Waiter:
    """Compared by identity: two waiters with the same limit are still two places in the queue."""
    __slots__ = ('limit',)

    def __init__(self, limit):
        self.limit = limit


class Slot:
    """One held place in the pool. ``release()`` is idempotent."""

    def __init__(self):
        self._held = True

    def release(self):
        with _pool.cond:
            if self._held:
                self._held = False
                _pool.held -= 1
                _pool.cond.notify_all()


def _take(me):
    """Under the lock: FIFO, except a waiter whose own (profile's) limit is full lets the next one by."""
    if next((w for w in _pool.waiting if w.limit > _pool.held), None) is not me:
        return None
    _pool.waiting.remove(me)
    _pool.held += 1
    return Slot()


def _notice(running, limit, waited):
    """One warning a minute while calls queue (a fan-out queues many at once); info lines for the rest."""
    now = time.monotonic()
    loud = _pool.warned_at is None or now - _pool.warned_at >= _WARN_EVERY_S
    if loud:
        _pool.warned_at = now
    (logger.warning if loud else logger.info)(
        'Claude Code call waiting %.0f s for a free slot: %d running in this Hermes process, max_concurrent %d. '
        'Raise the plugin setting max_concurrent to run more at once.', waited, running, limit)


def acquire(limit, cancelled, timeout, beat):
    """A generator that returns a held ``Slot``. While it waits it yields ``beat()`` about once a second: a
    liveness chunk, without which Hermes' stale timer kills the waiting call and sends it to the back of the queue.

    Raises the plugin's cancel error within ``_POLL_S`` of ``cancelled`` being set, and ``TimeoutError`` once it
    has waited ``timeout`` seconds."""
    me, started = _Waiter(limit), time.monotonic()
    next_beat, noticed = started + _BEAT_S, False
    with _pool.cond:
        _pool.waiting.append(me)
    try:
        while True:
            with _pool.cond:
                slot = _take(me)
                if slot is None:
                    _pool.cond.wait(_POLL_S)
                    slot = _take(me)
                running = _pool.held
            waited = time.monotonic() - started
            if slot is not None:
                if noticed:
                    logger.info('Claude Code call started after waiting %.1f s for a free slot', waited)
                return slot
            if cancelled.is_set():
                raise RuntimeError('Claude request cancelled')
            if waited >= timeout:
                raise TimeoutError(f'Claude request timed out after {waited:.0f} s waiting for a free Claude Code '
                                   f'slot ({running} running, max_concurrent {limit})')
            if not noticed and waited >= WAIT_NOTICE_S:
                noticed = True
                _notice(running, limit, waited)
            if time.monotonic() >= next_beat:
                next_beat = time.monotonic() + _BEAT_S
                yield beat()
    finally:
        with _pool.cond:
            if me in _pool.waiting:
                _pool.waiting.remove(me)
            _pool.cond.notify_all()
