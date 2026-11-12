"""``--bot-chat``: Bot Mode messaging on the MCP wire. The roster is a real on-disk install read with core's
own predicate; delivery goes through core's ``tools.bot_mode_dm`` with the live-owner admission and the
receipt mailbox faked at their seams (no gateway is dialed)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import textwrap
from pathlib import Path

import pytest

from mcp_serve_plugin import bot_chat, bridge

_BRIDGE_TOOLS = set(bridge._TOOL_NAMES)
_BOT_TOOLS = set(bot_chat.BOT_CHAT_TOOL_NAMES)


def _profile_yaml(path: Path, *, title: str | None = None, description: str = "", bot: bool = True) -> None:
    lines = [f"description: {description}"] if description else []
    if bot:
        lines += ["ui_meta:", "  hermes-bots:", "    shape: cloud"] + ([f"    title: {title}"] if title else [])
    path.mkdir(parents=True, exist_ok=True)
    (path / "profile.yaml").write_text("\n".join(lines or ["display_name: plain"]) + "\n", encoding="utf-8")


@pytest.fixture
def install(monkeypatch, tmp_path):
    """HERMES_HOME (the conftest's) as an install root: a Bot Mode default profile, two Bot Mode
    teammates and one ordinary profile. DM files land under the test's temp dir, never the real one."""
    root = Path(os.environ["HERMES_HOME"])
    _profile_yaml(root, title="Maia", description="primary")
    _profile_yaml(root / "profiles" / "taiyi", title="Taiyi", description="architecture and verification")
    _profile_yaml(root / "profiles" / "scribe", description="writes things")
    _profile_yaml(root / "profiles" / "plain", description="not a bot", bot=False)
    dm_tmp = tmp_path / "tmp"
    dm_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(dm_tmp))
    return root


class _Owner:
    """The two seams below the plugin: ``_admit_live_dm`` (the profile authority) and the receipt file
    ``await_delivery`` polls."""

    def __init__(self, monkeypatch):
        from tools import bot_live_delivery, bot_mode_dm

        self.admitted: list[dict] = []
        self.receipts: dict[str, dict] = {}
        self.reply: str | None = "收到"
        self.admit_error: Exception | None = None

        def admit(profile_home, dm_file, author=None):
            if self.admit_error is not None:
                raise self.admit_error
            delivery_id = bot_mode_dm._dm_delivery_id(dm_file)
            home = str(Path(profile_home).resolve())
            self.admitted.append(dict(home=home, message=Path(dm_file).read_text(encoding="utf-8"),
                                      author=author, dm_file=dm_file, delivery_id=delivery_id))
            # Core pins the intent beside the DM file before admission.
            Path(bot_mode_dm._live_intent_file(dm_file)).write_text("{}", encoding="utf-8")
            record = dict(status="queued", delivery_id=delivery_id, profile_home=home)
            self.receipts[delivery_id] = dict(record, home=home)
            if self.reply is not None:
                self.receipts[delivery_id].update(status="settled", reply=self.reply)
            return record

        def poll(profile_home, delivery_id):
            rec = self.receipts.get(delivery_id)
            if rec is None or rec["home"] != str(Path(profile_home).resolve()):
                return None
            return {k: v for k, v in rec.items() if k != "home"}

        monkeypatch.setattr(bot_mode_dm, "_admit_live_dm", admit)
        monkeypatch.setattr(bot_live_delivery, "_poll_receipt", poll)


@pytest.fixture
def owner(monkeypatch):
    return _Owner(monkeypatch)


@pytest.fixture
def server(require_mcp_2_sdk):
    return bridge.create_mcp_server(event_bridge=bridge.EventBridge(), include_bot_chat=True)


def _call(server, name, args=None) -> dict:
    result = asyncio.run(server.call_tool(name, args or {}))
    text = "".join(getattr(block, "text", "") for block in (getattr(result, "content", None) or []))
    return json.loads(text)


