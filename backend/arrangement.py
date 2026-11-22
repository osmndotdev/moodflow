"""Automatic section selection and transition planning (librosa + NumPy).

Produces a JSON-serialisable plan:
  {"segments": [{"id", "start_sec", "duration_sec", "gain_db", "bpm"}...],
   "transitions": [{"from", "to", "type", "duration_sec"}...],
   "total_duration_sec": ...}
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

ANALYSIS_SR = 22050
HOP = 512
TARGET_TOTAL_SEC = 85.0
MIN_SEG, MAX_SEG = 15.0, 30.0
TARGET_RMS_DB = -16.0
SAFE_FADE_SEC = 2.0


@dataclass
class TrackAnalysis:
    duration: float
    bpm: float | None
    beat_times: np.ndarray
    beat_confidence: float
    rms: np.ndarray  # linear RMS per frame

    @property
    def frame_sec(self) -> float:
        return HOP / ANALYSIS_SR

    @property
    def loudness_db(self) -> float:
        """Mean frame loudness of the non-silent part (for playlist-relative energy)."""
        db = 20 * np.log10(np.maximum(self.rms, 1e-6))
        voiced = db[db > db.max() - 40]
        return float(np.mean(voiced if voiced.size else db))


class AnalysisError(Exception):
    pass


def analyze_track(path: Path, max_minutes: float = 8) -> TrackAnalysis:
    import librosa

    try:
        y, sr = librosa.load(str(path), sr=ANALYSIS_SR, mono=True, duration=max_minutes * 60)
    except Exception as err:  # noqa: BLE001 — corrupt/undecodable file
        raise AnalysisError(f"ses çözümlenemedi: {err}") from err
    duration = len(y) / sr
    if duration < 20:
        raise AnalysisError("ses çok kısa")
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    if float(rms.max()) < 1e-3:
        raise AnalysisError("ses sessiz görünüyor")
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, hop_length=HOP)
    beat_times = librosa.frames_to_time(beats, sr=sr, hop_length=HOP)
    bpm = float(np.atleast_1d(tempo)[0]) or None
    return TrackAnalysis(duration, bpm, beat_times, beat_confidence(beat_times), rms)


def beat_confidence(beat_times: np.ndarray) -> float:
    if len(beat_times) < 16:
        return 0.0
    ibi = np.diff(beat_times)
    cv = float(np.std(ibi) / max(np.mean(ibi), 1e-6))
    return float(np.clip(1 - cv * 4, 0, 1))


# --------------------------------------------------------------------------- transitions

def bpm_close(a: float | None, b: float | None, tol: float = 0.06) -> bool:
    if not a or not b:
        return False
    hi, lo = max(a, b), min(a, b)
    ratios = [hi / lo, hi / (2 * lo) if hi > 1.6 * lo else np.inf]
    return min(abs(r - 1) for r in ratios) <= tol


def plan_transition(a: TrackAnalysis, b: TrackAnalysis) -> tuple[str, float]:
    if bpm_close(a.bpm, b.bpm) and min(a.beat_confidence, b.beat_confidence) >= 0.6:
        beat = 60.0 / ((a.bpm + b.bpm) / 2)
        for n_beats in (8, 16, 4):  # a phrase-ish length that lands in 3..6 s
            if 3.0 <= n_beats * beat <= 6.0:
                return "beat_crossfade", round(n_beats * beat, 3)
        return "beat_crossfade", 4.0
    return "safe_fade", SAFE_FADE_SEC


# --------------------------------------------------------------------------- segment choice

def _snap(t: float, beats: np.ndarray, max_shift: float = 1.5) -> float:
    if len(beats) == 0:
        return t
    i = int(np.argmin(np.abs(beats - t)))
    return float(beats[i]) if abs(beats[i] - t) <= max_shift else t


def choose_segment(a: TrackAnalysis, target: float) -> tuple[float, float, float]:
    """Return (start_sec, duration_sec, segment_rms_db) for the strongest musical window."""
    fs = a.frame_sec
    r = a.rms
    n_frames = len(r)
    win = max(1, int(target / fs))
    lo_t = min(max(8.0, 0.08 * a.duration), max(0.0, a.duration - target - 1))
    hi_t = max(lo_t, a.duration - target - 4.0)

    starts = a.beat_times[(a.beat_times >= lo_t) & (a.beat_times <= hi_t)]
    if len(starts) < 4:
        starts = np.arange(lo_t, hi_t + 1e-6, 0.5)

    peak_db = 20 * np.log10(r.max())
    silent = (20 * np.log10(np.maximum(r, 1e-6)) < peak_db - 30).astype(float)
    csum = np.concatenate([[0.0], np.cumsum(r)])
    csil = np.concatenate([[0.0], np.cumsum(silent)])
    rmax = float(r.max())
    pre = max(1, int(4.0 / fs))

    best, best_score = float(starts[0]), -np.inf
    for s in starts:
        i0 = int(s / fs)
        i1 = min(n_frames, i0 + win)
        if i1 - i0 < win * 0.9:
            continue
        mean = (csum[i1] - csum[i0]) / (i1 - i0) / rmax
        sil = (csil[i1] - csil[i0]) / (i1 - i0)
        p0 = max(0, i0 - pre)
        pre_mean = (csum[i0] - csum[p0]) / max(1, i0 - p0) / rmax if i0 > p0 else mean
        lift = max(0.0, mean - pre_mean)  # favour windows where a section kicks in
        score = mean - 1.5 * sil + 0.3 * lift
        if score > best_score:
            best, best_score = float(s), score

    end = _snap(best + target, a.beat_times)
    end = min(end, a.duration - 0.5)
    duration = max(MIN_SEG * 0.8, end - best)
    i0, i1 = int(best / fs), min(n_frames, int((best + duration) / fs))
    seg_rms_db = 10 * np.log10(max(float(np.mean(r[i0:i1] ** 2)), 1e-10))
    return round(best, 3), round(duration, 3), round(float(seg_rms_db), 2)


def segment_length(n: int, transition_total: float) -> float:
    if n <= 0:
        return 0.0
    return float(np.clip((TARGET_TOTAL_SEC + transition_total) / n, MIN_SEG, MAX_SEG))


def build_plan(items: list[tuple[str, TrackAnalysis]]) -> dict:
    """items: [(occurrence_id, analysis)] in mix order (≥ 2)."""
    if len(items) < 2:
        raise ValueError("en az iki parça gerekli")
    transitions = []
    for (ida, a), (idb, b) in zip(items, items[1:]):
        kind, dur = plan_transition(a, b)
        transitions.append({"from": ida, "to": idb, "type": kind, "duration_sec": dur})
    seg_len = segment_length(len(items), sum(t["duration_sec"] for t in transitions))

    segments = []
    for idx, (oid, a) in enumerate(items):
        start, duration, seg_db = choose_segment(a, seg_len)
        segments.append({
            "id": oid,
            "start_sec": start,
            "duration_sec": duration,
            "gain_db": round(float(np.clip(TARGET_RMS_DB - seg_db, -12, 9)), 2),
            "bpm": round(a.bpm, 1) if a.bpm else None,
            "beat_confidence": round(a.beat_confidence, 2),
        })
    # crossfade can't exceed half of either neighbouring segment
    for i, t in enumerate(transitions):
        limit = 0.45 * min(segments[i]["duration_sec"], segments[i + 1]["duration_sec"])
        t["duration_sec"] = round(min(t["duration_sec"], limit), 3)
    total = sum(s["duration_sec"] for s in segments) - sum(t["duration_sec"] for t in transitions)
    return {"segments": segments, "transitions": transitions, "total_duration_sec": round(total, 2)}
