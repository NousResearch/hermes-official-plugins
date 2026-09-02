"""WeCom native streaming (``msgtype: stream`` via aibot_respond_msg).

Per-turn stream state, per-req_id ack tracking (official SDK's
replyStreamNonBlocking semantics), the stream-level keep-alive heartbeat and
the finalize clock fallback. Mixed into :class:`WeComAdapter`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("plugins.platforms.wecom.adapter")

APP_CMD_RESPONSE = "aibot_respond_msg"

# WeCom binds a ~6-minute lifetime to each reply stream (stream_id + req_id);
# the connection-level ping does NOT refresh it. Past that window updates come
# back 846608 (stream update window) / 846604 (req_id reply-request window) —
# both mean the reply flow is dead and further frames will be rejected.
STREAM_EXPIRED_ERRCODE = 846608
STREAM_REQUEST_EXPIRED_ERRCODE = 846604
STREAM_NOT_SUBSCRIBED_ERRCODE = 846609  # ws connection lost the subscription
# 6000 = finalize raced a newer frame on the same stream_id: the bubble was
# ALREADY replaced, so for a finalize frame this is benign, not a failure.
STREAM_VERSION_CONFLICT_ERRCODE = 6000
MAX_STREAM_CONTENT_LENGTH = 20480  # WeCom server-enforced byte limit per frame
# WeCom SDK has a 100-frame per-reqId queue; cap intermediates at 85 (matches
# the openclaw plugin) so the finalize frame always has room. Past the cap
# intermediates are silently dropped — finalize still sends unconditionally.
MAX_INTERMEDIATE_FRAMES = 85

# Two independent defences against the 6-min stream window, both defaulting
# to the safe side (see docs/wecom-stream-keepalive-*.md):
#   Layer 2 — clock fallback (always on): finalize declines the finish=true
#     frame once the stream is older than STREAM_SAFE_DURATION_SECONDS and
#     returns False so the consumer's send() fallback delivers the content.
#   Layer 1 — keep-alive heartbeat (OFF by default): every
#     STREAM_KEEPALIVE_INTERVAL_SECONDS re-send the accumulated text as a
#     finish=false frame. Never sends a placeholder. Off by default because an
#     extra intermediate frame widens the ack race the double-send
#     coordination depends on.
STREAM_SAFE_DURATION_SECONDS = 330.0
STREAM_KEEPALIVE_INTERVAL_SECONDS = 120.0
STREAM_KEEPALIVE_ENABLED_DEFAULT = False


class WeComStreamExpiredError(RuntimeError):
    """Raised on errcode 846608/846604: the stream/req_id reply flow is dead.

    Callers must fall back to a proactive ``aibot_send_msg``.
    """

    def __init__(self, errcode: int = STREAM_EXPIRED_ERRCODE, errmsg: str = ""):
        super().__init__(f"WeCom stream expired (errcode={errcode}): {errmsg or 'no detail'}")
        self.errcode = errcode
        self.errmsg = errmsg


@dataclass
class ReplyFrame:
    """A reply frame awaiting its aibot_respond_msg ack (FIFO per req_id)."""
    body: Dict[str, Any]
    future: asyncio.Future
    is_final: bool = False
    sent_at: Optional[float] = None


class ReplyQueue:
    """Per-req_id pending-ack tracker: intermediates skip while an ack is pending, finals wait."""
    def __init__(self, req_id: str):
        self.req_id = req_id
        self.pending_ack: Optional[ReplyFrame] = None


class StreamTurn:
    """Per-turn stream state so concurrent messages never share a stream."""
    def __init__(self, chat_id: str, req_id: str):
        self.chat_id = chat_id
        self.req_id = req_id
        self.stream_id = f"stream_{uuid.uuid4().hex[:12]}"
        self.accumulated_text = ""
        self.finalized = False
        self.seeded = False  # seed frame sent (prevents double seed → errcode 6000)
        self.start_time = time.monotonic()
        self.expired = False
        # Last content ACTUALLY sent (not skipped) — finalize uses it to avoid a
        # duplicate-content final frame that WeCom silently drops.
        self.last_sent_content: str = ""
        self._intermediate_frames_sent: int = 0
        # Keep-alive TimerHandle; MUST be cancelled on every turn-exit path
        # (finalize / expired / error / cleanup) so it never fires on a dead turn.
        self.keepalive_handle: Optional[asyncio.TimerHandle] = None


def _stream_of(body: Dict[str, Any]) -> Dict[str, Any]:
    return body.get("stream", {}) if isinstance(body.get("stream"), dict) else {}


class WeComStreamMixin:
    """Native streaming for WeComAdapter (expects ``_ws``, ``_send_json``, ``_reply_queues``,
    ``_stream_turns``, ``_stream_expired_chats``, ``_last_chat_req_ids`` and the
    ``_stream_*`` config attributes set in ``__init__``)."""

    MAX_STREAM_CONTENT_LENGTH = MAX_STREAM_CONTENT_LENGTH

    # Ack timeout matches the official plugin's REPLY_SEND_TIMEOUT_MS = 15_000; a
    # shorter window widened the race where the final-frame ack is still in
    # flight while the gateway's normal final send fires → duplicate messages.
    _REPLY_ACK_TIMEOUT = 15.0

    # ── Per-req_id reply queue (ack tracking) ────────────────────────────

    async def _send_reply_queued(
        self, reply_req_id: str, body: Dict[str, Any], *, is_final: bool = False, skip_if_pending: bool = False,
    ) -> Dict[str, Any]:
        """Send a reply via aibot_respond_msg with per-req_id ack tracking.

        is_final: wait for any pending ack before sending, then await our own ack.
        skip_if_pending: return ``{"skipped": True}`` if a prior frame's ack is pending.
        """
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")
        normalized = str(reply_req_id or "").strip()
        if not normalized:
            raise ValueError("reply_req_id is required")

        queue = self._reply_queues.get(normalized)
        if queue is None:
            queue = ReplyQueue(normalized)
            self._reply_queues[normalized] = queue

        if skip_if_pending and queue.pending_ack is not None:
            return {"skipped": True, "errcode": 0, "errmsg": "pending_ack"}

        if is_final and queue.pending_ack is not None:
            pending_frame = queue.pending_ack
            _pending_stream = _stream_of(pending_frame.body)
            pending_desc = (self.name, normalized, _pending_stream.get("id", "N/A"), _pending_stream.get("finish", "N/A"))
            logger.debug(
                "[%s] _send_reply_queued: final waiting for pending ack drain — "
                "req_id=%s pending_stream_id=%s pending_finish=%s pending_sent_at=%.1fs_ago",
                *pending_desc, time.monotonic() - (pending_frame.sent_at or time.monotonic()),
            )
            try:
                await asyncio.wait_for(asyncio.shield(pending_frame.future), timeout=self._REPLY_ACK_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] Reply ack timeout waiting for pending (req_id=%s) — "
                    "pending_stream_id=%s pending_finish=%s elapsed=%.1fs. "
                    "Possible causes: ack cmd filtered, ack req_id mismatch, or WeCom did not ack.",
                    *pending_desc, time.monotonic() - (pending_frame.sent_at or time.monotonic()),
                )
            except Exception:
                pass
            queue.pending_ack = None  # resolved or timed out either way

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        frame = ReplyFrame(body=body, future=future, is_final=is_final)
        frame.sent_at = time.monotonic()
        # Register pending BEFORE sending so an ack arriving mid-send is routed.
        # Re-attach `queue` too: while the final frame awaited the drain above, the
        # intermediate ack may have popped the whole queue out of _reply_queues,
        # leaving our local reference orphaned (its ack would then be Unrouted →
        # 15s timeout).
        self._reply_queues[normalized] = queue
        queue.pending_ack = frame

        _stream_info = _stream_of(body)
        logger.debug(
            "[%s] _send_reply_queued: req_id=%s is_final=%s skip_if_pending=%s stream_id=%s finish=%s content_len=%d",
            self.name, normalized, is_final, skip_if_pending,
            _stream_info.get("id", "N/A"), _stream_info.get("finish", "N/A"), len(_stream_info.get("content", "") or ""),
        )

        try:
            await self._send_json({"cmd": APP_CMD_RESPONSE, "headers": {"req_id": normalized}, "body": body})
        except Exception:
            # Nobody awaits the future on this branch — cancel it rather than
            # leave a "Future exception was never retrieved" log.
            if queue.pending_ack is frame:
                queue.pending_ack = None
                self._reply_queues.pop(normalized, None)
            if not future.done():
                future.cancel()
            raise

        if not is_final:
            # Fire-and-forget; pending_ack stays registered so later frames can skip.
            return {"errcode": 0, "errmsg": "sent_nonblocking"}
        try:
            return await asyncio.wait_for(future, timeout=self._REPLY_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            # The bytes went out (send did not raise) but the ack is late — in
            # practice WeCom has already rendered the message. Raising here made
            # the upper layer fall back to a markdown send and produced duplicates;
            # match the official plugin: warn and treat as delivered.
            logger.warning(
                "[%s] Final frame ack timeout (req_id=%s) — treating as "
                "delivered (matches official wecom-openclaw-plugin "
                "behaviour). No fallback send.",
                self.name, normalized,
            )
            return {"errcode": 0, "errmsg": "ack_timeout_assumed_delivered", "ack_pending": True}
        finally:
            self._release_pending(queue, normalized, frame)

    def _release_pending(self, queue: ReplyQueue, req_id: str, frame: ReplyFrame) -> None:
        """Clear ``frame`` if it is still the pending ack; drop the queue once empty."""
        if queue.pending_ack is frame:
            queue.pending_ack = None
        if queue.pending_ack is None:
            self._reply_queues.pop(req_id, None)

    def _resolve_reply_ack(self, req_id: str, payload: Dict[str, Any]) -> bool:
        """Resolve a pending reply ack. Returns True if handled."""
        queue = self._reply_queues.get(req_id)
        if queue is None or queue.pending_ack is None:
            return False
        frame = queue.pending_ack
        if not frame.future.done():
            _body = payload.get("body", {}) if isinstance(payload.get("body"), dict) else {}
            logger.debug(
                "[%s] _resolve_reply_ack: resolved req_id=%s is_final=%s "
                "elapsed=%.2fs errcode=%s",
                self.name, req_id, frame.is_final,
                time.monotonic() - (frame.sent_at or time.monotonic()),
                _body.get("errcode", "N/A"),
            )
            frame.future.set_result(payload)
        self._release_pending(queue, req_id, frame)
        return True

    def _fail_reply_queues(self, error: Exception) -> None:
        """Fail all pending reply acks (disconnect/error)."""
        for queue in list(self._reply_queues.values()):
            if queue.pending_ack and not queue.pending_ack.future.done():
                queue.pending_ack.future.set_exception(error)
        self._reply_queues.clear()

    # ── Turn registry ────────────────────────────────────────────────────

    def _resolve_stream_req_id(self, chat_id: str, reply_to: Optional[str]) -> Optional[str]:
        """Explicit ``reply_to`` (cached message id) → last inbound req_id for the chat → None."""
        req_id = self._reply_req_id_for_message(reply_to)
        if req_id:
            return req_id
        return self._last_chat_req_ids.get(str(chat_id or "").strip()) or None

    @staticmethod
    def _cancel_keepalive(turn: StreamTurn) -> None:
        if turn.keepalive_handle is not None:
            try:
                turn.keepalive_handle.cancel()
            except Exception:
                pass
            turn.keepalive_handle = None

    def _retire_turn(self, turn: StreamTurn, turn_id: Optional[str]) -> None:
        """Single choke point for "turn is dead": cancel the timer, then drop it from the registry."""
        self._cancel_keepalive(turn)
        self._stream_turns.pop(f"{turn.chat_id}:{turn_id or turn.req_id}", None)

    def _expire_turn(self, turn: StreamTurn, turn_id: Optional[str]) -> None:
        turn.expired = True
        self._retire_turn(turn, turn_id)
        self._stream_expired_chats.add(turn.chat_id)

    def _find_active_turn_for_chat(self, chat_id: str) -> Optional[StreamTurn]:
        for turn in self._stream_turns.values():
            if turn.chat_id == chat_id and not turn.finalized:
                return turn
        return None

    # ── Stream-level keep-alive (Layer 1) ────────────────────────────────

    def _arm_keepalive(self, turn: StreamTurn, *, turn_id: Optional[str]) -> None:
        """Arm the keep-alive timer if enabled and not already armed (idempotent)."""
        if not self._stream_keepalive_enabled or turn.finalized or turn.expired:
            return
        if turn.keepalive_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        turn.keepalive_handle = loop.call_later(
            self._stream_keepalive_interval_seconds, self._on_keepalive_fire, turn, turn_id,
        )

    def _on_keepalive_fire(self, turn: StreamTurn, turn_id: Optional[str]) -> None:
        turn.keepalive_handle = None
        if turn.finalized or turn.expired:
            return
        try:
            asyncio.ensure_future(self._keepalive_send(turn, turn_id))
        except RuntimeError:
            pass

    async def _keepalive_send(self, turn: StreamTurn, turn_id: Optional[str]) -> None:
        """Re-send the accumulated text as finish=false to refresh the server window, then re-arm.

        Never sends a placeholder: with no accumulated text the tick is skipped
        (Layer 2 handles content-less turns). On 846604/846608 the turn is
        retired so finalize takes the Layer 2 fallback; no re-arm.
        """
        if turn.finalized or turn.expired:
            return
        if turn._intermediate_frames_sent >= MAX_INTERMEDIATE_FRAMES:
            return  # no room left for intermediates; let finalize / Layer 2 run
        content = turn.accumulated_text or ""
        if not content.strip():
            self._arm_keepalive(turn, turn_id=turn_id)
            return
        try:
            await self._send_stream_reply(turn.req_id, turn.stream_id, content, finish=False)
        except WeComStreamExpiredError:
            self._expire_turn(turn, turn_id)
            return
        except Exception as exc:
            logger.debug(
                "[%s] keep-alive send failed (chat=%s, turn=%s): %s",
                self.name, turn.chat_id, turn.stream_id, exc,
            )
            self._arm_keepalive(turn, turn_id=turn_id)  # transient — retry next interval
            return
        turn.last_sent_content = content
        self._arm_keepalive(turn, turn_id=turn_id)

    # ── Frame sending ────────────────────────────────────────────────────

    @staticmethod
    def _truncate_stream_content(content: str, limit: int) -> str:
        """Truncate to ``limit`` UTF-8 bytes (WeCom caps frames by bytes, not codepoints)."""
        encoded = content.encode("utf-8")
        if len(encoded) <= limit:
            return content
        return encoded[:limit].decode("utf-8", errors="ignore")

    async def _send_stream_reply(
        self, reply_req_id: str, stream_id: str, content: str, finish: bool = False,
    ) -> Dict[str, Any]:
        """Send one ``msgtype: "stream"`` frame.

        Intermediate frames are non-blocking with skip-if-pending (cumulative
        text means nothing is lost). The final frame drains any pending ack
        first, then awaits its own ack so 846608/6000 are detected reliably.
        Raises WeComStreamExpiredError on 846608/846604.
        """
        truncated = self._truncate_stream_content(content or "", self.MAX_STREAM_CONTENT_LENGTH)
        if len(content or "") != len(truncated):
            logger.warning("[%s] Stream content truncated for stream_id=%s", self.name, stream_id)
        body: Dict[str, Any] = {"msgtype": "stream", "stream": {"id": stream_id, "finish": bool(finish), "content": truncated}}
        if not finish:
            return await self._send_reply_queued(reply_req_id, body, is_final=False, skip_if_pending=True)

        response = await self._send_reply_queued(reply_req_id, body, is_final=True, skip_if_pending=False)
        errcode = response.get("errcode", 0)
        if errcode in (STREAM_EXPIRED_ERRCODE, STREAM_REQUEST_EXPIRED_ERRCODE):
            raise WeComStreamExpiredError(errcode=errcode, errmsg=str(response.get("errmsg") or ""))
        if errcode == STREAM_VERSION_CONFLICT_ERRCODE:
            # Content is already on screen; raising would pop the turn and cause
            # a duplicate standalone send(). Absorbing makes finalize retry safe.
            logger.info(
                "[%s] finalize hit errcode 6000 (version conflict) — bubble "
                "already replaced by a newer frame; treating as delivered.",
                self.name,
            )
            return response
        self._raise_for_wecom_error(response, "send stream reply")
        return response

    async def send_stream_frame(
        self, text: str, *, finalize: bool = False, chat_id: Optional[str] = None, reply_to: Optional[str] = None, **kwargs,
    ) -> bool:
        """Entry point for the gateway streaming consumer.

        First call for a turn resolves the req_id, creates the StreamTurn and
        seeds the typing bubble; later calls push cumulative text (not deltas);
        ``finalize=True`` closes the stream and drops turn state. ``turn_id``
        (kwarg) keys the turn by (chat, turn_id) so concurrent consumers
        (/background, subagents) never share a stream.

        Returns False when the stream is unavailable (no req_id, expired,
        transport error) — the caller should fall back to :meth:`send`.
        """
        chat = (chat_id or "").strip()
        if not chat:
            logger.warning("[%s] send_stream_frame: chat_id required", self.name)
            return False
        turn_id = kwargs.get("turn_id")
        # Chat-level expiry only blocks NEW turn creation; a known turn_id may
        # still finalize after another turn in the chat expired.
        if not turn_id and chat in self._stream_expired_chats:
            return False
        if finalize:
            # Finalize counts toward 30/min — control lane so it is never blocked.
            return await self._enqueue_chat_send(
                chat,
                lambda: self._send_stream_frame_inner(text, chat=chat, reply_to=reply_to, finalize=True, turn_id=turn_id),
                is_control=True,
            )
        # Intermediate frames don't count toward the quota: no queue, no rate limit.
        return await self._send_stream_frame_inner(text, chat=chat, reply_to=reply_to, finalize=False, turn_id=turn_id)

    def _locate_turn(
        self, chat: str, reply_to: Optional[str], finalize: bool, turn_id: Optional[str],
    ) -> Optional[StreamTurn]:
        """Find or create the StreamTurn for a frame; None means "stream unavailable".

        A turn locks to its req_id at creation even if ``_last_chat_req_ids``
        changes mid-turn (e.g. the user sends /approve).
        """
        if turn_id:
            turn = self._stream_turns.get(f"{chat}:{turn_id}")
            if turn:
                return turn
            # finalize must NOT create a turn: if it was cleaned up (e.g. 6000) the
            # caller should fall back rather than send a fresh seed + finish.
            if finalize:
                logger.debug(
                    "[%s] send_stream_frame: cannot finalize non-existent turn (turn_id=%s, chat=%s)",
                    self.name, turn_id, chat,
                )
                return None
        else:
            # No turn_id (direct callers): reuse the chat's active turn if any.
            existing_turn = self._find_active_turn_for_chat(chat)
            if existing_turn and not existing_turn.finalized:
                logger.debug(
                    "[%s] send_stream_frame: reusing existing turn %s for chat %s",
                    self.name, existing_turn.stream_id, chat,
                )
                return existing_turn

        suffix = f" (turn_id={turn_id})" if turn_id else ""
        if chat in self._stream_expired_chats:
            logger.debug("[%s] send_stream_frame: chat %s is expired, cannot create new turn%s", self.name, chat, suffix)
            return None
        req_id = self._resolve_stream_req_id(chat, reply_to)
        if not req_id:
            logger.debug("[%s] send_stream_frame: no req_id available for chat %s%s", self.name, chat, suffix)
            return None
        key = f"{chat}:{turn_id or req_id}"
        turn = (None if turn_id else self._stream_turns.get(key)) or StreamTurn(chat, req_id)
        self._stream_turns[key] = turn
        logger.debug(
            "[%s] send_stream_frame: created new turn %s (%s) for chat %s",
            self.name, turn.stream_id, f"turn_id={turn_id}, req_id={req_id}" if turn_id else f"req_id={req_id}", chat,
        )
        return turn

    async def _send_stream_frame_inner(
        self, text: str, *, chat: str, reply_to: Optional[str] = None, finalize: bool = False, turn_id: Optional[str] = None,
    ) -> bool:
        """Stream frame logic with per-turn state (see ``send_stream_frame``)."""
        turn: Optional[StreamTurn] = None
        try:
            turn = self._locate_turn(chat, reply_to, finalize, turn_id)
            if turn is None or turn.expired:
                return False

            if not turn.seeded and not turn.finalized:
                # Seed with the official plugin's THINKING_MESSAGE (<think></think>)
                # so the client shows a reasoning turn; the seeded flag prevents a
                # double seed (errcode 6000) since the consumer seeds too.
                await self._send_stream_reply(turn.req_id, turn.stream_id, "<think></think>", finish=False)
                turn.seeded = True
                self._arm_keepalive(turn, turn_id=turn_id)
                if not text and not finalize:
                    return True  # consumer's explicit seed call — nothing more to send

            if finalize:
                # Layer 2 clock fallback: an old stream would almost certainly hit
                # 846604/846608 on finish=true, so decline up front and let the
                # consumer's send() fallback deliver exactly once. SKIPPED when
                # Layer 1 keep-alive is on — the heartbeat has been refreshing the
                # window, so age alone does not mean dead, and declining a live
                # stream would re-deliver content already on screen. A truly dead
                # stream still raises WeComStreamExpiredError below.
                if not self._stream_keepalive_enabled:
                    stream_age = time.monotonic() - turn.start_time
                    if stream_age >= self._stream_safe_duration_seconds:
                        logger.info(
                            "[%s] Stream age %.0fs >= safe duration %.0fs for chat "
                            "%s — declining finalize frame, falling back to "
                            "proactive send (Layer 2 clock fallback).",
                            self.name, stream_age,
                            self._stream_safe_duration_seconds, chat,
                        )
                        self._expire_turn(turn, turn_id)
                        return False

                self._cancel_keepalive(turn)
                # WeCom silently drops (no ack) a final frame identical to the last
                # intermediate — append a zero-width space so the content differs.
                final_text = text
                if text and text == turn.last_sent_content:
                    final_text = text + "\u200b"
                await self._send_stream_reply(turn.req_id, turn.stream_id, final_text, finish=True)
                turn.finalized = True
                self._stream_turns.pop(f"{chat}:{turn_id or turn.req_id}", None)
            else:
                # Fire-and-forget: the gateway decides when to push (identity dedup
                # in stream_consumer.py); no adapter-side buffering.
                turn.accumulated_text = text
                if turn._intermediate_frames_sent >= MAX_INTERMEDIATE_FRAMES:
                    return True  # cap reached — drop intermediates; finalize drains the rest
                if text == turn.last_sent_content:
                    return True
                await self._send_stream_reply(turn.req_id, turn.stream_id, text, finish=False)
                turn._intermediate_frames_sent += 1
                turn.last_sent_content = text
            return True

        except WeComStreamExpiredError:
            # An intermediate frame is overwritten by the next cumulative/final
            # frame anyway; flipping the turn expired here would trip the
            # consumer's send() fallback and duplicate the bubble. Only a FINAL
            # frame's expiry means content is genuinely missing.
            if not finalize:
                logger.info(
                    "[%s] Intermediate stream frame expired (errcode=%d) for chat %s — dropping frame, stream stays live",
                    self.name, STREAM_EXPIRED_ERRCODE, chat,
                )
                return True
            logger.info(
                "[%s] Stream expired (errcode=%d) for chat %s — switching to proactive send",
                self.name, STREAM_EXPIRED_ERRCODE, chat,
            )
            if turn is not None:
                self._expire_turn(turn, turn_id)
            else:
                self._stream_expired_chats.add(chat)
            return False
        except Exception as exc:
            if not finalize:  # same intermediate/final split as above
                logger.info(
                    "[%s] Intermediate stream frame failed (chat=%s): %s — dropping frame, stream stays live",
                    self.name, chat, exc,
                )
                return True
            logger.warning("[%s] Stream frame failed (chat=%s): %s", self.name, chat, exc)
            if turn is not None:
                self._retire_turn(turn, turn_id)
            return False

    def supports_native_streaming(
        self, chat_type: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Stream frames work in DMs and groups alike (groups just need a cached inbound req_id)."""
        del chat_type, metadata
        return True

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op: the stream consumer's seed frame triggers WeCom typing; repeated
        send_typing calls would open orphan streams."""
        del chat_id, metadata
