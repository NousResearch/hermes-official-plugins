"""``hermes mcp-serve --bot-chat``: talk to this install's Bot Mode agents from an MCP client.

Off by default. With ``--bot-chat`` an MCP client gets three tools:

* ``bots_list``: the profiles that are in Bot Mode (``ui_meta['hermes-bots']`` in profile.yaml) with
  their @handle, friendly name and role line. Read from disk on every call, never cached, with the
  same roster and Bot Mode predicate core uses (``tools/bot_mode_probe``).
* ``bot_message_send``: DM one of them in its canonical "Bot Chat". The message takes core's own
  local delivery path (``tools/bot_mode_dm``: the DM file, the pinned live-owner intent, admission
  through the profile authority, the delivery receipt), signed ``Message from 🤖 <sender> (@<handle>):``
  with a ``bot:<handle>`` author, the way one bot messages another. Returns the reply when it lands
  within ``wait_seconds``, else the ``delivery_id``.
* ``bot_reply_get``: the reply for a ``delivery_id``, or that it is still pending.

Only profiles in Bot Mode are targets: anything else is refused, so this never opens a chat in a
profile that did not opt in. Delivery needs the target profile's gateway running (it owns the Bot
Chat); nothing here starts one. The runner core spawns for ``message_agent`` is not used: it prints
its outcome to stdout, which is this server's MCP wire, and an MCP client has no completion
notifications to wake on. The wait happens here instead, on the receipt the runner would poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.mcp_serve.bot_chat")

BOT_CHAT_TOOL_NAMES: tuple[str, ...] = ("bots_list", "bot_message_send", "bot_reply_get")

DEFAULT_SENDER = "MCP client"
DEFAULT_WAIT_SECONDS = 60
MAX_WAIT_SECONDS = 300
_SENDER_MAX_CHARS = 64
_DELIVERY_ID_RE = re.compile(r"[0-9a-f]{32,64}")
_PENDING_STATUSES = ("queued", "claimed")


def _coerce_wait(value: Any, default: int) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = float(default)
    if seconds != seconds:  # NaN
        seconds = float(default)
    return max(0.0, min(seconds, float(MAX_WAIT_SECONDS)))


def _install_root() -> Path:
    """~/.hermes for every profile: a named profile's server still sees its siblings."""
    from tools.bot_mode_probe import _default_home, _hermes_root

    return _hermes_root(Path(_default_home()))


def _bot_roster(root: Path) -> tuple[list[str], list[tuple[str, Path]]]:
    """(every profile folder id, the Bot Mode ones as (name, dir)), read fresh from disk."""
    from tools.bot_mode_probe import _is_bot_managed, _roster

    roster = _roster(root)
    return [name for name, _d in roster], [(name, d) for name, d in roster if _is_bot_managed(d)]


def _clean_sender(sender_name: Optional[str]) -> str:
    name = " ".join(str(sender_name or "").split())[:_SENDER_MAX_CHARS].strip()
    return name or DEFAULT_SENDER


def _sender_handle(name: str, root: Path) -> str:
    """The @handle the signature and author id carry: the sender name slugified like a Desktop mention.
    It never equals a form a local profile answers to (folder id, @handle, friendly-name slug) nor a
    reserved mention token, so an MCP sender cannot sign as a teammate; a colliding slug gets ``mcp-``."""
    from tools.bot_mode_probe import _RESERVED_ALIASES, local_taken_forms

    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")[:48].strip("-_")
    if not slug:
        return "mcp-client"
    taken = local_taken_forms(root) | set(_RESERVED_ALIASES)
    return f"mcp-{slug}" if slug in taken else slug


def _resolve_bot(profile: str, root: Path) -> tuple[Optional[tuple[str, Path]], Optional[str]]:
    """((folder id, dir), None) for a Bot Mode target, else (None, error JSON)."""
    from tools.bot_mode_dm import _resolve_local_name
    from tools.bot_mode_probe import _handle

    names, bots = _bot_roster(root)
    handles = [f"@{_handle(name, names)}" for name, _d in bots]
    raw = str(profile or "").strip().lstrip("@")
    if not raw:
        return None, json.dumps({"error": "profile is required.", "bots": handles})
    resolved = _resolve_local_name(raw, names, root)
    if resolved is None:
        return None, json.dumps({"error": f"No profile named {raw!r} on this install.", "bots": handles})
    match = next(((name, d) for name, d in bots if name == resolved), None)
    if match is None:
        return None, json.dumps({"error": f"Profile {resolved!r} is not in Bot Mode; only Bot Mode profiles "
                                          "can be messaged.", "bots": handles})
    return match, None


