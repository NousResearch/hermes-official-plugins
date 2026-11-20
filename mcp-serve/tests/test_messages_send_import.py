"""#454: messages_send follows the relocated outbound sender."""
from __future__ import annotations

import json
import sys
import types

from mcp_serve_plugin import bridge


def test_prefers_gateway_outbound(monkeypatch):
    calls = {}

    def send_message_tool(args):
        calls["args"] = args
        return "ok"

    new = types.ModuleType("gateway.outbound.send")
    new.send_message_tool = send_message_tool
    old = types.ModuleType("tools.send_message_tool")
    old.send_message_tool = lambda args: (_ for _ in ()).throw(AssertionError("old path"))
    monkeypatch.setitem(sys.modules, "gateway.outbound.send", new)
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", old)
    assert bridge._load_send_message_tool() is send_message_tool


def test_falls_back_to_the_old_module(monkeypatch):
    def send_message_tool(args):
        return args

    old = types.ModuleType("tools.send_message_tool")
    old.send_message_tool = send_message_tool
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", old)
    monkeypatch.delitem(sys.modules, "gateway.outbound.send", raising=False)
    # A real gateway package may already be imported; hide it for this call.
    real = sys.modules.get("gateway.outbound.send")
    sys.modules["gateway.outbound.send"] = None  # type: ignore[assignment]
    try:
        assert bridge._load_send_message_tool() is send_message_tool
    finally:
        if real is None:
            sys.modules.pop("gateway.outbound.send", None)
        else:
            sys.modules["gateway.outbound.send"] = real


def test_handler_dispatches_and_names_a_missing_sender(monkeypatch):
    seen = {}

    def send_message_tool(args):
        seen.update(args)
        return "sent"

    monkeypatch.setattr(bridge, "_load_send_message_tool", lambda: send_message_tool)
    handler = bridge._ToolHandlers(bridge.EventBridge())
    assert handler.messages_send("telegram:1", "hi") == "sent"
    assert seen == {"action": "send", "target": "telegram:1", "message": "hi"}

    def _missing():
        raise ImportError("No module named 'gateway.outbound.send'")

    monkeypatch.setattr(bridge, "_load_send_message_tool", _missing)
    err = json.loads(handler.messages_send("telegram:1", "hi"))
    assert "not available" in err["error"]
    assert "gateway.outbound.send" in err["error"]
