"""Test bootstrap: import the plugin directory as the package ``elevenlabs_sfx_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports in ``__init__.py`` resolve; tests mirror that under a stable alias. Hermes core
(``hermes_cli``, ``tools``, ``agent``) must be importable: run with a hermes-agent checkout on
``PYTHONPATH``.
"""

from __future__ import annotations

import importlib.util
import io
import struct
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

PACKAGE = "elevenlabs_sfx_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the plugin dir puts it on sys.path, where its modules could shadow core.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]


def _load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_load_plugin_package()


@pytest.fixture
def plugin_root() -> Path:
    return _ROOT


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    """Every test gets its own HOME and profile home; nothing touches the real ones."""
    home = tmp_path / "os-home" / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home.parent))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    return home


class _RepoRootIsNotATestPackage:
    """The plugin dir *is* a package (its ``__init__.py`` has relative imports), so pytest would
    collect it as a test ``Package`` and import that ``__init__.py`` standalone. Collect it as a plain
    directory instead (registered globally: the root's collector comes from its parent's hook proxy)."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("elevenlabs-sfx-root-as-dir"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "elevenlabs-sfx-root-as-dir")


# Audio built byte by byte in the three containers the plugin offers (shared by the length tests).
def wav_bytes(seconds: float, rate: int = 48000, channels: int = 2, streamed: bool = False) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * channels * round(seconds * rate))
    data = buf.getvalue()
    if streamed:  # what a streaming encoder writes before it knows the size
        at = data.find(b"data") + 4
        data = data[:at] + b"\xff\xff\xff\xff" + data[at + 4:]
    return data


def mp3_bytes(frames: int, delay: int = 576, padding: int = 1575) -> bytes:
    """ID3v2 tag, a Xing ``Info`` frame with a LAME extension (encoder delay/padding), then ``frames``
    MPEG-1 Layer III frames at 128 kbps / 44.1 kHz (417 bytes each, 1152 samples)."""
    header = b"\xff\xfb\x90\x00"
    size = 144 * 128000 // 44100
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10
    lame = b"Lavf\x00\x00\x00\x00\x00" + b"\x00" * 12 + bytes([delay >> 4, (delay & 0xF) << 4 | padding >> 8, padding & 0xFF])
    info = header + b"\x00" * 32 + b"Info" + struct.pack(">II", 0x0F, frames) + struct.pack(">I", 0) + b"\x00" * 104 + lame
    info += b"\x00" * (size - len(info))
    return tag + info + (header + b"\x55" * (size - 4)) * frames


def ogg_page(granule: int, packets: list[bytes], flags: int = 0) -> bytes:
    lacing = bytes(len(p) for p in packets)  # every test packet is under 255 bytes: one segment each
    return (b"OggS\x00" + bytes([flags]) + struct.pack("<qIII", granule, 1, 0, 0) + bytes([len(packets)])
            + lacing + b"".join(packets))


def opus_bytes(samples: int, pre_skip: int = 312, offset: int = 0) -> bytes:
    """Ogg Opus: header pages, then 20 ms CELT packets (TOC 0xfc, 960 samples each) on two pages whose
    granules count ``offset`` + pre-skip + audio, with end trimming on the last page."""
    head = b"OpusHead\x01\x02" + struct.pack("<HIhB", pre_skip, 48000, 0, 0)
    count = -(-(samples + pre_skip) // 960)
    half = count // 2
    packet = b"\xfc" + b"\x00" * 39
    return (ogg_page(0, [head], 2) + ogg_page(0, [b"OpusTags" + b"\x00" * 8])
            + ogg_page(offset + half * 960, [packet] * half)
            + ogg_page(offset + samples + pre_skip, [packet] * (count - half), 4))


@pytest.fixture
def audio():
    return SimpleNamespace(wav=wav_bytes, mp3=mp3_bytes, opus=opus_bytes, page=ogg_page)
