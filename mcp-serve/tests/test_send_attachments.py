import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mcp_serve_plugin import bridge


def attachment(filename="image.png", data=b"image bytes"):
    return {"filename": filename, "data": base64.b64encode(data).decode("ascii")}


@pytest.fixture
def sender(monkeypatch):
    """Mock both sender locations so this suite works across core versions."""
    send = Mock(return_value='{"success": true}')
    module = SimpleNamespace(send_message_tool=send)
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", module)
    monkeypatch.setitem(sys.modules, "gateway.outbound.send", module)
    return send


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "profile with spaces"
    path.mkdir()
    monkeypatch.setattr(bridge, "_hermes_home", lambda: path)
    return path


def call(message, attachments=None):
    return json.loads(bridge._ToolHandlers(bridge.EventBridge()).messages_send(
        "discord:test", message, attachments
    ))


def test_text_only(sender, home):
    assert call("hello")["success"]
    sender.assert_called_once_with({"action": "send", "target": "discord:test", "message": "hello"})
    assert not list(home.iterdir())


def test_two_attachments_with_same_filename(sender, home):
    from gateway.platforms.base import BasePlatformAdapter

    paths = []

    def send(args):
        media, text = BasePlatformAdapter.extract_media(args["message"])
        assert text == "hello"
        paths.extend(Path(path) for path, _ in media)
        assert len(paths) == 2
        assert paths[0] != paths[1]
        assert [p.name for p in paths] == ["image.png", "image.png"]
        assert [p.read_bytes() for p in paths] == [b"first", b"second"]
        if sys.platform != "win32":
            assert all(p.stat().st_mode & 0o777 == 0o600 for p in paths)
        return '{"success": true}'

    sender.side_effect = send
    assert call("hello", [attachment(data=b"first"), attachment(data=b"second")])["success"]
    assert sender.call_count == 1
    assert not any(p.exists() for p in paths)
    assert not list((home / "cache").iterdir())


def test_attachment_only(sender, home):
    assert call("", [attachment()])["success"]
    assert sender.call_args.args[0]["message"].startswith('MEDIA:"')
    assert not list((home / "cache").iterdir())


@pytest.mark.parametrize("invalid", [
    [attachment("../image.png")],
    [attachment("/tmp/image.png")],
    [attachment("C:\\image.png")],
    [attachment("CON.png")],
    [attachment("image.png.")],
    [attachment("image name.png")],
    [{"filename": "a.png", "data": "!invalid"}],
    [{"filename": "a.png", "data": ""}],
    [{"filename": "a.png", "data": "data:image/png;base64,YQ=="}],
    [{**attachment(), "mime_type": "image\n/png"}],
    [{**attachment(), "path": "/tmp/file"}],
    [attachment()] * 5,
    [attachment(), {"filename": "bad.png", "data": "!"}],
])
def test_invalid_upload_never_sends_or_writes(invalid, sender, home):
    assert "error" in call("hello", invalid)
    sender.assert_not_called()
    assert not list(home.iterdir())


def test_decoded_size_limit(sender, home, monkeypatch):
    monkeypatch.setattr(bridge, "_MAX_ATTACHMENT_BYTES", 5)
    assert len(bridge._decode_send_attachments([attachment(data=b"12345")])[0][1]) == 5
    assert "error" in call("hello", [attachment(data=b"123456")])
    assert "error" in call("hello", [attachment(data=b"123"), attachment(data=b"456")])
    sender.assert_not_called()
    assert not list(home.iterdir())


@pytest.mark.parametrize("raises", [True, False])
def test_cleanup_after_sender_failure(raises, sender, home):
    if raises:
        sender.side_effect = RuntimeError("send failed")
    else:
        sender.return_value = '{"success": false, "error": "send failed"}'
    assert "error" in call("hello", [attachment()])
    assert not list((home / "cache").iterdir())


def test_mcp_attachment_schema_and_dispatch(require_mcp_2_sdk, sender, home):
    async def exercise():
        server = bridge.create_mcp_server()
        tools = await server.list_tools()
        tool = next(t for t in tools if t.name == "messages_send")
        schema = tool.input_schema
        assert "attachments" in schema["properties"]
        assert "attachments" not in schema["required"]
        fields = schema["$defs"]["SendAttachment"]
        assert set(fields["required"]) == {"filename", "data"}
        assert "mime_type" in fields["properties"]
        result = await server.call_tool("messages_send", {
            "target": "discord:test", "message": "hello", "attachments": [attachment()]
        })
        assert json.loads(result.content[0].text)["success"]

    asyncio.run(exercise())
    sender.assert_called_once()
    assert not list((home / "cache").iterdir())
