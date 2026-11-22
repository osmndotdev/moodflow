"""Four ranking modes on top of one optimizer.

Transition cost: C(i, j) = Σ w_f · d_f(i, j), each d_f in 0..1. Features missing on either
side drop out and the remaining weights are re-normalised. Emotional mode adds a
calm → peak → calm target curve over positions.
"""
from __future__ import annotations

import math
import random

from .schemas import Features, Mode, Track, Weights

FEATURES = ("bpm", "key", "mood", "energy", "genre")

MODE_WEIGHTS: dict[str, dict[str, float]] = {
    "daily":     {"bpm": 20, "key": 10, "mood": 20, "energy": 25, "genre": 25},
    "dj":        {"bpm": 30, "key": 30, "mood": 10, "energy": 20, "genre": 10},
    "emotional": {"bpm": 15, "key": 5,  "mood": 30, "energy": 30, "genre": 20},
}
CURVE_SHARE = 0.5  # emotional mode: share of the score coming from the target curve


def resolve_weights(mode: Mode, weights: Weights | None) -> dict[str, float]:
    raw = MODE_WEIGHTS.get(mode) if mode != "custom" else (weights.model_dump() if weights else None)
    raw = raw or MODE_WEIGHTS["daily"]
    total = sum(max(0.0, v) for v in raw.values())
    if total <= 0:
        return {f: 1 / len(FEATURES) for f in FEATURES}
    return {f: max(0.0, raw.get(f, 0)) / total for f in FEATURES}


# --------------------------------------------------------------------------- distances

def _parse_camelot(code: str | None) -> tuple[int, str] | None:
    if not code or len(code) < 2 or code[-1].upper() not in "AB":
        return None
    try:
        n = int(code[:-1])
    except ValueError:
        return None
    return (n, code[-1].upper()) if 1 <= n <= 12 else None


def d_bpm(a: Features, b: Features) -> float | None:
    if not a.bpm or not b.bpm:
        return None
    hi, lo = max(a.bpm, b.bpm), min(a.bpm, b.bpm)
    ratio = hi / lo
    if hi > 1.6 * lo:  # half/double time mixes fine, with a small penalty
        ratio = min(ratio, max(hi, 2 * lo) / min(hi, 2 * lo) + 0.05)
    return min(1.0, (ratio - 1) / 0.25)  # 25 % apart → maximal distance


def d_key(a: Features, b: Features) -> float | None:
    ka, kb = _parse_camelot(a.camelot), _parse_camelot(b.camelot)
    if not ka or not kb:
        return None
    step = min((ka[0] - kb[0]) % 12, (kb[0] - ka[0]) % 12)
    same_letter = ka[1] == kb[1]
    if step == 0:
        return 0.0 if same_letter else 0.2
    if step == 1:
        return 0.2 if same_letter else 0.5
    if step == 2:
        return 0.6 if same_letter else 0.8
    return 1.0


def _jaccard(x: list[str] | None, y: list[str] | None) -> float | None:
    if not x or not y:
        return None
    sx, sy = {s.lower() for s in x}, {s.lower() for s in y}
    return len(sx & sy) / len(sx | sy)


def d_mood(a: Features, b: Features) -> float | None:
    parts = []
    if a.valence is not None and b.valence is not None:
        parts.append(abs(a.valence - b.valence))
    j = _jaccard(a.mood, b.mood)
    if j is not None:
        parts.append(1 - j)
    return sum(parts) / len(parts) if parts else None


def d_energy(a: Features, b: Features) -> float | None:
    if a.energy is None or b.energy is None:
        return None
    return abs(a.energy - b.energy)


def d_genre(a: Features, b: Features) -> float | None:
    j = _jaccard(a.genre, b.genre)
    return None if j is None else 1 - j


DISTANCES = {"bpm": d_bpm, "key": d_key, "mood": d_mood, "energy": d_energy, "genre": d_genre}


def transition_cost(a: Features, b: Features, weights: dict[str, float]) -> float:
    num = den = 0.0
    for f, w in weights.items():
        if w <= 0:
            continue
        d = DISTANCES[f](a, b)
        if d is not None:
            num += w * d
            den += w
    return num / den if den > 0 else 0.5  # nothing comparable → neutral


# --------------------------------------------------------------------------- objective

def target_curve(pos: int, n: int) -> float:
    """Emotional mode target energy: calm → peak → calm."""
    if n <= 1:
        return 0.5
    return 0.1 + 0.85 * math.sin(math.pi * pos / (n - 1))


def _intensity(f: Features) -> float | None:
    vals = [v for v in (f.energy, f.valence) if v is not None]
    if f.energy is not None and f.valence is not None:
        return 0.7 * f.energy + 0.3 * f.valence
    return vals[0] if vals else None


