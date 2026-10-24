#!/usr/bin/env python3
"""Local post-processing for generated sound effects (stdlib + ffmpeg/ffprobe).

Subcommands: probe, process (trim silence, loudness-normalise, fades, convert), loopfix (crossfade
a loop's end into its start), loopcheck (seam click check, seam dip measurement), pack (named folder + manifest.json +
zip). Every run prints exactly ONE JSON line on
stdout, ``{"ok": true, ...}`` or ``{"ok": false, "code": ..., "error": ...}``, and exits 0 or 1. It
never prints a traceback: ffmpeg missing, a bad input file or bad arguments all come back as that
line, so an agent can read the outcome instead of guessing.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path

CODECS = {".ogg": ["-c:a", "libvorbis", "-q:a", "5"], ".wav": ["-c:a", "pcm_s16le"],
          ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"], ".opus": ["-c:a", "libopus", "-b:a", "128k"]}
FORMAT_EXT = {"ogg": ".ogg", "wav": ".wav", "mp3": ".mp3"}
_TIMEOUT = 300
_MAX_PCM_BYTES = 64 * 1024 * 1024
_LOOP_RATE = 48000
_SILENT_PEAK = 10  # int16 peak at or below about -70 dBFS: (near-)digital silence
AUDIO_EXT = {".wav", ".ogg", ".mp3", ".opus", ".flac", ".m4a", ".aac", ".aiff", ".aif", ".webm", ".mka"}
_LOUDNESS_CEILING_DBTP = -1.5
_LOOP_MEASURE_SECONDS = 3.0  # a clip too short for one 400 ms EBU R128 block is measured repeated to this length
_LOOP_EDGE_SECONDS = 10  # loopcheck decodes this much of a long file's head and tail, never the whole file


class Failure(Exception):
    def __init__(self, code: str, error: str):
        super().__init__(error)
        self.code, self.error = code, error


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would print usage to stderr and exit 2
        raise Failure("bad_args", message)


def emit(payload: dict) -> int:
    # ASCII-only: a Windows pipe uses the ANSI code page, and a non-ASCII path must not turn the
    # one JSON line into a UnicodeEncodeError traceback.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0 if payload.get("ok") else 1


def _tool(name: str, override: str | None) -> str:
    found = shutil.which(override or name)
    if not found:
        raise Failure("ffmpeg_missing", f"{override or name} not found on PATH; install ffmpeg "
                                        "(it includes ffprobe) to process sounds locally")
    return found


def _input(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise Failure("bad_input", f"input file not found: {path}")
    return p.resolve()


def _run(cmd: list[str], *, binary: bool = False) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise Failure("ffmpeg_failed", f"{Path(cmd[0]).name} did not finish within {_TIMEOUT} s") from None
    except OSError as exc:
        raise Failure("ffmpeg_missing", f"could not run {cmd[0]}: {exc.strerror or exc}") from None
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        code = "bad_input" if any(s in " ".join(tail) for s in ("Invalid data", "could not find codec",
                                                                "does not contain any stream")) else "ffmpeg_failed"
        raise Failure(code, f"{Path(cmd[0]).name} failed: {' | '.join(tail) or 'exit ' + str(proc.returncode)}")
    return proc


def probe(path: Path, ffprobe: str) -> dict:
    proc = _run([ffprobe, "-v", "error", "-show_entries",
                 "format=duration,format_name:stream=codec_type,codec_name,sample_rate,channels",
                 "-of", "json", str(path)])
    try:
        data = json.loads(proc.stdout or b"{}")
    except ValueError:
        raise Failure("bad_input", f"ffprobe returned unreadable output for {path.name}") from None
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not audio:
        raise Failure("bad_input", f"{path.name} has no audio stream")
    fmt = data.get("format", {})
    duration = float(fmt["duration"]) if fmt.get("duration") else None
    if audio.get("codec_name") == "opus":
        duration = _opus_seconds(path, ffprobe) or duration
    return {"file": str(path), "duration_seconds": round(duration, 3) if duration else None,
            "codec": audio.get("codec_name"), "sample_rate": int(audio.get("sample_rate") or 0) or None,
            "channels": audio.get("channels"), "container": fmt.get("format_name"),
            "bytes": path.stat().st_size}


def _opus_seconds(path: Path, ffprobe: str) -> float:
    # Ogg Opus container durations include the encoder pre-skip (and ffprobe's start_time is not reliable
    # across muxers); the decoded frames are the length a player actually plays. Opus always decodes at 48 kHz.
    proc = _run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "frame=nb_samples",
                 "-of", "csv=p=0", str(path)])
    counts = proc.stdout.decode("ascii", "replace").split()
    return sum(int(c.strip(",")) for c in counts if c.strip(",").isdigit()) / 48000


def measure_loudness(path: Path, ffmpeg: str, *, pre: list[str] | None = None, loops: int = 0) -> dict:
    """Measured integrated loudness (EBU R128) and true peak of a file (after the PRE filters, played
    LOOPS extra times). ``integrated_lufs`` is None when nothing could be measured: ebur128 prints its
    -70 LUFS gate for a clip with no 400 ms block, and that is not a loudness."""
    loop = ["-stream_loop", str(loops)] if loops else []
    proc = _run([ffmpeg, "-nostdin", "-hide_banner", "-nostats", *loop, "-i", str(path), "-vn",
                 "-af", ",".join([*(pre or []), "ebur128=peak=true"]), "-f", "null", "-"])
    text = proc.stderr.decode("utf-8", "replace")

    def last(pattern: str) -> float | None:
        found = re.findall(pattern, text)
        return float(found[-1]) if found and found[-1] != "-inf" else None

    integrated = last(r"\bI:\s+(-?[\d.]+|-inf) LUFS")
    return {"integrated_lufs": integrated if integrated is not None and integrated > -70 else None,
            "true_peak_dbtp": last(r"Peak:\s+(-?[\d.]+|-inf) dBFS")}


def _measure_looped(path: Path, ffmpeg: str, ffprobe: str) -> dict:
    """Loudness of a clip too short to measure, taken over copies of it played back to back."""
    length = probe(path, ffprobe)["duration_seconds"] or 0.0
    if length < 0.001:  # nothing left (an all-silent clip after --trim): nothing to loop
        return {"integrated_lufs": None, "true_peak_dbtp": None}
    loops = min(30000, math.ceil(_LOOP_MEASURE_SECONDS / length))
    return measure_loudness(path, ffmpeg, loops=loops)


def _trim_filters(trim: bool) -> list[str]:
    edge = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.01"
    return [edge, "areverse", edge, "areverse"] if trim else []


def _fade_filters(fade_ms: float) -> list[str]:
    d = fade_ms / 1000
    return [f"afade=t=in:d={d:g}", "areverse", f"afade=t=in:d={d:g}", "areverse"] if fade_ms > 0 else []


def _filters(trim: bool, loudness: float | None, fade_ms: float) -> list[str]:
    chain = _trim_filters(trim)
    if loudness is not None:
        chain.append(f"loudnorm=I={loudness:g}:TP={_LOUDNESS_CEILING_DBTP:g}:LRA=11")
    return chain + _fade_filters(fade_ms)


def cmd_probe(args) -> dict:
    return {"ok": True, **probe(_input(args.input), _tool("ffprobe", args.ffprobe))}


def _cannot_write(path: Path, exc: OSError) -> Failure:
    return Failure("bad_args", f"cannot write {path}: {exc.strerror or exc}")


def _temp_beside(out: Path, suffix: str) -> str:
    # Write beside the target and rename, so a failed run never leaves a half-written file at OUT.
    try:
        fd, tmp = tempfile.mkstemp(prefix=".sfx_tool_", suffix=suffix, dir=out.parent)
    except OSError as exc:  # a read-only folder: name OUT, not the hidden temp file
        raise _cannot_write(out, exc) from None
    os.close(fd)
    return tmp


def _publish(tmp: str, out: Path) -> None:
    try:
        os.replace(tmp, out)
    except OSError as exc:
        raise _cannot_write(out, exc) from None


def _encode(ffmpeg: str, src: Path, out: Path, chain: list[str], sample_rate: int | None, check=None) -> None:
    """Encode SRC to OUT; ``check(tmp_path)`` may refuse the encoded result before it replaces OUT."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn"]
    if chain:
        cmd += ["-af", ",".join(chain)]
    if sample_rate:  # loudnorm resamples to 192 kHz internally; keep the source rate
        # libopus takes only 48/24/16/12/8 kHz.
        cmd += ["-ar", "48000" if out.suffix.lower() == ".opus" else str(sample_rate)]
    cmd += CODECS[out.suffix.lower()]
    tmp = _temp_beside(out, out.suffix)
    try:
        _run(cmd + [tmp])
        if check:
            check(Path(tmp))
        _publish(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _output(path: str, src: Path, force: bool) -> Path:
    out = Path(path).expanduser()
    if out.suffix.lower() not in CODECS:
        raise Failure("bad_args", f"output extension must be one of {', '.join(sorted(CODECS))}")
    if not out.parent.is_dir():
        raise Failure("bad_args", f"output folder does not exist: {out.parent}")
    out = out.resolve()
    if out.is_dir():
        raise Failure("bad_args", f"{out} is a folder; give a file name for the output")
    if out == src:
        raise Failure("bad_args", "output must differ from the input (the input is never overwritten)")
    if out.exists() and not force:
        raise Failure("bad_args", f"{out} exists; pass --force to replace it")
    return out


def cmd_process(args) -> dict:
    src = _input(args.input)
    ffmpeg, ffprobe = _tool("ffmpeg", args.ffmpeg), _tool("ffprobe", args.ffprobe)
    out = _output(args.out, src, args.force)
    if args.loudness is not None and not -70 <= args.loudness <= -5:
        raise Failure("bad_args", "--loudness must be between -70 and -5 LUFS")
    if not 0 <= args.fade_ms <= 5000:
        raise Failure("bad_args", "--fade-ms must be between 0 and 5000")
    before = probe(src, ffprobe)
    check = _fade_fits(ffprobe, args.fade_ms, args.trim) if args.fade_ms > 0 else None
    applied = {"trim_silence": args.trim, "loudness_target_lufs": args.loudness, "fade_ms": args.fade_ms}
    # loudnorm measures in 400 ms blocks: a shorter clip reads -70 LUFS and one pass would push it to the ceiling.
    if args.loudness is not None and measure_loudness(src, ffmpeg, pre=_trim_filters(args.trim))["integrated_lufs"] is None:
        after = _process_short(args, src, out, before, check, ffmpeg, ffprobe)
        applied["loudness_gain_db"] = after.pop("gain_db")
    else:
        chain = _filters(args.trim, args.loudness, args.fade_ms)
        _encode(ffmpeg, src, out, chain, before["sample_rate"] if args.loudness is not None else None, check)
        after = {**probe(out, ffprobe), **measure_loudness(out, ffmpeg)}
    note = _loudness_note(args.loudness, after)
    if note:
        applied["loudness_note"] = note
    return {"ok": True, "input": before, "output": after, "applied": applied}


def _process_short(args, src: Path, out: Path, before: dict, check, ffmpeg: str, ffprobe: str) -> dict:
    """Level a clip too short for loudnorm with one plain gain: measured on the (trimmed) clip played back
    to back, capped so the true peak stays under the ceiling, reported the same looped way."""
    trimmed = Path(_temp_beside(out, ".wav"))
    try:
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn"]
        _run(cmd + (["-af", ",".join(_trim_filters(args.trim))] if args.trim else []) + ["-c:a", "pcm_f32le", str(trimmed)])
        level = _measure_looped(trimmed, ffmpeg, ffprobe)
        gain = 0.0
        if level["integrated_lufs"] is not None:
            gain = args.loudness - level["integrated_lufs"]
            if level["true_peak_dbtp"] is not None:
                gain = min(gain, _LOUDNESS_CEILING_DBTP - level["true_peak_dbtp"])
        _encode(ffmpeg, trimmed, out, [f"volume={gain:.2f}dB", *_fade_filters(args.fade_ms)], before["sample_rate"], check)
    finally:
        trimmed.unlink(missing_ok=True)
    looped = _measure_looped(out, ffmpeg, ffprobe)
    return {**probe(out, ffprobe), "integrated_lufs": looped["integrated_lufs"],
            "true_peak_dbtp": measure_loudness(out, ffmpeg)["true_peak_dbtp"],
            "loudness_measured_on": f"the clip looped to {_LOOP_MEASURE_SECONDS:g} s (shorter than one 400 ms EBU R128 block)", "gain_db": round(gain, 2)}


def _fade_fits(ffprobe: str, fade_ms: float, trimmed: bool):
    def check(encoded: Path) -> None:
        # Measured on the encoded result, so a --trim that shortened the clip counts.
        length = probe(encoded, ffprobe)["duration_seconds"] or 0.0
        if 2 * fade_ms / 1000 >= length:
            where = " after trimming" if trimmed else ""
            raise Failure("bad_args", f"--fade-ms {fade_ms:g} fades in and out over {2 * fade_ms / 1000:g} s, but the "
                                      f"clip is {length:g} s long{where}; use a fade shorter than half the clip")
    return check


def _loudness_note(target: float | None, measured: dict) -> str | None:
    got, peak = measured.get("integrated_lufs"), measured.get("true_peak_dbtp")
    if target is not None and got is None:
        return f"aimed at {target:g} LUFS, but the result is (near-)silent: there is no loudness to measure or raise"
    if target is None or abs(got - target) <= 1.0:
        return None
    held = got < target and peak is not None and peak >= _LOUDNESS_CEILING_DBTP - 0.5
    why = (f"the {_LOUDNESS_CEILING_DBTP:g} dBTP true-peak ceiling held it down" if held
           else "loudnorm could not reach it on this material (too short, too peaky or too uneven)")
    return f"aimed at {target:g} LUFS, measured {got:g} LUFS: {why}"


def _pcm_frames(ffmpeg: str, src: Path, channels: int, rate: int, *, seek: list[str] | None = None) -> array.array:
    """Decode SRC (or the stretch SEEK picks, e.g. ``["-t", "10"]``) to interleaved int16 samples."""
    proc = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *(seek or []), "-i", str(src), "-vn",
                 "-ac", str(channels), "-ar", str(rate), "-f", "s16le", "-acodec", "pcm_s16le", "-"])
    if len(proc.stdout) > _MAX_PCM_BYTES:  # refused, never cut short: a cut file would be judged at the wrong seam
        raise Failure("bad_input", f"{src.name} is too long to process as one loop (over {_MAX_PCM_BYTES // 2**20} MiB of audio)")
    raw = proc.stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % (2 * channels)])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def cmd_loopfix(args) -> dict:
    """Crossfade the loop's last N ms into its first N ms (equal-power) and drop that tail: playback
    then runs from the blended start straight out of the original audio, with no gap. The gain follows the
    segments' correlation; an anti-correlated seam can still dip a few dB (``crossfade_level_db``)."""
    src = _input(args.input)
    ffmpeg, ffprobe = _tool("ffmpeg", args.ffmpeg), _tool("ffprobe", args.ffprobe)
    out = _output(args.out, src, args.force)
    if not 1 <= args.crossfade_ms <= 2000:
        raise Failure("bad_args", "--crossfade-ms must be between 1 and 2000")
    before = probe(src, ffprobe)
    channels, rate = int(before["channels"] or 2), int(before["sample_rate"] or 48000)
    if out.suffix.lower() == ".opus":
        rate = 48000
    samples = _pcm_frames(ffmpeg, src, channels, rate)
    frames = len(samples) // channels
    n = int(rate * args.crossfade_ms / 1000)
    if frames < 3 * n:
        raise Failure("bad_input", f"{src.name} is too short for a {args.crossfade_ms:g} ms crossfade")
    keep = frames - n
    head = samples[: n * channels]
    tail = samples[keep * channels: (keep + n) * channels]
    head_power, tail_power = sum(a * a for a in head), sum(b * b for b in tail)
    dot = sum(a * b for a, b in zip(head, tail))
    r = max(-1.0, min(1.0, dot / (math.sqrt(head_power * tail_power) or 1.0)))
    blended = array.array("h", samples[: keep * channels])
    clipped = _blend(blended, head, tail, channels, r)
    # Anti-correlated segments (r well below 0) partly cancel; the gain cap leaves a dip inside the blend.
    fade_db = _crossfade_dip_db(blended, head, tail, channels)
    if sys.byteorder == "big":
        blended.byteswap()
    raw_path = _temp_beside(out, ".pcm")
    try:
        Path(raw_path).write_bytes(blended.tobytes())
        raw_src = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le",
                   "-ar", str(rate), "-ac", str(channels), "-i", raw_path]
        tmp = _temp_beside(out, out.suffix)
        try:
            _run(raw_src + CODECS[out.suffix.lower()] + [tmp])
            _publish(tmp, out)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    finally:
        os.unlink(raw_path)
    return {"ok": True, "input": before, "output": probe(out, ffprobe), "crossfade_ms": args.crossfade_ms,
            "seam_correlation": round(r, 3), "crossfade_level_db": round(fade_db, 1), "clipped_samples": clipped}


