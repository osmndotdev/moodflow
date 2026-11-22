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
TARGET_TOTAL_SEC = 170.0          # ~3 min: room for long, phrase-length transitions
MIN_SEG, MAX_SEG = 25.0, 45.0
TARGET_RMS_DB = -16.0
SAFE_FADE_SEC = 4.0
# beat crossfades: a musical phrase of the outgoing song (16 beats), 32 for fast and 8 for slow
# tempos, aiming at XFADE_MIN..XFADE_MAX seconds
XFADE_BEATS = (16, 32, 8)
XFADE_MIN_SEC, XFADE_MAX_SEC = 7.0, 12.0


@dataclass
class TrackAnalysis:
    duration: float
    bpm: float | None
    beat_times: np.ndarray
    beat_confidence: float
    rms: np.ndarray  # linear RMS per frame
    path: Path | None = None  # source file, for fine alignment on short slices
    bass_onset: np.ndarray | None = None  # low-frequency onset strength per frame (kick/bass hits)

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
    bpm = precise_bpm(beat_times) or float(np.atleast_1d(tempo)[0]) or None
    bass = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP, fmax=200, n_mels=32)
    return TrackAnalysis(duration, bpm, beat_times, beat_confidence(beat_times), rms, Path(path), bass)


def beat_confidence(beat_times: np.ndarray) -> float:
    if len(beat_times) < 16:
        return 0.0
    ibi = np.diff(beat_times)
    cv = float(np.std(ibi) / max(np.mean(ibi), 1e-6))
    return float(np.clip(1 - cv * 4, 0, 1))


# --------------------------------------------------------------------------- transitions

def precise_bpm(beat_times: np.ndarray) -> float | None:
    """Tempo from a line fit through the tracked beats (middle 80 % of the song).

    librosa's overall tempo estimate snaps to a coarse grid (161.5, 172.3 … BPM, 3–7 % apart at
    fast tempos), which made beat-matchable neighbours look too far apart; the beats themselves
    pin the tempo down far more precisely and on the same metrical level we align on.
    """
    if len(beat_times) < 16:
        return None
    trim = len(beat_times) // 10
    period = fit_period(beat_times[trim: len(beat_times) - trim])
    return 60.0 / period if period else None


def bpm_close(a: float | None, b: float | None, tol: float = 0.08) -> bool:
    if not a or not b:
        return False
    hi, lo = max(a, b), min(a, b)
    ratios = [hi / lo, hi / (2 * lo) if hi > 1.6 * lo else np.inf]
    return min(abs(r - 1) for r in ratios) <= tol


def plan_transition(a: TrackAnalysis, b: TrackAnalysis) -> tuple[str, float]:
    if bpm_close(a.bpm, b.bpm) and min(a.beat_confidence, b.beat_confidence) >= 0.6:
        beat = 60.0 / ((a.bpm + b.bpm) / 2)
        for n_beats in XFADE_BEATS:
            if XFADE_MIN_SEC <= n_beats * beat <= XFADE_MAX_SEC:
                return "beat_crossfade", round(n_beats * beat, 3)
        return "beat_crossfade", round(16 * beat, 3)
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


# --------------------------------------------------------------------------- beat alignment

MAX_STRETCH = 0.08     # never change a segment's tempo by more than ±8 % (pitch is preserved)
ALIGN_HOP = 128        # ~5.8 ms resolution for the fine alignment
STRONG_BEAT_SEARCH = 2  # look ±2 beats for the strongest bass hit (rough bar-start stand-in)


def local_period(beats: np.ndarray, t: float, n: int = 16) -> float | None:
    """Beat period around time ``t``: slope of a line fitted through ~``n`` beats.

    Beat times are quantised to the analysis frame (~23 ms), so a median of intervals is off
    by a few percent; the fitted slope is precise to well under a millisecond per beat.
    """
    if len(beats) < 4:
        return None
    i = int(np.argmin(np.abs(beats - t)))
    lo, hi = max(0, i - n // 2), min(len(beats), i + n // 2 + 1)
    return fit_period(beats[lo:hi])


def period_between(beats: np.ndarray, t0: float, t1: float) -> float | None:
    """Beat period fitted over the beats inside [t0, t1] (e.g. exactly the overlap)."""
    return fit_period(beats[(beats >= t0 - 1e-6) & (beats <= t1 + 1e-6)])


def fit_period(seg: np.ndarray) -> float | None:
    if len(seg) < 4:
        return None
    ibi = np.diff(seg)
    keep = np.abs(ibi - np.median(ibi)) < 0.25 * np.median(ibi)  # drop skipped / doubled beats
    if keep.sum() < 3:
        return float(np.median(ibi))
    idx = np.concatenate([[0], np.cumsum(np.where(keep, 1, np.round(ibi / np.median(ibi))))])
    slope = np.polyfit(idx, seg, 1)[0]
    return float(slope)


def fold_ratio(target_period: float, period: float) -> float:
    """Stretch factor f (play ``f``× faster) that makes ``period`` match ``target_period``,
    allowing half/double time. Returned so that |f - 1| is smallest."""
    cands = [period / target_period, (period / 2) / target_period, (period * 2) / target_period]
    return min(cands, key=lambda f: abs(f - 1))


def beat_strength(a: TrackAnalysis, t: float) -> float:
    if a.bass_onset is None:
        return 0.0
    i = int(round(t / a.frame_sec))
    lo, hi = max(0, i - 2), min(len(a.bass_onset), i + 3)
    return float(a.bass_onset[lo:hi].max()) if hi > lo else 0.0


def _onset_slice(a: TrackAnalysis, start: float, dur: float) -> tuple[np.ndarray, float]:
    """Fine onset envelope of a slice of the source (times relative to ``start``)."""
    import librosa

    y, sr = librosa.load(str(a.path), sr=ANALYSIS_SR, mono=True, offset=max(0.0, start), duration=dur)
    # bass band only: kicks / bass notes define the groove; full-band onsets let snares and
    # hi-hats pull the alignment onto the off-beat
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=ALIGN_HOP, fmax=200, n_mels=32)
    lead = max(0.0, -start)  # slice clipped at 0 → pad so index 0 still means ``start``
    if lead:
        env = np.concatenate([np.zeros(int(round(lead * sr / ALIGN_HOP))), env])
    return env, ALIGN_HOP / sr


