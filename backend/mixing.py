"""FFmpeg renderer: trims segments, levels them, chains crossfades, limits and encodes MP3."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
SR = 44100
LIMIT_LINEAR = 0.79  # ≈ -2 dBFS: headroom for MP3 inter-sample overshoot


class RenderError(Exception):
    pass


def build_command(plan: dict, sources: dict[str, Path], out_path: Path) -> list[str]:
    segments = plan["segments"]
    transitions = plan["transitions"]
    if len(segments) < 2:
        raise RenderError("Miks için en az iki ses kaynağı gerekli.")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    for seg in segments:
        src = sources[seg["id"]]
        src_dur = seg.get("source_duration_sec", seg["duration_sec"])
        # small pad so a stretched segment can always be trimmed to its exact length below
        cmd += ["-ss", f"{seg['start_sec']:.4f}", "-t", f"{src_dur + 0.1:.4f}", "-i", str(src)]

    parts = []
    for i, seg in enumerate(segments):
        stretch = float(seg.get("stretch", 1.0))
        chain = [f"aformat=sample_fmts=fltp:sample_rates={SR}:channel_layouts=stereo"]
        if abs(stretch - 1) > 1e-4:
            chain.append(f"atempo={stretch:.5f}")  # tempo match, pitch preserved
        # exact length in samples: the next crossfade then starts on the planned beat
        chain += ["apad=pad_dur=0.2", f"atrim=end_sample={round(seg['duration_sec'] * SR)}",
                  f"volume={seg['gain_db']:.2f}dB"]
        parts.append(f"[{i}:a]{','.join(chain)}[s{i}]")
    prev = "s0"
    for i, t in enumerate(transitions, start=1):
        # beat crossfades use equal-power curves; safe fades use a gentler exp/log pair
        c1, c2 = ("qsin", "qsin") if t["type"] == "beat_crossfade" else ("exp", "log")
        label = f"x{i}"
        ns = max(1, round(t["duration_sec"] * SR))  # in samples, so overlaps are exact
        parts.append(f"[{prev}][s{i}]acrossfade=ns={ns}:c1={c1}:c2={c2}[{label}]")
        prev = label
    total = float(plan["total_duration_sec"])
    fade_out = min(3.0, total / 10)
    parts.append(
        f"[{prev}]afade=t=in:d=0.4,afade=t=out:st={max(0.0, total - fade_out):.3f}:d={fade_out:.3f},"
        f"alimiter=limit={LIMIT_LINEAR}:attack=5:release=60:level=false[out]"
    )
    cmd += ["-filter_complex", ";".join(parts), "-map", "[out]",
            "-ar", str(SR), "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k", str(out_path)]
    return cmd


def render_mix(plan: dict, sources: dict[str, Path], out_path: Path, timeout: int = 180) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(plan, sources, out_path)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        raise RenderError("Render zaman aşımına uğradı.") from err
    if proc.returncode != 0 or not out_path.is_file():
        raise RenderError(f"FFmpeg render başarısız: {proc.stderr.strip()[-400:]}")
    return out_path


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as err:
        raise RenderError("Çıktı süresi okunamadı.") from err


def audio_stats(path: Path, silence_db: float = -50.0) -> dict:
    """Decode to float PCM and report peak, longest silence run, duration (for QA/tests)."""
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", "8000", "-"],
        capture_output=True, timeout=60,
    )
    stereo = np.frombuffer(proc.stdout, dtype=np.float32)
    if stereo.size < 2:
        raise RenderError("Çıktı çözümlenemedi.")
    stereo = stereo[: stereo.size // 2 * 2].reshape(-1, 2)
    peak = float(np.max(np.abs(stereo)))
    x = stereo.mean(axis=1)  # (ffmpeg's own mono downmix can add gain)
    frame = 400  # 50 ms @ 8 kHz
    n = x.size // frame
    rms = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    quiet = 20 * np.log10(rms) < silence_db
    longest = run = 0
    for q in quiet:
        run = run + 1 if q else 0
        longest = max(longest, run)
    return {
        "peak": peak,
        "longest_silence_sec": longest * frame / 8000,
        "duration_sec": x.size / 8000,
    }