def _crossfade_dip_db(blended: array.array, head: array.array, tail: array.array, channels: int) -> float:
    """Worst level of the blend against the audio it replaced, over quarters of the crossfade (dB, <= 0)."""
    step = max(channels, len(head) // 4 // channels * channels)
    worst = 0.0
    for at in range(0, len(head) - step + 1, step):
        power = sum(v * v for v in blended[at: at + step])
        reference = (sum(v * v for v in head[at: at + step]) + sum(v * v for v in tail[at: at + step])) / 2
        if reference > step * 10 ** 2:  # skip near-silence, where any ratio is noise
            worst = min(worst, 10 * math.log10(max(power, 1) / reference))
    return worst


def _blend(blended: array.array, head: array.array, tail: array.array, channels: int, r: float) -> int:
    """Equal-power crossfade of TAIL into HEAD, written over the start of BLENDED; returns clipped samples."""
    n = len(head) // channels
    clipped = 0
    for i in range(n):
        t = (i + 0.5) / n
        fade_in, fade_out = math.sin(t * math.pi / 2), math.cos(t * math.pi / 2)
        # Equal-power fades sum to sqrt(2) on correlated material (a hum, a tone): normalise the gain by
        # the segments' correlation. Capped: anti-correlated segments cancel, and boosting what is left
        # would only add noise.
        gain = min(1.5, 1 / math.sqrt(max(1e-6, fade_in ** 2 + fade_out ** 2 + 2 * r * fade_in * fade_out)))
        for c in range(channels):
            v = (head[i * channels + c] * fade_in + tail[i * channels + c] * fade_out) * gain
            if not -32768 <= v <= 32767:
                clipped += 1
            blended[i * channels + c] = max(-32768, min(32767, int(round(v))))
    return clipped


def _rms(chunk) -> float:
    return math.sqrt(sum(v * v for v in chunk) / max(1, len(chunk))) / 32768.0


def _level_step_p90(ch: array.array, window: int) -> float:
    """90th percentile of the level change (dB) between neighbouring WINDOW-long stretches of CH."""
    levels = [_rms(ch[at: at + window]) for at in range(0, len(ch) - window + 1, window)]
    steps = sorted(abs(20 * math.log10(max(a, 1e-6) / max(b, 1e-6)))
                   for a, b in zip(levels, levels[1:]) if max(a, b) >= 0.001)
    return steps[int(len(steps) * 0.9)] if steps else 0.0


def _seam(ch: array.array) -> dict:
    """Seam statistics for ONE channel (a mono mix would cancel anti-phase clicks)."""
    scale = 32768.0
    diffs = sorted(abs(ch[i + 1] - ch[i]) for i in range(0, len(ch) - 1, 7))
    typical = diffs[int(len(diffs) * 0.99)] / scale
    jump = abs(ch[0] - ch[-1]) / scale
    edge, ms = _LOOP_RATE // 20, _LOOP_RATE // 1000  # 50 ms and 1 ms either side of the seam
    head, tail = _rms(ch[:edge]), _rms(ch[-edge:])
    level_db = 20 * math.log10(max(head, 1e-6) / max(tail, 1e-6))
    # A level dip at the seam is either a fade (a dropout on every repeat) or a loop cut inside a rest
    # (fine): the samples can't tell them apart, so it is reported as a measurement with a note, never
    # as a verdict that would send anyone to regenerate a good loop.
    seam_level = _rms(ch[-ms:].tolist() + ch[:ms].tolist())
    around = _rms(ch[-200 * ms:].tolist() + ch[:200 * ms].tolist())
    dip_db = 20 * math.log10(max(seam_level, 1e-6) / max(around, 1e-6)) if around > 0.005 else 0.0
    # Out of a silent tail, a louder head is a sound starting (an onset), not a discontinuity: only
    # the sample jump can make that a click. A loud tail into a quiet head is a sound cut off.
    onset = level_db > 0 and tail < 0.001
    # 50 ms windows average out a hum's cycle-to-cycle swing, so on steady material a 6 dB step is the level
    # jumping on every repeat. Rhythmic material (drums) steps further than that between its own windows:
    # there the seam only counts when it steps more than 9 in 10 of the file's own steps. Between two
    # near-silent edges a level ratio is noise, not a jump anyone hears.
    level_limit = max(6.0, _level_step_p90(ch, edge))
    click = jump > max(2 * typical, 0.02) or (abs(level_db) > level_limit and not onset and max(head, tail) >= 0.001)
    return {"click": click, "seam_jump": jump, "typical_step_p99": typical, "edge_level_difference_db": level_db,
            "seam_dip_db": dip_db}


def _loop_edges(ffmpeg: str, src: Path, channels: int, duration: float | None) -> tuple[array.array, float]:
    """Samples to judge the seam on, and the file's length. A long file is decoded as its first and last
    ``_LOOP_EDGE_SECONDS`` joined end to end: the seam (last sample -> first) is the same, and the whole
    file never has to fit in memory."""
    if duration is None or duration <= 3 * _LOOP_EDGE_SECONDS:
        samples = _pcm_frames(ffmpeg, src, channels, _LOOP_RATE)
        return samples, len(samples) // channels / _LOOP_RATE
    head = _pcm_frames(ffmpeg, src, channels, _LOOP_RATE, seek=["-t", str(_LOOP_EDGE_SECONDS)])
    tail = _pcm_frames(ffmpeg, src, channels, _LOOP_RATE, seek=["-sseof", f"-{_LOOP_EDGE_SECONDS}"])
    return head + tail, duration


def cmd_loopcheck(args) -> dict:
    src = _input(args.input)
    ffmpeg, ffprobe = _tool("ffmpeg", args.ffmpeg), _tool("ffprobe", args.ffprobe)
    info = probe(src, ffprobe)
    channels = max(1, min(8, int(info["channels"] or 1)))
    samples, duration = _loop_edges(ffmpeg, src, channels, info["duration_seconds"])
    if duration < 0.05:
        raise Failure("bad_input", f"{src.name} is shorter than 50 ms; nothing to loop")
    result = {"ok": True, "file": str(src), "verdict": "silent", "channels_checked": channels,
              "duration_seconds": round(duration, 3)}
    live = [(c, ch) for c, ch in ((c, samples[c::channels]) for c in range(channels))
            if max(max(ch), -min(ch)) > _SILENT_PEAK]
    if not live:
        result["note"] = "the file is (near-)digital silence; there is no seam to judge"
        return result
    stats = [(c, _seam(ch)) for c, ch in live]
    worst_c, worst = max(stats, key=lambda cs: (cs[1]["click"], cs[1]["seam_jump"] / max(cs[1]["typical_step_p99"], 1e-3)))
    dip_db = min(st["seam_dip_db"] for _, st in stats)
    result.update(verdict="click" if worst["click"] else "seamless", worst_channel=worst_c,
                  seam_jump=round(worst["seam_jump"], 4), typical_step_p99=round(worst["typical_step_p99"], 4),
                  edge_level_difference_db=round(worst["edge_level_difference_db"], 1), seam_dip_db=round(dip_db, 1))
    if dip_db < -20:
        result["dip_note"] = ("the level drops sharply at the seam: a fade there is a dropout on every repeat, "
                              "a cut inside a rest is fine; listen before changing it")
    if src.suffix.lower() == ".mp3":
        result["note"] = ("MP3 decoders add encoder padding at the start; convert the loop to .ogg or .wav "
                          "for engine playback and check that file")
    return result


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")  # ünï -> uni
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:48]