def _listed(server) -> dict:
    return {t.name: t for t in asyncio.run(server.list_tools())}


# --- registration ------------------------------------------------------------------------------------

def test_bot_chat_is_off_by_default(require_mcp_2_sdk):
    assert set(_listed(bridge.create_mcp_server(event_bridge=bridge.EventBridge()))) == _BRIDGE_TOOLS


def test_bot_chat_flag_adds_the_three_tools(server):
    listed = _listed(server)
    assert set(listed) == _BRIDGE_TOOLS | _BOT_TOOLS
    send = listed["bot_message_send"].input_schema
    assert set(send["required"]) == {"profile", "message"}
    assert {"sender_name", "wait_seconds"} <= set(send["properties"])
    assert listed["bot_reply_get"].input_schema["required"] == ["delivery_id"]


# --- bots_list ---------------------------------------------------------------------------------------

def test_bots_list_shows_only_bot_mode_profiles(server, install):
    result = _call(server, "bots_list")
    bots = {b["profile"]: b for b in result["bots"]}
    assert set(bots) == {"default", "scribe", "taiyi"} and result["count"] == 3
    assert bots["default"]["handle"] == "@hermes" and bots["default"]["name"] == "Maia"
    assert bots["taiyi"] == {"profile": "taiyi", "handle": "@taiyi", "name": "Taiyi",
                             "role": "Taiyi — architecture and verification"}
    assert bots["scribe"]["name"] == "scribe" and bots["scribe"]["role"] == "writes things"


def test_bots_list_reads_the_roster_on_every_call(server, install):
    assert "newbie" not in {b["profile"] for b in _call(server, "bots_list")["bots"]}
    _profile_yaml(install / "profiles" / "newbie", description="just joined")
    _profile_yaml(install / "profiles" / "scribe", description="left bot mode", bot=False)
    profiles = {b["profile"] for b in _call(server, "bots_list")["bots"]}
    assert "newbie" in profiles and "scribe" not in profiles


# --- bot_message_send --------------------------------------------------------------------------------

def test_send_signs_and_returns_the_reply(server, install, owner):
    result = _call(server, "bot_message_send", {"profile": "@taiyi", "message": "只回复'收到'",
                                                "sender_name": "Alma"})
    (sent,) = owner.admitted
    assert sent["home"] == str((install / "profiles" / "taiyi").resolve())
    assert sent["message"] == "Message from 🤖 Alma (@alma): 只回复'收到'"
    assert sent["author"] == {"id": "bot:alma", "name": "alma", "is_bot": True}
    assert result == {"status": "settled", "delivery_id": sent["delivery_id"], "to": "@taiyi", "reply": "收到"}
    # A settled delivery leaves neither its DM file nor the pinned intent (both carry the plaintext).
    assert not os.path.exists(sent["dm_file"]) and not os.path.exists(sent["dm_file"] + ".live.json")


def test_send_defaults_the_sender(server, install, owner):
    _call(server, "bot_message_send", {"profile": "taiyi", "message": "hi"})
    assert owner.admitted[0]["message"] == "Message from 🤖 MCP client (@mcp-client): hi"


@pytest.mark.parametrize("sender, handle", [("Taiyi", "mcp-taiyi"), ("hermes", "mcp-hermes"),
                                            ("Maia", "mcp-maia"), ("!!!", "mcp-client")])
def test_sender_handle_never_impersonates_a_teammate(server, install, owner, sender, handle):
    _call(server, "bot_message_send", {"profile": "taiyi", "message": "hi", "sender_name": sender})
    assert owner.admitted[0]["author"]["id"] == f"bot:{handle}"
    assert f"(@{handle}): hi" in owner.admitted[0]["message"]


