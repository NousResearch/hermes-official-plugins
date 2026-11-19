"""Playing length of the audio ElevenLabs returns, read from the bytes with the standard library.

ElevenLabs can return more audio than was asked for (a 1 s loop came back 1.5 s long, 2 s loops 2.25 s),
so the tool reports the length of the file it saved, not the request. Handles every ``output_format``
the plugin offers: MP3 (frame scan minus the LAME encoder delay and padding), WAV (RIFF header) and Ogg Opus (last
granule minus the start offset and pre-skip). Returns ``None`` for anything it cannot read or a length the
byte count cannot hold; it never raises and never runs ffprobe.
"""

from __future__ import annotations

import struct

# MPEG audio frame header tables: bitrates in kbps by [version is MPEG1][layer][index].
_BITRATES = {
    (True, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame(data: bytes, pos: int) -> tuple[int, int, int] | None:
    """``(frame bytes, samples, sample rate)`` of the MPEG audio frame at ``pos``, or None."""
    if pos + 4 > len(data) or data[pos] != 0xFF or data[pos + 1] & 0xE0 != 0xE0:
        return None
    version, layer_bits = (data[pos + 1] >> 3) & 3, (data[pos + 1] >> 1) & 3
    bitrate_index, rate_index = data[pos + 2] >> 4, (data[pos + 2] >> 2) & 3
    if version == 1 or layer_bits == 0 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    layer = 4 - layer_bits
    mpeg1 = version == 3
    bitrate = _BITRATES[(mpeg1, layer)][bitrate_index] * 1000
    rate = _SAMPLE_RATES[version][rate_index]
    padding = (data[pos + 2] >> 1) & 1
    if layer == 1:
        return (12 * bitrate // rate + padding) * 4, 384, rate
    samples = 1152 if layer == 2 or mpeg1 else 576
    return samples // 8 * bitrate // rate + padding, samples, rate


def _info_frame_trim(frame: bytes) -> int | None:
    """For a Xing/Info header frame (it carries no audio): the encoder delay + padding its LAME
    extension records, which decoders drop (0 without one). None when ``frame`` is an audio frame."""
    for tag in (b"Xing", b"Info"):
        at = frame.find(tag)
        if at >= 0:
            break
    else:
        return 0 if b"VBRI" in frame else None
    flags = struct.unpack_from(">I", frame, at + 4)[0]
    ext = at + 8 + 4 * bool(flags & 1) + 4 * bool(flags & 2) + 100 * bool(flags & 4) + 4 * bool(flags & 8)
    if ext + 24 > len(frame):
        return 0
    b = frame[ext + 21:ext + 24]
    return (b[0] << 4 | b[1] >> 4) + ((b[1] & 0x0F) << 8 | b[2])


def _synced(data: bytes, pos: int, run: int = 3) -> bool:
    """A frame header at ``pos`` followed by ``run - 1`` more of the same sample rate (or the end of
    the data): random bytes pass a single header check about once per few hundred bytes."""
    rate = None
    for _ in range(run):
        if rate is not None and (pos == len(data) or data[pos:pos + 3] == b"TAG"):
            return True  # a short stream that ends exactly after its last frame (or at an ID3v1 tag)
        frame = _mp3_frame(data, pos)
        if frame is None or frame[0] <= 4 or (rate is not None and frame[2] != rate):
            return False
        rate = frame[2]
        pos += frame[0]
    return pos <= len(data)


# How far past the ID3 tag the first MPEG frame may start. Encoders put it right after
# the tag; a junk response would otherwise cost seconds of pure-Python scanning per 50 MB.
_JUNK_SCAN_BYTES = 64 * 1024


def mp3_seconds(data: bytes) -> float | None:
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
        pos = 10 + size + (10 if data[5] & 0x10 else 0)
    scan_end = pos + _JUNK_SCAN_BYTES
    samples, rate, first, trim = 0, 0, True, 0
    while pos < len(data):
        frame = _mp3_frame(data, pos) if not first or _synced(data, pos) else None
        if frame is None:
            if first:  # skip junk before the first frame, to a run of 3 consistent frames
                nxt = data.find(b"\xff", pos + 1, scan_end)
                if nxt < 0:  # no audio start in the first 64 KiB: not an MP3 (bounds the scan's CPU)
                    break
                pos = nxt
                continue
            break
        size, count, rate = frame
        if size <= 4:
            break
        info_trim = _info_frame_trim(data[pos:pos + size]) if first else None
        if info_trim is None:
            samples += count
        else:
            trim = info_trim
        first = False
        pos += size
    if trim < samples:
        samples -= trim
    return samples / rate if samples and rate else None


def wav_seconds(data: bytes) -> float | None:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, byte_rate = 12, 0
    while pos + 8 <= len(data):
        tag, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if tag == b"fmt " and size >= 16 and body + 16 <= len(data):
            byte_rate = struct.unpack_from("<I", data, body + 8)[0]
        elif tag == b"data":
            # A streamed WAV can declare 0 or 0xFFFFFFFF; the bytes that follow are the audio.
            available = len(data) - body
            size = available if size in (0, 0xFFFFFFFF) or size > available else size
            return size / byte_rate if byte_rate else None
        pos = body + size + (size & 1)
    return None


def _ogg_pages(data: bytes):
    """``(granule, packets completed on this page)`` per Ogg page, in order."""
    pos = data.find(b"OggS")
    while 0 <= pos and pos + 27 <= len(data):
        granule, count = struct.unpack_from("<q", data, pos + 6)[0], data[pos + 26]
        lacing = data[pos + 27:pos + 27 + count]
        body = pos + 27 + count
        packets, start, size = [], body, 0
        for value in lacing:
            size += value
            if value < 255:
                packets.append(data[start:start + size])
                start, size = start + size, 0
        yield granule, packets
        pos = data.find(b"OggS", body + sum(lacing))


# Opus frame length (48 kHz samples) by TOC configuration: SILK 0-11, hybrid 12-15, CELT 16-31.
_OPUS_FRAME = [480, 960, 1920, 2880] * 3 + [480, 960] * 2 + [120, 240, 480, 960] * 4


def _opus_packet_samples(packet: bytes) -> int:
    if not packet:
        return 0
    frame, code = _OPUS_FRAME[packet[0] >> 3], packet[0] & 3
    if code == 0:
        return frame
    if code in (1, 2):
        return 2 * frame
    return frame * (packet[1] & 0x3F) if len(packet) > 1 else 0


def opus_seconds(data: bytes) -> float | None:
    """Playable length = last granule - start offset - pre-skip (RFC 7845). The start offset is the
    first audio page's granule minus the samples its packets carry; ElevenLabs' files have one (its
    first page claims 312 samples more than it holds), which ffmpeg and browsers honour."""
    head = data.find(b"OpusHead")
    if head < 0 or head + 12 > len(data):
        return None
    pre_skip = struct.unpack_from("<H", data, head + 10)[0]
    first = last = None
    carried = 0  # samples the audio packets really hold: a granule claiming more is corrupt
    for index, (granule, packets) in enumerate(_ogg_pages(data)):
        if index < 2:  # identification + comment headers
            continue
        carried += sum(_opus_packet_samples(p) for p in packets)
        if granule < 0:  # -1 = no packet ends on this page
            continue
        if first is None:
            first = granule - sum(_opus_packet_samples(p) for p in packets)
        last = granule
    if first is None or last is None:
        return None
    samples = last - max(first, 0) - pre_skip
    if samples <= 0 or samples > carried:
        return None
    return samples / 48000  # Opus granules always count 48 kHz samples


_READERS = {"mp3": mp3_seconds, "wav": wav_seconds, "opus": opus_seconds}
# MP3 frames start at 8 kbit/s and PCM is far above, so an MP3 or WAV length the byte count cannot hold at
# 4 kbit/s comes from a corrupt header. Opus is checked against the samples its packets carry instead
# (libopus codes digital silence at under 2 kbit/s).
_MIN_BITS_PER_SECOND = {"mp3": 4000, "wav": 4000}


def audio_seconds(data: bytes, extension: str) -> float | None:
    """Length in seconds (3 decimals) of ``data`` saved as ``extension``; None when unreadable."""
    reader = _READERS.get(extension)
    try:
        seconds = reader(data) if reader else None
    except (struct.error, IndexError, KeyError, ZeroDivisionError):
        seconds = None
    floor = _MIN_BITS_PER_SECOND.get(extension)
    if not seconds or seconds <= 0 or (floor and seconds > len(data) * 8 / floor):
        return None
    return round(seconds, 3)