class BotChat:
    """State for one server process: where each delivery it sent lives, so ``bot_reply_get`` and the
    cleanup of a settled delivery need no roster scan. A delivery this process did not send is found by
    its receipt in the Bot Mode profiles' mailboxes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sent: dict[str, dict] = {}  # delivery_id -> {"home", "dm_file", "to"}

    # -- tools ------------------------------------------------------------------------------------

    def bots_list(self) -> str:
        """List the Hermes agents on this install that are in Bot Mode.

        Returns each bot's profile id, @handle, display name and role (its Bot Mode
        title and profile description), read live from disk on every call. Use the
        profile or handle with bot_message_send.
        """
        from tools.bot_mode_probe import _display_name, _handle, _profile_role

        try:
            root = _install_root()
            names, bots = _bot_roster(root)
            listed = [{"profile": name, "handle": f"@{_handle(name, names)}",
                       "name": _display_name(name, d, names), "role": _profile_role(d)} for name, d in bots]
        except Exception as exc:
            logger.exception("bots_list failed")
            return json.dumps({"error": f"Could not read the Bot Mode roster: {exc}"})
        return json.dumps({"count": len(listed), "bots": listed}, indent=2, ensure_ascii=False)

    async def bot_message_send(self, profile: str, message: str, sender_name: Optional[str] = None,
                               wait_seconds: int = DEFAULT_WAIT_SECONDS) -> str:
        """Send a message to a Bot Mode agent's "Bot Chat" and return its reply.

        The message is delivered the way one bot messages another, signed
        "Message from 🤖 <sender_name> (@<handle>):". If the agent replies within
        wait_seconds the reply is returned (status "settled"); otherwise the result
        has status "queued" or "claimed" and a delivery_id: fetch the reply later
        with bot_reply_get, do not resend. Only profiles listed by bots_list can be
        messaged, and the target's gateway must be running.

        Args:
            profile: Target bot: profile id or @handle from bots_list (a friendly name also works)
            message: The message text; compose it for that agent
            sender_name: Name to sign the message with (default "MCP client")
            wait_seconds: How long to wait for the reply before returning the delivery_id (default 60, max 300)
        """
        from tools.bot_mode_dm import MESSAGE_MAX_CHARS

        body = str(message or "").strip()
        if not body:
            return json.dumps({"error": "message is required."})
        if len(body) > MESSAGE_MAX_CHARS:
            return json.dumps({"error": f"message too long ({len(body)} chars > {MESSAGE_MAX_CHARS})."})
        wait = _coerce_wait(wait_seconds, DEFAULT_WAIT_SECONDS)
        try:
            admitted = await asyncio.to_thread(self._admit, profile, body, sender_name)
        except Exception as exc:  # an MCP tool call reports the failure, it never kills the server
            logger.exception("bot_message_send failed")
            return json.dumps({"error": f"Delivery could not be started: {exc}"})
        if "error" in admitted:
            return json.dumps(admitted, ensure_ascii=False)
        return await self._outcome(admitted["delivery_id"], wait, admitted)

    async def bot_reply_get(self, delivery_id: str, wait_seconds: int = 0) -> str:
        """Get the reply to a message sent with bot_message_send.

        Returns the reply when the agent has answered (status "settled"), the
        failure when the delivery failed, or status "queued"/"claimed" with
        pending=true while the agent is still working on it.

        Args:
            delivery_id: The delivery_id returned by bot_message_send
            wait_seconds: How long to wait for a pending reply before returning (default 0, max 300)
        """
        key = str(delivery_id or "").strip().lower()
        if not _DELIVERY_ID_RE.fullmatch(key):
            return json.dumps({"error": f"Invalid delivery_id: {delivery_id!r}"})
        return await self._outcome(key, _coerce_wait(wait_seconds, 0), None)

    # -- delivery -----------------------------------------------------------------------------------

    def _admit(self, profile: str, body: str, sender_name: Optional[str]) -> dict:
        """Write the DM file and admit it through the target's live Bot Chat owner (core's path).
        Returns {"delivery_id", "status", "to"} or a payload with "error"."""
        from tools.bot_mode_dm import (LiveTargetUnavailableError, _admit_live_dm, _dm_delivery_id,
                                       _settled_no_send, _write_dm_file)
        from tools.bot_mode_probe import _handle

        root = _install_root()
        target, error = _resolve_bot(profile, root)
        if error:
            return json.loads(error)
        name, profile_dir = target
        names, _bots = _bot_roster(root)
        label = f"@{_handle(name, names)}"
        sender = _clean_sender(sender_name)
        handle = _sender_handle(sender, root)
        content = f"Message from 🤖 {sender} (@{handle}): {body}"
        author = {"id": f"bot:{handle}", "name": handle, "is_bot": True}

        dm_file = _write_dm_file(content)
        try:
            record = _admit_live_dm(profile_dir, dm_file, author)
        except LiveTargetUnavailableError as exc:  # held included: a definite no-send
            return {**json.loads(_settled_no_send(dm_file, exc)), "to": label}
        except Exception as exc:
            # The admission may have happened: keep the payload as evidence, like core does.
            return {"status": "ambiguous", "delivery_id": _dm_delivery_id(dm_file), "to": label,
                    "error": f"Live delivery admission could not be confirmed: {exc}. Do not resend.",
                    "evidence_file": dm_file}
        if record is None:  # the target profile has no session store yet
            return {**json.loads(_settled_no_send(dm_file, LiveTargetUnavailableError(
                "canonical Bot Chat target is unavailable"))), "to": label}
        delivery_id = record["delivery_id"]
        with self._lock:
            self._sent[delivery_id] = {"home": record["profile_home"], "dm_file": dm_file, "to": label}
        return {"delivery_id": delivery_id, "status": record.get("status", "queued"), "to": label}

    def _locate(self, delivery_id: str) -> Optional[dict]:
        with self._lock:
            known = self._sent.get(delivery_id)
        if known:
            return known
        from tools.bot_live_delivery import _poll_receipt
        from tools.bot_mode_probe import _handle

        names, bots = _bot_roster(_install_root())
        for name, d in bots:
            with contextlib.suppress(Exception):
                if _poll_receipt(d, delivery_id) is not None:
                    return {"home": str(Path(d).resolve()), "dm_file": None, "to": f"@{_handle(name, names)}"}
        return None

    async def _outcome(self, delivery_id: str, wait: float, admitted: Optional[dict]) -> str:
        from tools.bot_live_delivery import await_delivery_async

        where = await asyncio.to_thread(self._locate, delivery_id)
        if where is None:
            return json.dumps({"error": f"Unknown delivery_id: {delivery_id}", "delivery_id": delivery_id})
        record = await await_delivery_async(where["home"], delivery_id, wait)
        status = record["status"] if record else (admitted or {}).get("status", "ambiguous")
        result: dict[str, Any] = {"status": status, "delivery_id": delivery_id, "to": where["to"]}
        result.update({k: record[k] for k in ("reply", "error", "reason") if record and record.get(k)})
        if status in _PENDING_STATUSES:
            result.update(pending=True, detail="No reply yet. Call bot_reply_get with this delivery_id later; "
                                               "do not resend.")
        elif status == "settled":
            result.setdefault("reply", "")
            self._settle(delivery_id, where)
        elif status == "ambiguous":
            result.setdefault("detail", "Delivery outcome is unknown. Do not resend.")
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _settle(self, delivery_id: str, where: dict) -> None:
        """Once the owner settled a delivery nothing retries it: its DM file and the pinned intent (both
        carry the plaintext) go, as core's runner does (``_wait_live_dm``)."""
        from tools.bot_mode_dm import _live_intent_file, _unlink_dm_file

        dm_file = where.get("dm_file")
        if dm_file:
            _unlink_dm_file(_live_intent_file(dm_file))
            _unlink_dm_file(dm_file)
        with self._lock:
            self._sent.pop(delivery_id, None)


def register_bot_chat_tools(mcp: Any, *, reserved: tuple[str, ...] = ()) -> int:
    """Register :data:`BOT_CHAT_TOOL_NAMES` on *mcp*; a name in *reserved* is never replaced."""
    chat = BotChat()
    count = 0
    for name in BOT_CHAT_TOOL_NAMES:
        if name in reserved:
            continue
        mcp.tool()(getattr(chat, name))
        count += 1
    return count