def _pack_ext(info: dict, src: Path, fmt: str | None) -> str:
    if fmt:
        return FORMAT_EXT[fmt]
    if src.suffix.lower() in AUDIO_EXT:
        return src.suffix.lower()
    if info["codec"] == "opus":
        return ".opus"
    container = (info["container"] or "").split(",")[0]  # extensionless source: name it by what it is
    return {"mov": ".m4a", "matroska": ".mka", "aac": ".aac"}.get(container, f".{container}" if container else ".bin")


def _pack_stem(src: Path) -> str:
    stem = src.stem if src.suffix.lower() in AUDIO_EXT else src.name
    stem = re.sub(r"^sfx_|_\d{8}_\d{6}_[0-9a-f]+$", "", stem)
    return _slug(re.sub(r"^\d{2}-", "", stem)) or "sound"  # re-packing a pack's own 01-x files keeps x


def cmd_pack(args) -> dict:
    name = _slug(args.name)
    if not name:
        raise Failure("bad_args", "--name must contain letters or digits")
    out_dir = Path(args.out_dir).expanduser().resolve()
    if not out_dir.is_dir():
        raise Failure("bad_args", f"--out-dir does not exist: {out_dir}")
    sources = [_input(f) for f in args.files]
    prompts: dict = {}
    if args.prompts:
        try:
            prompts = json.loads(Path(args.prompts).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Failure("bad_args", f"--prompts must be a JSON object file: {exc}") from None
        if not isinstance(prompts, dict):
            raise Failure("bad_args", "--prompts must map file names to prompts (a JSON object)")
    ffprobe = _tool("ffprobe", args.ffprobe)
    ffmpeg = _tool("ffmpeg", args.ffmpeg) if args.format else None
    folder = out_dir / name
    zip_path = out_dir / f"{name}.zip"
    if (folder.exists() or folder.is_symlink() or zip_path.exists() or zip_path.is_symlink()) and not args.force:
        raise Failure("bad_args", f"{folder} or {zip_path} exists; pass --force to replace them")
    # Build in a fresh staging folder and swap it in by rename: a re-pack never keeps files from an
    # earlier pack, and never writes through a symlink left in the old folder or at the zip path.
    if zip_path.is_dir() and not zip_path.is_symlink():
        raise Failure("bad_args", f"{zip_path} is a folder; move it away first")
    umask = os.umask(0)
    os.umask(umask)
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{name}.staging-", dir=out_dir))
    except OSError as exc:  # a read-only --out-dir
        raise _cannot_write(out_dir, exc) from None
    tmp_zip = None
    try:
        entries = _fill_pack(staging, sources, prompts, args, ffmpeg, ffprobe)
        manifest = {"name": args.name, "sounds": entries}
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                               encoding="utf-8")
        fd, tmp_zip = tempfile.mkstemp(prefix=f".{name}.", suffix=".zip", dir=out_dir)
        os.close(fd)
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.iterdir()):
                archive.write(path, f"{name}/{path.name}")
        os.chmod(staging, 0o777 & ~umask)  # mkdtemp/mkstemp are private; a delivered pack follows the umask
        os.chmod(tmp_zip, 0o666 & ~umask)
        # Folder first, then the zip: if the old folder can't be moved (a file in it is open), nothing changed.
        old = None
        if folder.is_symlink() or folder.is_file():
            folder.unlink()
        elif folder.exists():
            old = Path(tempfile.mkdtemp(prefix=f".{name}.old-", dir=out_dir))
            try:
                os.replace(folder, old / name)
            except OSError as exc:
                shutil.rmtree(old, ignore_errors=True)
                raise Failure("bad_input", f"could not replace {folder} (is a file in it open?): {exc.strerror or exc}") from None
        os.replace(staging, folder)
        try:
            os.replace(tmp_zip, zip_path)  # replaces a symlink itself, never its target
        except OSError as exc:
            # Put the old pack back so folder and zip still agree.
            os.replace(folder, staging)
            if old is not None:
                os.replace(old / name, folder)
                shutil.rmtree(old, ignore_errors=True)
            raise Failure("bad_input", f"could not replace {zip_path} (is it open?): {exc.strerror or exc}") from None
        tmp_zip = None
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)  # never follows links out; a locked leftover is only hidden clutter
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if tmp_zip and os.path.exists(tmp_zip):
            os.unlink(tmp_zip)
    return {"ok": True, "folder": str(folder), "zip": str(zip_path), "count": len(entries),
            "manifest": str(folder / "manifest.json"), "sounds": entries}


