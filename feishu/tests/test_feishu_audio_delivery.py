"""Feishu audio delivery: plain audio MEDIA is a named file, only voice intent is a voice note."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.feishu.adapter import FeishuAdapter


def _recording_adapter():
    adapter = FeishuAdapter(PlatformConfig())
    sent = {"uploads": [], "messages": []}

    class _FileAPI:
        def create(self, request):
            body = request.request_body
            sent["uploads"].append((body.file_type, body.file_name))
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(file_key="file_1"))

    class _MessageAPI:
        def create(self, request):
            sent["messages"].append(request.request_body.msg_type)
            return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_1"))

    adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(file=_FileAPI(), message=_MessageAPI())))
    return adapter, sent


async def _direct(func, *args, **kwargs):
    return func(*args, **kwargs)


@pytest.mark.parametrize("name", ["door_knock.mp3", "theme.wav"])
def test_plain_audio_media_is_a_named_file_not_a_transcoded_voice_note(tmp_path, name):
    adapter, sent = _recording_adapter()
    path = tmp_path / name
    path.write_bytes(b"\x00" * 64)

    def transcode_must_not_run(*_a, **_k):
        raise AssertionError("plain audio must not be transcoded into a voice note")

    with patch("plugins.platforms.feishu.adapter.asyncio.to_thread", side_effect=_direct), \
         patch("gateway.platforms.base.transcode_to_ogg_opus", transcode_must_not_run):
        result = asyncio.run(adapter.send_voice("oc_chat", str(path), is_voice=False))

    assert result.success
    assert sent["uploads"] == [("stream", name)]
    assert sent["messages"] == ["file"]