@pytest.mark.parametrize("args, needle", [
    ({"profile": "plain", "message": "hi"}, "not in Bot Mode"),
    ({"profile": "nobody", "message": "hi"}, "No profile named"),
    ({"profile": "", "message": "hi"}, "profile is required"),
    ({"profile": "taiyi", "message": "   "}, "message is required"),
    ({"profile": "taiyi", "message": "x" * 16001}, "message too long"),
])
def test_send_refusals_never_reach_the_owner(server, install, owner, args, needle):
    result = _call(server, "bot_message_send", args)
    assert needle in result["error"] and owner.admitted == []


def test_refusal_lists_the_bots(server, install, owner):
    assert set(_call(server, "bot_message_send", {"profile": "plain", "message": "hi"})["bots"]) == {
        "@hermes", "@scribe", "@taiyi"}


def test_unavailable_target_is_a_definite_no_send(server, install, owner):
    from tools.bot_mode_dm import LiveTargetUnavailableError

    owner.admit_error = LiveTargetUnavailableError("canonical Bot Chat target is unavailable")
    result = _call(server, "bot_message_send", {"profile": "taiyi", "message": "hi"})
    assert result["reason"] == "runtime_unavailable" and "NOT sent" in result["error"]
    assert result["to"] == "@taiyi"
    assert list(Path(tempfile.gettempdir()).glob("hermes-dm-*/*")) == []  # payload removed


def test_unconfirmed_admission_keeps_the_evidence(server, install, owner):
    owner.admit_error = RuntimeError("socket closed mid-RPC")
    result = _call(server, "bot_message_send", {"profile": "taiyi", "message": "hi"})
    assert result["status"] == "ambiguous" and "Do not resend" in result["error"]
    assert os.path.exists(result["evidence_file"])


# --- pending replies and bot_reply_get ----------------------------------------------------------------

def test_slow_reply_returns_the_delivery_id_then_bot_reply_get_fetches_it(server, install, owner):
    owner.reply = None  # the bot is still thinking
    pending = _call(server, "bot_message_send", {"profile": "taiyi", "message": "hi", "wait_seconds": 0})
    assert pending["status"] == "queued" and pending["pending"] is True
    delivery_id = pending["delivery_id"]
    assert _call(server, "bot_reply_get", {"delivery_id": delivery_id})["pending"] is True

    owner.receipts[delivery_id].update(status="settled", reply="收到")
    result = _call(server, "bot_reply_get", {"delivery_id": delivery_id})
    assert result == {"status": "settled", "delivery_id": delivery_id, "to": "@taiyi", "reply": "收到"}
    assert not os.path.exists(owner.admitted[0]["dm_file"])


def test_bot_reply_get_finds_a_delivery_another_server_sent(install, owner, require_mcp_2_sdk):
    first = bridge.create_mcp_server(event_bridge=bridge.EventBridge(), include_bot_chat=True)
    owner.reply = None
    delivery_id = _call(first, "bot_message_send", {"profile": "taiyi", "message": "hi",
                                                    "wait_seconds": 0})["delivery_id"]
    owner.receipts[delivery_id].update(status="failed", error="model refused", reason="provider_error")
    second = bridge.create_mcp_server(event_bridge=bridge.EventBridge(), include_bot_chat=True)
    result = _call(second, "bot_reply_get", {"delivery_id": delivery_id})
    assert (result["status"], result["error"], result["to"]) == ("failed", "model refused", "@taiyi")


@pytest.mark.parametrize("delivery_id, needle", [("nope", "Invalid delivery_id"), ("a" * 64, "Unknown delivery_id")])
def test_bot_reply_get_rejects_unknown_ids(server, install, owner, delivery_id, needle):
    assert needle in _call(server, "bot_reply_get", {"delivery_id": delivery_id})["error"]


def test_wait_is_clamped():
    assert bot_chat._coerce_wait(10_000, 60) == bot_chat.MAX_WAIT_SECONDS
    assert bot_chat._coerce_wait(-5, 60) == 0 and bot_chat._coerce_wait("x", 60) == 60
