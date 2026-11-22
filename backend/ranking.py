"""Four ranking modes on top of one optimizer.

Transition cost: C(i, j) = Σ w_f · d_f(i, j), each d_f in 0..1. Features missing on either
side drop out and the remaining weights are re-normalised. Emotional mode adds a
calm → peak → calm target curve over positions.
"""
from __future__ import annotations

import math
import random

from .schemas import Features, Mode, TargetCurve, Track, Weights

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


CURVE_FEATURES = ("bpm", "energy", "valence")  # numeric, ordered features a user curve can target


def interp_curve(points: list[float], pos: int, n: int) -> float:
    """Linear interpolation of evenly spaced control points at order position ``pos`` (0..n-1)."""
    if len(points) == 1 or n <= 1:
        return points[0]
    x = pos / (n - 1) * (len(points) - 1)
    i = min(int(x), len(points) - 2)
    return points[i] + (points[i + 1] - points[i]) * (x - i)


class Objective:
    """Mean transition cost, optionally blended with a curve term.

    Curve term = mean |track value − target(position)|, both scaled to 0..1 by the playlist's
    range of that value. Emotional mode uses a built-in calm → peak → calm intensity arc; a
    user ``target`` curve (Custom mode) targets BPM, energy or valence directly.
    """

    def __init__(self, tracks: list[Track], mode: Mode, weights: Weights | None,
                 target: TargetCurve | None = None):
        self.n = len(tracks)
        self.mode = mode
        w = resolve_weights(mode, weights)
        feats = [t.features for t in tracks]
        self.cost = [[transition_cost(feats[i], feats[j], w) if i != j else 0.0
                      for j in range(self.n)] for i in range(self.n)]
        self.curve_values: list[float | None] = [None] * self.n
        self.curve_targets: list[float] = []
        self.curve_share = 0.0
        if target is not None and target.strength > 0 and len(target.points) >= 2:
            raw = [getattr(f, target.feature) for f in feats]
            known = [v for v in raw if v is not None]
            if len(known) >= 2 and max(known) - min(known) > 1e-6:
                lo, hi = min(known), max(known)
                scale = lambda v: min(1.0, max(0.0, (v - lo) / (hi - lo)))  # noqa: E731
                self.curve_values = [None if v is None else scale(v) for v in raw]
                self.curve_targets = [scale(interp_curve(target.points, p, self.n)) for p in range(self.n)]
                self.curve_share = min(1.0, target.strength)
        elif mode == "emotional":
            self.curve_values = _normalise([_intensity(f) for f in feats])
            self.curve_targets = [target_curve(p, self.n) for p in range(self.n)]
            self.curve_share = CURVE_SHARE
        self.intensity = self.curve_values  # used for greedy start picks

    @property
    def has_curve(self) -> bool:
        return self.curve_share > 0 and any(v is not None for v in self.curve_values)

    def curve_dev(self, track: int, pos: int) -> float | None:
        v = self.curve_values[track]
        return None if v is None or not self.curve_targets else abs(v - self.curve_targets[pos])

    def curve_error(self, order: list[int]) -> float | None:
        devs = [d for p, t in enumerate(order) if (d := self.curve_dev(t, p)) is not None]
        return sum(devs) / len(devs) if devs else None

    def step_cost(self, cur: int, nxt: int, pos: int) -> float:
        """Greedy helper: cost of placing ``nxt`` at ``pos`` right after ``cur``."""
        c = self.cost[cur][nxt]
        dev = self.curve_dev(nxt, pos) if self.has_curve else None
        return c if dev is None else (1 - self.curve_share) * c + self.curve_share * dev

    def score(self, order: list[int]) -> float:
        if len(order) < 2:
            return 0.0
        trans = sum(self.cost[a][b] for a, b in zip(order, order[1:])) / (len(order) - 1)
        err = self.curve_error(order) if self.has_curve else None
        return trans if err is None else (1 - self.curve_share) * trans + self.curve_share * err

    def curve_matching_order(self) -> list[int] | None:
        """Sorted-to-sorted assignment of tracks to positions: the exact minimum of the curve
        term alone (L1). Tracks without the value are treated as the median for placement."""
        if not self.has_curve:
            return None
        known = sorted(v for v in self.curve_values if v is not None)
        median = known[len(known) // 2]
        by_value = sorted(range(self.n), key=lambda t: (self.curve_values[t] if self.curve_values[t] is not None
                                                        else median, t))
        by_target = sorted(range(self.n), key=lambda p: (self.curve_targets[p], p))
        order = [0] * self.n
        for track, pos in zip(by_value, by_target):
            order[pos] = track
        return order


# --------------------------------------------------------------------------- optimizer

def _greedy(start: int, obj: Objective) -> list[int]:
    order, left = [start], set(range(obj.n)) - {start}
    while left:
        cur = order[-1]
        nxt = min(left, key=lambda j: obj.step_cost(cur, j, len(order)))
        order.append(nxt)
        left.remove(nxt)
    return order


def _local_search(order: list[int], obj: Objective, max_rounds: int = 50, lo: int = 0) -> list[int]:
    """Swap/relocate improvement; positions before ``lo`` are fixed (a pinned first song)."""
    best, best_score = order[:], obj.score(order)
    n = len(order)
    for _ in range(max_rounds):
        improved = False
        for i in range(lo, n):
            for j in range(lo, n):
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


def _curve_info(obj: Objective, ids: list[str], mode: Mode, target: TargetCurve | None) -> dict | None:
    """The curve the optimizer actually used, for charting: per-position targets and per-track
    values on the same playlist-relative 0..1 scale (0 = playlist minimum, 1 = maximum)."""
    if not obj.has_curve:
        return None
    feature = target.feature if (mode == "custom" and target is not None) else "intensity"
    return {
        "feature": feature,
        "share": round(obj.curve_share, 3),
        "targets": [round(t, 4) for t in obj.curve_targets],
        "values": {oid: None if v is None else round(v, 4) for oid, v in zip(ids, obj.curve_values)},
    }


def transition_breakdown(order: list[Track], weights: dict[str, float]) -> list[dict]:
    """Per-step distances for charting: each feature's d_f (0..1, None if not comparable) + weighted total."""
    out = []
    for a, b in zip(order, order[1:]):
        step = {f: (None if (d := DISTANCES[f](a.features, b.features)) is None else round(d, 3)) for f in FEATURES}
        step["total"] = round(transition_cost(a.features, b.features, weights), 3)
        out.append(step)
    return out


def rank(tracks: list[Track], mode: Mode = "daily", weights: Weights | None = None,
         target: TargetCurve | None = None, first_id: str | None = None) -> dict:
    """``first_id`` pins that occurrence to position 1; only the rest is reordered."""
    n = len(tracks)
    ids = [t.occurrence_id for t in tracks]
    if first_id is not None and first_id not in ids:
        raise ValueError(f"İlk şarkı olarak seçilen öğe listede yok: {first_id}")
    pin = ids.index(first_id) if first_id is not None else None
    front = (lambda order: order if pin is None else [pin] + [i for i in order if i != pin])  # noqa: E731
    if n < 3:
        order = front(list(range(n)))
        w = resolve_weights(mode, weights)
        steps = transition_breakdown([tracks[i] for i in order], w)
        return {"ordered_ids": [ids[i] for i in order], "score_before": 0.0, "score_after": 0.0,
                "kept_original": order == list(range(n)), "first_id": first_id,
                "transition_costs": [s["total"] for s in steps],
                "transition_breakdown": {"before": transition_breakdown(tracks, w), "after": steps}}
    obj = Objective(tracks, mode, weights, target if mode == "custom" else None)
    original = list(range(n))
    before = obj.score(original)
    # with a pin, the fallback is the original order with the pinned song moved to the front
    baseline = original if pin in (None, 0) else front(original)
    best, best_score = baseline, obj.score(baseline)
    lo = 0 if pin is None else 1
    candidates = [_greedy(pin, obj)] if pin is not None else []
    candidates += [front(_greedy(start, obj)) for start in _start_indices(obj, tracks) if start != pin]
    if (matched := obj.curve_matching_order()) is not None:
        candidates.insert(0, front(matched))
    for start_order in candidates:
        cand = _local_search(start_order, obj, lo=lo)
        s = obj.score(cand)
        if s < best_score - 1e-9:
            best, best_score = cand, s
    kept = best is original
    err_before, err_after = (obj.curve_error(original), obj.curve_error(best)) if obj.has_curve else (None, None)
    return {
        "ordered_ids": [ids[i] for i in best],
        "score_before": round(before, 4),
        "score_after": round(best_score, 4),
        "kept_original": kept,
        "first_id": first_id,
        # mean distance to the curve (0 = exact fit), for a user target or the emotional arc
        "curve_error_before": None if err_before is None else round(err_before, 4),
        "curve_error_after": None if err_after is None else round(err_after, 4),
        "curve": _curve_info(obj, ids, mode, target),
        "transition_costs": [round(obj.cost[a][b], 3) for a, b in zip(best, best[1:])],
        "transition_breakdown": {
            "before": transition_breakdown(tracks, resolve_weights(mode, weights)),
            "after": transition_breakdown([tracks[i] for i in best], resolve_weights(mode, weights)),
        },
    }