def fine_lag(a: TrackAnalysis, a_start: float, f_a: float, b: TrackAnalysis, b_start: float, f_b: float,
             overlap_out: float, max_lag_out: float, center: float = 0.0) -> float:
    """Output-time lag L (s) of B's onsets relative to A's over the overlap; B is late when L > 0.

    Both sides are compared in *output* time (after their tempo stretch), at ~6 ms resolution.
    """
    if a.path is None or b.path is None:
        return 0.0
    try:
        env_a, step = _onset_slice(a, a_start, overlap_out * f_a)
        margin = (max_lag_out + abs(center)) * f_b
        env_b, _ = _onset_slice(b, b_start - margin, overlap_out * f_b + 2 * margin)
    except Exception:  # noqa: BLE001 — alignment is a refinement; never fail the mix over it
        return 0.0
    t_out = np.arange(0, overlap_out, step)
    oa = np.interp(t_out * f_a, np.arange(len(env_a)) * step, env_a, right=0)
    tb_src = np.arange(len(env_b)) * step - margin  # source time relative to b_start
    best, best_score = center, -np.inf
    oa = oa - oa.mean()
    for k in range(-int(max_lag_out / step), int(max_lag_out / step) + 1):
        lag = center + k * step
        ob = np.interp((t_out + lag) * f_b, tb_src, env_b, left=0, right=0)
        score = float(np.dot(oa, ob - ob.mean()))
        if score > best_score:
            best, best_score = lag, score
    return best


def build_plan(items: list[tuple[str, TrackAnalysis]]) -> dict:
    """items: [(occurrence_id, analysis)] in mix order (≥ 2).

    Beat crossfades are beat-locked: the overlap starts on a beat of the outgoing segment and
    spans a whole number of its beats, the incoming segment is tempo-matched (pitch-preserving
    stretch, ≤ ±8 %), both edges sit on the strongest nearby bass hit, and a fine onset
    cross-correlation removes the remaining offset.
    """
    if len(items) < 2:
        raise ValueError("en az iki parça gerekli")
    kinds = [plan_transition(a, b) for (_, a), (_, b) in zip(items, items[1:])]
    seg_len = segment_length(len(items), sum(d for _, d in kinds))

    segs = []
    for oid, a in items:
        start, duration, _ = choose_segment(a, seg_len)
        segs.append({"id": oid, "a": a, "start": start, "end": start + duration, "f": 1.0})

    transitions = []
    for i, (kind, dur) in enumerate(kinds):
        A, B = segs[i], segs[i + 1]
        a, b = A["a"], B["a"]
        t = {"from": A["id"], "to": B["id"], "type": "safe_fade", "duration_sec": SAFE_FADE_SEC}
        locked = kind == "beat_crossfade" and _lock_beats(A, B, t)
        if not locked:
            t.update(type="safe_fade", duration_sec=SAFE_FADE_SEC)
        transitions.append(t)

    segments = []
    for s_ in segs:
        a = s_["a"]
        src_dur = s_["end"] - s_["start"]
        fr = a.frame_sec
        i0, i1 = int(s_["start"] / fr), min(len(a.rms), int(s_["end"] / fr))
        seg_db = 10 * np.log10(max(float(np.mean(a.rms[i0:i1] ** 2)), 1e-10))
        segments.append({
            "id": s_["id"],
            "start_sec": round(s_["start"], 4),
            "source_duration_sec": round(src_dur, 4),
            "stretch": round(s_["f"], 5),          # played this many times faster (1 = unchanged)
            "duration_sec": round(src_dur / s_["f"], 4),  # length in the mix
            "gain_db": round(float(np.clip(TARGET_RMS_DB - seg_db, -12, 9)), 2),
            "bpm": round(a.bpm * s_["f"], 1) if a.bpm else None,
            "beat_confidence": round(a.beat_confidence, 2),
        })
    for i, t in enumerate(transitions):
        if t["type"] == "safe_fade":  # beat crossfades were already sized to fit
            limit = 0.45 * min(segments[i]["duration_sec"], segments[i + 1]["duration_sec"])
            t["duration_sec"] = round(min(t["duration_sec"], limit), 4)
    total = sum(s["duration_sec"] for s in segments) - sum(t["duration_sec"] for t in transitions)
    return {"segments": segments, "transitions": transitions, "total_duration_sec": round(total, 2)}


