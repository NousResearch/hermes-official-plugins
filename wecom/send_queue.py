"""Per-chat FIFO send queues with token-bucket rate limiting for WeCom.

Mirrors OpenClaw's chat-queue.ts (serial per chat) plus a token bucket that
keeps each chat under WeCom's 30 msgs/min/chat limit (errcode 846607). Two
lanes per chat: a normal lane and a high-priority control lane (approval
prompts, finalize frames, error notices) backed by a reserved token pool.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict

logger = logging.getLogger("plugins.platforms.wecom.adapter")


class ChatSendQueueMixin:
    """Expects ``_chat_queues/_chat_workers/_control_queues/_control_workers/_chat_token_usage`` dicts."""

    # Token bucket: 30 tokens/min per chat, split between normal and reserved (control) quota.
    _BUCKET_MAX_TOKENS = 30
    _BUCKET_NORMAL_TOKENS = 24
    _BUCKET_RESERVED_TOKENS = 6

    def _get_token_usage(self, chat_id: str) -> Dict[str, float]:
        """Get or create token usage tracking for a chat."""
        key = str(chat_id or "").strip()
        if key not in self._chat_token_usage:
            self._chat_token_usage[key] = {"normal": 0.0, "reserved": 0.0, "last_reset": time.monotonic()}
        return self._chat_token_usage[key]

    def _bucket_try_consume(self, chat_id: str, is_control: bool = False) -> float:
        """Consume one token. Returns 0 if available, else seconds until the next minute window.

        Normal messages only use the normal quota; control messages use normal
        quota first (don't waste reserved), then the reserved pool.
        """
        usage = self._get_token_usage(chat_id)
        now = time.monotonic()
        if now - usage["last_reset"] > 60.0:  # reset counters every minute
            usage["normal"] = 0.0
            usage["reserved"] = 0.0
            usage["last_reset"] = now
        if usage["normal"] < self._BUCKET_NORMAL_TOKENS:
            usage["normal"] += 1.0
            return 0.0
        if is_control and usage["reserved"] < self._BUCKET_RESERVED_TOKENS:
            usage["reserved"] += 1.0
            return 0.0
        return 60.0 - (now - usage["last_reset"])

    async def _enqueue_chat_send(self, chat_id: str, coro_factory, is_control: bool = False):
        """Enqueue a send task for a chat and await its result (FIFO per chat, parallel across chats).

        Control-lane sends bypass the normal queue so approval prompts are never blocked.
        """
        key = str(chat_id or "").strip()
        lane = "control" if is_control else "normal"
        queues = self._control_queues if is_control else self._chat_queues
        if key not in queues:
            logger.debug("[%s] Creating %s queue + worker for chat %s", self.name, lane, key)
            queues[key] = asyncio.Queue()
            workers = self._control_workers if is_control else self._chat_workers
            workers[key] = asyncio.create_task(self._send_worker(key, is_control))
        queue = queues[key]
        logger.debug("[%s] Enqueuing send for chat %s (lane=%s, qsize=%d)", self.name, key, lane, queue.qsize())
        future = asyncio.get_running_loop().create_future()
        await queue.put((coro_factory, future))
        return await future

    async def _send_worker(self, chat_key: str, is_control: bool) -> None:
        """Per-chat worker: drain one lane's queue under the token bucket."""
        if is_control:
            queue = self._control_queues[chat_key]
        else:
            queue = self._chat_queues[chat_key]
            logger.debug("[%s] Normal send worker started for chat %s", self.name, chat_key)
        try:
            while True:
                coro_factory, future = await queue.get()
                try:
                    wait = self._bucket_try_consume(chat_key, is_control)
                    if wait > 0:
                        if not is_control:
                            logger.debug(
                                "[%s] Normal worker rate-limited for chat %s, waiting %.1fs",
                                self.name, chat_key, wait,
                            )
                        await asyncio.sleep(wait)
                        self._bucket_try_consume(chat_key, is_control)  # re-consume after wait
                    result = await coro_factory()
                    if not future.done():
                        future.set_result(result)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            while not queue.empty():
                try:
                    _, future = queue.get_nowait()
                    if not future.done():
                        future.set_exception(RuntimeError("WeCom adapter shutting down"))
                except asyncio.QueueEmpty:
                    break
