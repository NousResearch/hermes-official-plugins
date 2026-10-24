"""The skill's post-processing script: one JSON line on stdout for success AND every failure, exit
0/1, never a traceback. Failure paths run everywhere; ffmpeg-backed paths run where ffmpeg exists."""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import wave
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "elevenlabs-sound-design" / "scripts" / "sfx_tool.py"
needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe not installed on this runner")


def run(*args: str, env: dict | None = None) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=120, check=False, env=env)
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, f"expected one JSON line, got stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "Traceback" not in proc.stderr and "Traceback" not in proc.stdout
    payload = json.loads(lines[0])
    assert payload["ok"] is (proc.returncode == 0)
    return proc.returncode, payload


def tone(path: Path, seconds: float, freq: float, *, rate: int = 48000, pad: float = 0.0) -> Path:
    silence = b"\x00\x00" * int(pad * rate)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(silence + b"".join(
            struct.pack("<h", int(16000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(int(seconds * rate)))
            + silence)
    return path


def test_ffmpeg_missing_is_a_clean_json_error(tmp_path):
    src = tone(tmp_path / "a.wav", 0.2, 440)
    for args in (("loopcheck", str(src)), ("probe", str(src)), ("process", str(src), "--out", str(tmp_path / "b.ogg"))):
        code, out = run("--ffmpeg", "no-such-ffmpeg-binary", "--ffprobe", "no-such-ffprobe-binary", *args)
        assert code == 1 and out["code"] == "ffmpeg_missing" and "ffmpeg" in out["error"]


def test_non_ascii_paths_stay_one_json_line_on_a_legacy_code_page(tmp_path):
    import os

    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    code, out = run("probe", str(tmp_path / "sonido-ñandú-音.wav"), env=env)
    assert code == 1 and out["code"] == "bad_input" and "音" in out["error"]


def test_bad_input_and_bad_arguments_are_clean_json_errors(tmp_path):
    assert run("probe", str(tmp_path / "missing.wav"))[1]["code"] == "bad_input"
    assert run("frobnicate")[1]["code"] == "bad_args"
    assert run("process", str(tone(tmp_path / "a.wav", 0.2, 440)))[1]["code"] == "bad_args"  # --out missing


@needs_ffmpeg
def test_garbage_audio_is_reported_not_raised(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"this is not audio")
    for args in (("probe", str(bad)), ("loopcheck", str(bad)), ("process", str(bad), "--out", str(tmp_path / "x.wav"))):
        code, out = run(*args)
        assert code == 1 and out["code"] in {"bad_input", "ffmpeg_failed"}


@needs_ffmpeg
def test_process_trims_normalises_fades_and_converts(tmp_path):
    src = tone(tmp_path / "padded.wav", 0.5, 440, pad=0.4)
    out_path = tmp_path / "clean.ogg"
    plain = run("process", str(src), "--out", str(tmp_path / "plain.wav"))[1]
    assert plain["output"]["duration_seconds"] == plain["input"]["duration_seconds"]  # no trim unless asked
    code, out = run("process", str(src), "--out", str(out_path), "--trim", "--loudness", "-16", "--fade-ms", "10")
    assert code == 0 and out_path.is_file()
    assert out["output"]["codec"] == "vorbis" and out["output"]["sample_rate"] == 48000
    assert out["output"]["duration_seconds"] < out["input"]["duration_seconds"] - 0.5  # silence trimmed
    # Never overwrites: the same output again refuses without --force; the input itself always refuses.
    assert run("process", str(src), "--out", str(out_path))[1]["code"] == "bad_args"
    assert run("process", str(src), "--out", str(src), "--force")[1]["code"] == "bad_args"


@needs_ffmpeg
def test_loopcheck_tells_a_clean_seam_from_a_click(tmp_path):
    # 480 Hz at 48 kHz: exactly 100 samples per cycle, so 1.0 s is a whole number of cycles.
    clean = run("loopcheck", str(tone(tmp_path / "clean.wav", 1.0, 480)))[1]
    # Cut at a quarter cycle: the last sample sits at the peak while the first is at zero.
    clicky = run("loopcheck", str(tone(tmp_path / "click.wav", 1.0 + 25 / 48000, 480)))[1]
    assert clean["verdict"] == "seamless"
    assert clicky["verdict"] == "click" and clicky["seam_jump"] > clean["seam_jump"]


@needs_ffmpeg
def test_loopfix_removes_the_click_and_a_faded_seam_is_flagged(tmp_path):
    clicky = tone(tmp_path / "click.wav", 1.0 + 25 / 48000, 480)
    fixed = tmp_path / "fixed.wav"
    code, out = run("loopfix", str(clicky), "--out", str(fixed), "--crossfade-ms", "20")
    assert code == 0 and abs(out["output"]["duration_seconds"] - (out["input"]["duration_seconds"] - 0.02)) < 0.002
    assert out["clipped_samples"] == 0
    assert run("loopcheck", str(fixed))[1]["verdict"] == "seamless"
    faded = tmp_path / "faded.wav"
    assert run("process", str(clicky), "--out", str(faded), "--fade-ms", "8")[0] == 0
    faded_check = run("loopcheck", str(faded))[1]
    assert faded_check["seam_dip_db"] < -20 and "dip_note" in faded_check
    assert run("process", str(clicky), "--out", str(tmp_path / "x.opus"), "--loudness", "-16")[0] == 0  # 48 kHz forced
    # A loud tonal loop keeps its level through the seam (no equal-power bump into clipping).
    hot = tmp_path / "hot.wav"
    with wave.open(str(hot), "wb") as out_wav:
        out_wav.setnchannels(1)
        out_wav.setsampwidth(2)
        out_wav.setframerate(48000)
        out_wav.writeframes(b"".join(struct.pack("<h", int(30000 * math.sin(2 * math.pi * 480 * i / 48000)))
                                     for i in range(48000 + 25)))
    assert run("loopfix", str(hot), "--out", str(tmp_path / "hotfix.wav"))[1]["clipped_samples"] == 0


@needs_ffmpeg
def test_a_loop_cut_inside_a_rest_is_seamless(tmp_path):
    rate, frames = 48000, []
    for i in range(rate):  # four short tone bursts per second with silence around each
        phase = i % (rate // 4)
        frames.append(int(14000 * math.sin(2 * math.pi * 700 * i / rate) * math.exp(-phase / 1500))
                      if 4000 < phase < 9000 else 0)
    beats = tmp_path / "beats.wav"
    with wave.open(str(beats), "wb") as out_wav:
        out_wav.setnchannels(1)
        out_wav.setsampwidth(2)
        out_wav.setframerate(rate)
        out_wav.writeframes(b"".join(struct.pack("<h", v) for v in frames))
    assert run("loopcheck", str(beats))[1]["verdict"] == "seamless"


@needs_ffmpeg
def test_pack_writes_folder_manifest_and_zip(tmp_path):
    a = tone(tmp_path / "sfx_coin_20261023_120000_aaaaaaaaaaaa.wav", 0.3, 880)
    b = tone(tmp_path / "sfx_door_20261023_120000_bbbbbbbbbbbb.wav", 0.4, 220)
    prompts = tmp_path / "prompts.json"
    prompts.write_text(json.dumps({a.name: "retro coin pickup"}), encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    code, out = run("pack", str(a), str(b), "--name", "UI Pack", "--out-dir", str(out_dir), "--format", "ogg",
                    "--prompts", str(prompts))
    assert code == 0 and out["count"] == 2
    manifest = json.loads((out_dir / "ui-pack" / "manifest.json").read_text(encoding="utf-8"))
    assert [s["file"] for s in manifest["sounds"]] == ["01-coin.ogg", "02-door.ogg"]
    assert manifest["sounds"][0]["prompt"] == "retro coin pickup" and manifest["sounds"][0]["codec"] == "vorbis"
    with zipfile.ZipFile(out_dir / "ui-pack.zip") as archive:
        assert sorted(archive.namelist()) == ["ui-pack/01-coin.ogg", "ui-pack/02-door.ogg", "ui-pack/manifest.json"]

    # Re-pack with fewer sounds: nothing from the earlier pack survives, and a symlink planted in the old
    # folder is replaced, not written through.
    victim = tmp_path / "victim.wav"
    victim.write_bytes(b"keep me")
    planted = out_dir / "ui-pack" / "01-coin.ogg"
    planted.unlink()
    try:
        planted.symlink_to(victim)
    except (OSError, NotImplementedError):
        pass
    assert run("pack", str(b), "--name", "UI Pack", "--out-dir", str(out_dir), "--force")[0] == 0
    assert victim.read_bytes() == b"keep me"
    assert sorted(p.name for p in (out_dir / "ui-pack").iterdir()) == ["01-door.wav", "manifest.json"]
    with zipfile.ZipFile(out_dir / "ui-pack.zip") as archive:
        assert sorted(archive.namelist()) == ["ui-pack/01-door.wav", "ui-pack/manifest.json"]
    assert not [p for p in out_dir.iterdir() if p.name.startswith(".")]  # no staging leftovers
    if os.name == "posix":  # the delivered pack follows the umask, not mkdtemp's private 0700
        umask = os.umask(0)
        os.umask(umask)
        assert (out_dir / "ui-pack").stat().st_mode & 0o777 == 0o777 & ~umask
        assert (out_dir / "ui-pack.zip").stat().st_mode & 0o777 == 0o666 & ~umask


def write_wav(path: Path, channels: list[list[float]], rate: int = 48000) -> Path:
    """Interleave per-channel float samples (int16 scale) into a 16-bit WAV."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(len(channels))
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(struct.pack("<h", max(-32767, min(32767, int(v))))
                                 for frame in zip(*channels) for v in frame))
    return path


@needs_ffmpeg
def test_an_unwritable_output_names_out_not_a_hidden_temp_file(tmp_path):
    src = tone(tmp_path / "a.wav", 0.3, 440)
    (tmp_path / "d.wav").mkdir()
    code, out = run("process", str(src), "--out", str(tmp_path / "d.wav"), "--force")
    assert code == 1 and out["code"] == "bad_args" and "d.wav" in out["error"] and ".sfx_tool_" not in out["error"]
    if os.name != "posix" or os.geteuid() == 0:
        return
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        for args in (("process", str(src), "--out", str(locked / "x.wav")),
                     ("loopfix", str(src), "--out", str(locked / "x.wav")),
                     ("pack", str(src), "--name", "p", "--out-dir", str(locked))):
            code, out = run(*args)
            assert code == 1 and out["code"] == "bad_args" and str(locked) in out["error"], args
            assert ".sfx_tool_" not in out["error"] and "staging" not in out["error"]
    finally:
        locked.chmod(0o755)


@needs_ffmpeg
def test_process_reports_the_loudness_it_measured_not_the_target(tmp_path):
    steady = run("process", str(tone(tmp_path / "t.wav", 2.0, 440)), "--out", str(tmp_path / "t.ogg"), "--loudness", "-16")[1]
    assert abs(steady["output"]["integrated_lufs"] + 16) <= 1 and steady["output"]["true_peak_dbtp"] <= -1.4
    assert steady["applied"]["loudness_target_lufs"] == -16 and "loudness_note" not in steady["applied"]
    # A quiet tone under sharp clicks: loudnorm can't reach -16 here, and the JSON must say so.
    rate = 48000
    quiet = [300 * math.sin(2 * math.pi * 440 * i / rate) + (30000 * (-1) ** i if i % (rate // 2) < 48 else 0)
             for i in range(2 * rate)]
    peaky = run("process", str(write_wav(tmp_path / "p.wav", [quiet])), "--out", str(tmp_path / "p2.wav"),
                "--loudness", "-16")[1]
    assert peaky["output"]["integrated_lufs"] < -17 and "loudness_note" in peaky["applied"]


@needs_ffmpeg
def test_a_fade_longer_than_half_the_clip_is_refused(tmp_path):
    src = tone(tmp_path / "two.wav", 2.0, 440)
    code, out = run("process", str(src), "--out", str(tmp_path / "x.wav"), "--fade-ms", "1000")
    assert code == 1 and out["code"] == "bad_args" and "2 s" in out["error"]
    assert not (tmp_path / "x.wav").exists()
    padded = tone(tmp_path / "padded.wav", 0.5, 440, pad=0.4)  # 1.3 s; 0.5 s once trimmed
    assert run("process", str(padded), "--out", str(tmp_path / "y.wav"), "--fade-ms", "300")[0] == 0
    code, out = run("process", str(padded), "--out", str(tmp_path / "z.wav"), "--fade-ms", "300", "--trim")
    assert code == 1 and out["code"] == "bad_args" and "after trimming" in out["error"]


@needs_ffmpeg
def test_loopcheck_judges_every_channel_and_tells_silence_and_onsets_apart(tmp_path):
    rate = 48000
    # 480 Hz cut at a quarter cycle clicks; the right channel is its exact inverse, so a mono mix is silent.
    left = [16000 * math.sin(2 * math.pi * 480 * i / rate) for i in range(rate + 25)]
    anti = run("loopcheck", str(write_wav(tmp_path / "anti.wav", [left, [-v for v in left]])))[1]
    assert anti["verdict"] == "click" and anti["channels_checked"] == 2
    assert run("loopcheck", str(write_wav(tmp_path / "silent.wav", [[0.0] * rate] * 2)))[1]["verdict"] == "silent"
    # A silent tail and a hit starting 3 ms after the seam: the sound begins, nothing is cut.
    hit = [0.0 if i < 144 or i > rate // 2 else 20000 * math.sin(2 * math.pi * 200 * (i - 144) / rate)
           * math.exp(-(i - 144) / 4000) for i in range(rate)]
    assert run("loopcheck", str(write_wav(tmp_path / "hit.wav", [hit])))[1]["verdict"] == "seamless"


@needs_ffmpeg
def test_loopfix_measures_the_dip_an_anti_correlated_seam_leaves(tmp_path):
    def sine(frames: int) -> Path:  # 480 Hz is 100 samples a cycle; the 20 ms tail starts at frames - 960
        return write_wav(tmp_path / f"s{frames}.wav", [[16000 * math.sin(2 * math.pi * 480 * i / 48000)
                                                        for i in range(frames)]])

    out = run("loopfix", str(sine(48010)), "--out", str(tmp_path / "anti_fix.wav"))[1]  # tail half a cycle off
    assert out["seam_correlation"] < -0.9 and out["crossfade_level_db"] < -3
    matched = run("loopfix", str(sine(48060)), "--out", str(tmp_path / "in_fix.wav"))[1]  # tail in phase
    assert matched["seam_correlation"] > 0.9 and matched["crossfade_level_db"] > -1


@needs_ffmpeg
def test_opus_duration_is_the_decoded_length(tmp_path):
    out = run("process", str(tone(tmp_path / "one.wav", 1.0, 440)), "--out", str(tmp_path / "one.opus"))[1]
    assert abs(out["output"]["duration_seconds"] - 1.0) <= 0.002  # not 1.0065: Opus pre-skip is not played


@needs_ffmpeg
def test_pack_names_extensionless_repacked_and_accented_sources(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    accented = tone(src / "\u00fcn\u00ef snare.wav", 0.2, 440)
    repacked = tone(src / "01-coin.wav", 0.2, 660)
    bare = tone(src / "noext.wav", 0.2, 880).rename(src / "noext")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    code, out = run("pack", str(accented), str(repacked), str(bare), "--name", "P", "--out-dir", str(out_dir))
    assert code == 0 and [s["file"] for s in out["sounds"]] == ["01-uni-snare.wav", "02-coin.wav", "03-noext.wav"]
    junk = src / "notaudio.wav"
    junk.write_bytes(b"not audio at all")
    code, out = run("pack", str(repacked), str(junk), "--name", "Q", "--out-dir", str(out_dir))
    assert code == 1 and "notaudio.wav" in out["error"] and "staging" not in out["error"]


def wav_mono(path: Path, samples, rate: int = 48000) -> Path:
    return write_wav(path, [list(samples)], rate)


def _mean_db(path: Path) -> float:
    with wave.open(str(path)) as w:
        raw = w.readframes(w.getnframes())
    values = struct.unpack(f"<{len(raw) // 2}h", raw)
    return 20 * math.log10(math.sqrt(sum(v * v for v in values) / len(values)) / 32768)


@needs_ffmpeg
def test_a_clip_too_short_for_one_loudness_block_is_levelled_and_measured_looped(tmp_path):
    rate = 48000  # 997 Hz, not 1 kHz: a 48-sample-periodic WAV can probe as MPEG-TS in ffmpeg 9
    blip = wav_mono(tmp_path / "blip.wav", (1000 * math.sin(2 * math.pi * 997 * i / rate) for i in range(int(0.3 * rate))))
    code, out = run("process", str(blip), "--out", str(tmp_path / "b.wav"), "--loudness", "-16")
    assert code == 0 and out["output"]["integrated_lufs"] is not None  # never the -70 "nothing measured" gate
    assert abs(out["output"]["integrated_lufs"] + 16) <= 1 and "looped" in out["output"]["loudness_measured_on"]
    gained = _mean_db(tmp_path / "b.wav") - _mean_db(blip)
    assert abs(gained - out["applied"]["loudness_gain_db"]) <= 0.5 and 10 < gained < 20  # not pushed to the ceiling
    assert "loudness_note" not in out["applied"]
    # A peaky short clip: the gain stops at the true-peak ceiling, and the note says so truthfully.
    spiky = wav_mono(tmp_path / "spiky.wav", (300 * math.sin(2 * math.pi * 997 * i / rate) + (20000 if i == 5000 else 0)
                                              for i in range(int(0.3 * rate))))
    out = run("process", str(spiky), "--out", str(tmp_path / "s.wav"), "--loudness", "-16")[1]
    assert out["output"]["true_peak_dbtp"] <= -1.4 and out["output"]["integrated_lufs"] < -17
    assert "ceiling" in out["applied"]["loudness_note"]
    silent = wav_mono(tmp_path / "silent.wav", [0] * int(0.3 * rate))
    out = run("process", str(silent), "--out", str(tmp_path / "z.wav"), "--loudness", "-16")[1]
    assert out["output"]["integrated_lufs"] is None and "silent" in out["applied"]["loudness_note"]


@needs_ffmpeg
def test_loopcheck_flags_a_level_jump_at_the_seam_but_not_a_rhythm_that_continues(tmp_path):
    rate = 48000
    # A 480 Hz crescendo (whole cycles, so no sample jump) rising 9 dB: every repeat drops back 9 dB.
    swell = wav_mono(tmp_path / "swell.wav", ((5000 * 10 ** (9 / 20 * i / rate)) * math.sin(2 * math.pi * 480 * i / rate)
                                               for i in range(rate)))
    out = run("loopcheck", str(swell))[1]
    assert out["verdict"] == "click" and abs(out["edge_level_difference_db"]) > 6
    # A gated texture (100 ms loud, 100 ms at -12 or -15.6 dB): the 12 dB step at the seam is one of its own.
    def gate(i: int) -> float:
        period, half = divmod(i // (rate // 10), 2)
        return 1.0 if half else (0.25 if period % 2 == 0 else 1 / 6)
    gated = wav_mono(tmp_path / "gated.wav", (16000 * gate(i) * math.sin(2 * math.pi * 480 * i / rate)
                                              for i in range(int(0.8 * rate))))
    assert run("loopcheck", str(gated))[1]["verdict"] == "seamless"


@needs_ffmpeg
def test_loopcheck_judges_a_long_file_at_its_real_seam(tmp_path):
    # 100 s of 8-channel audio decodes to ~77 MB at 48 kHz: the old check stopped at 64 MiB (87.4 s) and
    # judged a seam in the middle. Here the real end drops 20 dB, so the true seam is a level jump.
    long_file = tmp_path / "long.flac"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-filter_complex",
                    "sine=f=750:r=8000:d=97[a];sine=f=750:r=8000:d=3,volume=0.1[b];[a][b]concat=n=2:v=0:a=1",
                    "-ac", "8", str(long_file)], check=True, timeout=60)
    out = run("loopcheck", str(long_file))[1]
    assert abs(out["duration_seconds"] - 100) < 0.1 and out["channels_checked"] == 8
    assert out["verdict"] == "click" and out["edge_level_difference_db"] > 15