def _lock_beats(A: dict, B: dict, t: dict) -> bool:
    """Make A→B a beat-locked crossfade in place (A's end, B's start and stretch). False → safe fade."""
    a, b = A["a"], B["a"]
    ba, bb = a.beat_times, b.beat_times
    if len(ba) < 12 or len(bb) < 12:
        return False
    pa = local_period(ba, A["end"])
    pb = local_period(bb, B["start"])
    if not pa or not pb:
        return False
    pa_out = pa / A["f"]                       # A's beat period as heard in the mix
    f_b = fold_ratio(pa_out, pb)
    if abs(f_b - 1) > MAX_STRETCH:
        return False

    # A ends exactly on one of its beats; the overlap is k of A's beats back from there.
    j0 = int(np.argmin(np.abs(ba - A["end"])))
    a_first = int(np.searchsorted(ba, A["start"] - 1e-6))
    best = None
    for k in XFADE_BEATS:
        if not XFADE_MIN_SEC <= k * pa_out <= XFADE_MAX_SEC:
            continue
        for j in range(j0 - STRONG_BEAT_SEARCH, j0 + STRONG_BEAT_SEARCH + 1):
            if j - k < a_first or j >= len(ba) or ba[j] > a.duration - 0.25:
                continue
            seg_out = (ba[j] - A["start"]) / A["f"]
            d_out = (ba[j] - ba[j - k]) / A["f"]
            if d_out > 0.45 * seg_out:
                continue
            score = beat_strength(a, ba[j - k])  # land the incoming song on a strong hit
            if best is None or score > best[0]:
                best = (score, j, k, d_out)
        if best:
            break
    if not best:
        return False
    _, j, k, d_out = best

    # B starts on its strongest beat near the planned start, then the fine alignment trims the rest.
    i0 = int(np.argmin(np.abs(bb - B["start"])))
    cands = [i for i in range(i0 - STRONG_BEAT_SEARCH, i0 + STRONG_BEAT_SEARCH + 1)
             if 0 <= i < len(bb) and bb[i] + d_out * f_b < b.duration - 1]
    if not cands:
        return False
    ib = max(cands, key=lambda i: beat_strength(b, bb[i]))
    b_start = float(bb[ib])

    # Tempo match measured over exactly the beats that will overlap (long overlaps expose any
    # tempo error as drift), then corrected from the drift between the overlap's two halves.
    a0 = float(ba[j - k])
    pa_ov = period_between(ba, a0, float(ba[j]))
    pb_ov = period_between(bb, b_start, b_start + d_out * f_b + pb)
    if pa_ov and pb_ov:
        pa_out = pa_ov / A["f"]
        f_b = fold_ratio(pa_out, pb_ov)
        if abs(f_b - 1) > MAX_STRETCH:
            return False
    lag = fine_lag(a, a0, A["f"], b, b_start, f_b, d_out, max_lag_out=0.25 * pa_out)
    half = d_out / 2
    lag1 = fine_lag(a, a0, A["f"], b, b_start, f_b, half, max_lag_out=0.12 * pa_out, center=lag)
    lag2 = fine_lag(a, a0 + half * A["f"], A["f"], b, b_start + half * f_b, f_b, half,
                    max_lag_out=0.12 * pa_out, center=lag)
    rate = (lag2 - lag1) / half                # B drifts later by `rate` s per output second
    if abs(rate) < 0.03 and abs(f_b * (1 + rate) - 1) <= MAX_STRETCH:
        f_b *= 1 + rate                        # B too slow → play it faster, and vice versa
        lag = fine_lag(a, a0, A["f"], b, b_start, f_b, d_out, max_lag_out=0.12 * pa_out, center=lag)
    b_start = max(0.0, b_start + lag * f_b)   # B late by `lag` → start it later in its source

    # keep B's planned length (in its own source time) after moving its start
    length = B["end"] - B["start"]
    A["end"] = float(ba[j])
    B["start"], B["end"], B["f"] = b_start, min(b.duration - 0.25, b_start + length), f_b
    t.update(type="beat_crossfade", duration_sec=round(d_out, 4), beats=k,
             tempo_change_pct=round((f_b - 1) * 100, 2), align_ms=round(lag * 1000, 1))
    return True