def _fill_pack(folder: Path, sources: list[Path], prompts: dict, args, ffmpeg: str | None, ffprobe: str) -> list[dict]:
    entries, used = [], set()
    for index, src in enumerate(sources, 1):
        ext = _pack_ext(probe(src, ffprobe), src, args.format)  # probe the source: errors name it, not staging
        target = folder / f"{index:02d}-{_pack_stem(src)}{ext}"
        while target.name in used:
            target = target.with_name(f"{target.stem}-x{ext}")
        used.add(target.name)
        if ffmpeg and src.suffix.lower() != ext:
            _encode(ffmpeg, src, target, [], None)
        else:
            shutil.copyfile(src, target)
        info = probe(target, ffprobe)
        entry = {"file": target.name, "source": src.name, "duration_seconds": info["duration_seconds"],
                 "sample_rate": info["sample_rate"], "channels": info["channels"], "codec": info["codec"],
                 "bytes": info["bytes"]}
        prompt = prompts.get(src.name) or prompts.get(str(src))
        if isinstance(prompt, str):
            entry["prompt"] = prompt
        entries.append(entry)
    return entries


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="sfx_tool.py", description="Local post-processing for generated sound effects.")
    parser.add_argument("--ffmpeg", help="ffmpeg executable (default: ffmpeg on PATH)")
    parser.add_argument("--ffprobe", help="ffprobe executable (default: ffprobe on PATH)")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    p = sub.add_parser("probe", help="duration, codec, sample rate, channels")
    p.add_argument("input")
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("process", help="trim silence, normalise loudness, fade, convert")
    p.add_argument("input")
    p.add_argument("--out", required=True, help="output file; its extension picks the format (.ogg .wav .mp3 .opus)")
    p.add_argument("--trim", dest="trim", action="store_true", default=False,
                   help="trim leading/trailing silence (one-shots; never on loops)")
    p.add_argument("--no-trim", dest="trim", action="store_false", help="keep the edges (the default)")
    p.add_argument("--loudness", type=float, default=None, help="target integrated loudness in LUFS, e.g. -16")
    p.add_argument("--fade-ms", type=float, default=0.0, help="fade in and out, milliseconds")
    p.add_argument("--force", action="store_true", help="replace an existing output file")
    p.set_defaults(fn=cmd_process)
    p = sub.add_parser("loopfix", help="crossfade a loop's end into its start (fixes a click without a gap)")
    p.add_argument("input")
    p.add_argument("--out", required=True, help="output file; its extension picks the format")
    p.add_argument("--crossfade-ms", type=float, default=20.0, help="crossfade length, milliseconds (default 20)")
    p.add_argument("--force", action="store_true", help="replace an existing output file")
    p.set_defaults(fn=cmd_loopfix)
    p = sub.add_parser("loopcheck", help="check a loop's seam for a click; measure any level dip")
    p.add_argument("input")
    p.set_defaults(fn=cmd_loopcheck)
    p = sub.add_parser("pack", help="named folder + manifest.json + zip")
    p.add_argument("files", nargs="+")
    p.add_argument("--name", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--format", choices=sorted(FORMAT_EXT), help="convert every file to this format")
    p.add_argument("--prompts", help="JSON file mapping source file names to the prompts that made them")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_pack)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return emit(args.fn(args))
    except Failure as exc:
        return emit({"ok": False, "code": exc.code, "error": exc.error})
    except SystemExit as exc:  # --help
        return int(exc.code or 0)
    except KeyboardInterrupt:
        return emit({"ok": False, "code": "interrupted", "error": "interrupted"})
    except Exception as exc:  # noqa: BLE001 -- the contract is one JSON line, never a traceback
        return emit({"ok": False, "code": "internal", "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    sys.exit(main())