def _normalise(values: list[float | None]) -> list[float | None]:
    """Min-max scale within the playlist so the emotional arc is relative to what's available."""
    known = [v for v in values if v is not None]
    if len(known) < 2 or max(known) - min(known) < 0.05:
        return values
    lo, hi = min(known), max(known)
    return [None if v is None else (v - lo) / (hi - lo) for v in values]


class Objective:
    def __init__(self, tracks: list[Track], mode: Mode, weights: Weights | None):
        self.n = len(tracks)
        self.mode = mode
        w = resolve_weights(mode, weights)
        feats = [t.features for t in tracks]
        self.cost = [[transition_cost(feats[i], feats[j], w) if i != j else 0.0
                      for j in range(self.n)] for i in range(self.n)]
        self.intensity = _normalise([_intensity(f) for f in feats])

    def curve_dev(self, track: int, pos: int) -> float | None:
        if self.intensity[track] is None:
            return None
        return abs(self.intensity[track] - target_curve(pos, self.n))

    def step_cost(self, cur: int, nxt: int, pos: int) -> float:
        """Greedy helper: cost of placing ``nxt`` at ``pos`` right after ``cur``."""
        c = self.cost[cur][nxt]
        dev = self.curve_dev(nxt, pos) if self.mode == "emotional" else None
        return c if dev is None else (1 - CURVE_SHARE) * c + CURVE_SHARE * dev

    def score(self, order: list[int]) -> float:
        if len(order) < 2:
            return 0.0
        trans = sum(self.cost[a][b] for a, b in zip(order, order[1:])) / (len(order) - 1)
        if self.mode != "emotional":
            return trans
        devs = [d for p, t in enumerate(order) if (d := self.curve_dev(t, p)) is not None]
        if not devs:
            return trans
        return (1 - CURVE_SHARE) * trans + CURVE_SHARE * (sum(devs) / len(devs))


# --------------------------------------------------------------------------- optimizer

def _greedy(start: int, obj: Objective) -> list[int]:
    order, left = [start], set(range(obj.n)) - {start}
    while left:
        cur = order[-1]
        nxt = min(left, key=lambda j: obj.step_cost(cur, j, len(order)))
        order.append(nxt)
        left.remove(nxt)
    return order


def _local_search(order: list[int], obj: Objective, max_rounds: int = 50) -> list[int]:
    best, best_score = order[:], obj.score(order)
    n = len(order)
    for _ in range(max_rounds):
        improved = False
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                # swap
                if i < j:
                    cand = best[:]
                    cand[i], cand[j] = cand[j], cand[i]
                    s = obj.score(cand)
                    if s < best_score - 1e-9:
                        best, best_score, improved = cand, s, True
                        continue
                # relocate i → j
                cand = best[:]
                item = cand.pop(i)
                cand.insert(j, item)
                s = obj.score(cand)
                if s < best_score - 1e-9:
                    best, best_score, improved = cand, s, True
        if not improved:
            break
    return best


def _start_indices(obj: Objective, tracks: list[Track], k: int = 5) -> list[int]:
    starts = [0]
    known = [i for i in range(obj.n) if obj.intensity[i] is not None]
    if known:
        starts.append(min(known, key=lambda i: obj.intensity[i]))  # calmest
        starts.append(max(known, key=lambda i: obj.intensity[i]))  # most intense
    rng = random.Random(len(tracks))  # deterministic per playlist size
    pool = [i for i in range(obj.n) if i not in starts]
    rng.shuffle(pool)
    starts += pool
    seen: list[int] = []
    for s in starts:
        if s not in seen:
            seen.append(s)
    return seen[:k]


def rank(tracks: list[Track], mode: Mode = "daily", weights: Weights | None = None) -> dict:
    n = len(tracks)
    ids = [t.occurrence_id for t in tracks]
    if n < 3:
        return {"ordered_ids": ids, "score_before": 0.0, "score_after": 0.0,
                "kept_original": True, "transition_costs": []}
    obj = Objective(tracks, mode, weights)
    original = list(range(n))
    before = obj.score(original)
    best, best_score = original, before
    for start in _start_indices(obj, tracks):
        cand = _local_search(_greedy(start, obj), obj)
        s = obj.score(cand)
        if s < best_score - 1e-9:
            best, best_score = cand, s
    kept = best is original
    return {
        "ordered_ids": [ids[i] for i in best],
        "score_before": round(before, 4),
        "score_after": round(best_score, 4),
        "kept_original": kept,
        "transition_costs": [round(obj.cost[a][b], 3) for a, b in zip(best, best[1:])],
    }
