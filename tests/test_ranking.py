import pytest
from collections import Counter

from backend.ranking import Objective, d_bpm, d_key, rank, resolve_weights, target_curve, transition_cost
from backend.schemas import Features, Track, Weights


def _t(i, **f):
    return Track(occurrence_id=f"item-{i:03d}", platform="youtube", source_id=f"v{i}", title=f"S{i}",
                 artist="A", original_position=i, features=Features(**f))


def test_every_mode_returns_permutation_and_keeps_duplicates(sample_tracks):
    ids = [t.occurrence_id for t in sample_tracks]
    for mode in ("daily", "dj", "emotional", "custom"):
        res = rank(sample_tracks, mode, Weights(bpm=1, key=1, mood=0, energy=0, genre=0))
        assert Counter(res["ordered_ids"]) == Counter(ids)
        assert res["score_after"] <= res["score_before"]
        assert len(res["transition_costs"]) == len(ids) - 1


def test_input_not_mutated(sample_tracks):
    before = [t.occurrence_id for t in sample_tracks]
    rank(sample_tracks, "dj")
    assert [t.occurrence_id for t in sample_tracks] == before


def test_keeps_original_when_already_optimal():
    tracks = [_t(i, bpm=100 + i) for i in range(6)]
    res = rank(tracks, "custom", Weights(bpm=1))
    assert res["score_after"] <= res["score_before"]
    assert res["ordered_ids"] in ([t.occurrence_id for t in tracks], [t.occurrence_id for t in reversed(tracks)])


def test_dj_mode_sorts_tempo_chaos():
    bpms = [128, 90, 126, 92, 124, 94, 122, 96]
    tracks = [_t(i, bpm=b) for i, b in enumerate(bpms)]
    res = rank(tracks, "custom", Weights(bpm=1))
    by_id = {t.occurrence_id: t.features.bpm for t in tracks}
    ordered = [by_id[i] for i in res["ordered_ids"]]
    jumps = sum(abs(a - b) > 10 for a, b in zip(ordered, ordered[1:]))
    assert jumps <= 1
    assert res["score_after"] < res["score_before"]


def test_missing_metadata_does_not_crash_and_is_neutral():
    a, b = Features(), Features(bpm=120)
    assert transition_cost(a, b, resolve_weights("daily", None)) == 0.5
    tracks = [_t(i) for i in range(5)]
    res = rank(tracks, "emotional")
    assert sorted(res["ordered_ids"]) == sorted(t.occurrence_id for t in tracks)


def test_missing_features_reweight():
    w = resolve_weights("dj", None)
    a, b = Features(bpm=120, camelot=None), Features(bpm=120, camelot="8A")
    assert transition_cost(a, b, w) == 0.0  # only bpm comparable, and equal


def test_distances():
    assert d_key(Features(camelot="8A"), Features(camelot="8A")) == 0
    assert d_key(Features(camelot="8A"), Features(camelot="9A")) == 0.2
    assert d_key(Features(camelot="12A"), Features(camelot="1A")) == 0.2  # wraps around the wheel
    assert d_key(Features(camelot="8A"), Features(camelot="2B")) == 1.0
    assert d_bpm(Features(bpm=120), Features(bpm=120)) == 0
    assert d_bpm(Features(bpm=70), Features(bpm=140)) < 0.3  # half-time mixes OK
    assert d_bpm(Features(bpm=100), Features(bpm=130)) == 1.0


def test_custom_weights_normalised():
    w = resolve_weights("custom", Weights(bpm=3, key=1))
    assert abs(sum(w.values()) - 1) < 1e-9 and w["bpm"] == 0.75
    assert resolve_weights("custom", Weights()) == {f: 0.2 for f in w}


def test_emotional_curve_peaks_in_middle():
    energies = [0.9, 0.2, 0.8, 0.3, 0.5, 0.7, 0.25]
    tracks = [_t(i, energy=e, valence=e) for i, e in enumerate(energies)]
    res = rank(tracks, "emotional")
    by_id = {t.occurrence_id: t.features.energy for t in tracks}
    ordered = [by_id[i] for i in res["ordered_ids"]]
    mid = ordered[len(ordered) // 2]
    assert mid >= max(ordered[0], ordered[-1])
    assert target_curve(3, 7) > target_curve(0, 7)


def test_objective_score_range(sample_tracks):
    obj = Objective(sample_tracks, "daily", None)
    s = obj.score(list(range(len(sample_tracks))))
    assert 0 <= s <= 1


def test_transition_breakdown_for_both_orders(sample_tracks):
    res = rank(sample_tracks, "dj")
    bd = res["transition_breakdown"]
    n = len(sample_tracks)
    assert len(bd["before"]) == len(bd["after"]) == n - 1
    assert [s["total"] for s in bd["after"]] == res["transition_costs"]
    for step in bd["before"] + bd["after"]:
        assert set(step) == {"bpm", "key", "mood", "energy", "genre", "total"}
        assert all(v is None or 0 <= v <= 1 for v in step.values())
    # the fixture's 9th track has no metadata → its transitions aren't comparable, not zero
    i = [t.occurrence_id for t in sample_tracks].index("item-009")
    assert bd["before"][i - 1]["bpm"] is None and bd["before"][i]["key"] is None


# ------------------------------------------------------------------ user target curve (Custom mode)

from backend.ranking import interp_curve  # noqa: E402
from backend.schemas import TargetCurve  # noqa: E402


def _bpms(res, tracks):
    by_id = {t.occurrence_id: t.features.bpm for t in tracks}
    return [by_id[i] for i in res["ordered_ids"]]


def test_descending_bpm_curve_sorts_high_to_low():
    tracks = [_t(i, bpm=b) for i, b in enumerate([100, 128, 90, 140, 115, 122, 96, 133])]
    res = rank(tracks, "custom", Weights(bpm=1), TargetCurve(feature="bpm", points=[140, 90], strength=1))
    assert _bpms(res, tracks) == sorted(_bpms(res, tracks), reverse=True)
    assert res["curve_error_after"] < res["curve_error_before"]


def test_peak_curve_puts_highest_energy_in_the_middle():
    tracks = [_t(i, energy=e) for i, e in enumerate([0.9, 0.2, 0.5, 0.7, 0.3, 0.8, 0.4])]
    res = rank(tracks, "custom", None, TargetCurve(feature="energy", points=[0.2, 0.9, 0.2], strength=1))
    by_id = {t.occurrence_id: t.features.energy for t in tracks}
    e = [by_id[i] for i in res["ordered_ids"]]
    assert e[3] == 0.9 and e[0] < e[3] and e[-1] < e[3]


def test_curve_strength_trades_fit_against_smooth_transitions():
    tracks = [_t(i, bpm=b, camelot=c) for i, (b, c) in enumerate(
        [(100, "8A"), (110, "3B"), (120, "8A"), (130, "3B"), (140, "8A"), (150, "3B")])]
    curve = dict(feature="bpm", points=[100, 150])
    loose = rank(tracks, "custom", Weights(key=1), TargetCurve(**curve, strength=0.05))
    strict = rank(tracks, "custom", Weights(key=1), TargetCurve(**curve, strength=1))
    assert strict["curve_error_after"] <= loose["curve_error_after"]
    assert _bpms(strict, tracks) == [100, 110, 120, 130, 140, 150]


def test_curve_ignored_outside_custom_mode_and_at_zero_strength():
    tracks = [_t(i, bpm=b) for i, b in enumerate([100, 128, 90, 140, 115])]
    curve = TargetCurve(feature="bpm", points=[140, 90], strength=1)
    assert rank(tracks, "daily", None, curve) == rank(tracks, "daily")
    zero = TargetCurve(feature="bpm", points=[140, 90], strength=0)
    assert rank(tracks, "custom", Weights(bpm=1), zero)["curve_error_after"] is None


def test_curve_with_missing_values_still_returns_every_track():
    tracks = [_t(i, bpm=b) for i, b in enumerate([100, None, 140, 120, None, 90])]
    res = rank(tracks, "custom", None, TargetCurve(feature="bpm", points=[90, 140], strength=1))
    assert sorted(res["ordered_ids"]) == sorted(t.occurrence_id for t in tracks)
    known = [b for b in _bpms(res, tracks) if b is not None]
    assert known == sorted(known)


def test_interp_curve():
    assert interp_curve([0, 10], 0, 5) == 0 and interp_curve([0, 10], 4, 5) == 10
    assert interp_curve([0, 10, 0], 2, 5) == 10 and interp_curve([0, 10, 0], 1, 5) == 5


def test_rank_api_accepts_target():
    from fastapi.testclient import TestClient

    from backend.main import app

    tracks = [_t(i, bpm=b).model_dump() for i, b in enumerate([100, 128, 90, 140])]
    body = {"tracks": tracks, "mode": "custom", "weights": {"bpm": 1},
            "target": {"feature": "bpm", "points": [140, 90], "strength": 1}}
    res = TestClient(app).post("/api/rank", json=body).json()
    assert res["ordered_ids"] == ["item-003", "item-001", "item-000", "item-002"]
    bad = TestClient(app).post("/api/rank", json={**body, "target": {"feature": "key", "points": [1, 2]}})
    assert bad.status_code == 422


def test_rank_returns_the_curve_it_used():
    tracks = [_t(i, energy=e, valence=e) for i, e in enumerate([0.9, 0.2, 0.5, 0.7, 0.3])]
    emo = rank(tracks, "emotional")["curve"]
    assert emo["feature"] == "intensity" and len(emo["targets"]) == 5
    assert emo["targets"][2] == max(emo["targets"])  # calm → peak → calm
    assert set(emo["values"]) == {t.occurrence_id for t in tracks}
    assert all(0 <= v <= 1 for v in emo["values"].values())
    assert rank(tracks, "daily")["curve"] is None and rank(tracks, "dj")["curve"] is None
    custom = rank(tracks, "custom", None, TargetCurve(feature="energy", points=[0.2, 0.9], strength=1))["curve"]
    assert custom["feature"] == "energy" and custom["targets"][0] == 0 and custom["targets"][-1] == 1


# ------------------------------------------------------------------ pinned first song

@pytest.mark.parametrize("mode", ["daily", "dj", "emotional", "custom"])
def test_pinned_song_is_always_first(sample_tracks, mode):
    ids = [t.occurrence_id for t in sample_tracks]
    for pinned in ("item-001", "item-005", "item-009", "item-010"):  # incl. a no-metadata track and a duplicate
        res = rank(sample_tracks, mode, Weights(bpm=1, key=1), None, pinned)
        assert res["ordered_ids"][0] == pinned and res["first_id"] == pinned
        assert sorted(res["ordered_ids"]) == sorted(ids)


def test_pin_with_curve_still_fits_the_rest():
    tracks = [_t(i, bpm=b) for i, b in enumerate([100, 128, 90, 140, 115, 122, 96, 133])]
    res = rank(tracks, "custom", Weights(bpm=1), TargetCurve(feature="bpm", points=[140, 90], strength=1),
               first_id="item-002")  # the 90 BPM song, against a falling curve
    bpms = _bpms(res, tracks)
    assert bpms[0] == 90 and bpms[1:] == sorted(bpms[1:], reverse=True)


def test_pin_matching_original_first_can_keep_original():
    tracks = [_t(i, bpm=100 + i) for i in range(6)]
    res = rank(tracks, "custom", Weights(bpm=1), None, "item-000")
    assert res["ordered_ids"] == [t.occurrence_id for t in tracks] and res["kept_original"]


def test_pin_small_playlist_and_unknown_id():
    tracks = [_t(i, bpm=100 + i) for i in range(2)]
    assert rank(tracks, "daily", None, None, "item-001")["ordered_ids"] == ["item-001", "item-000"]
    with pytest.raises(ValueError):
        rank(tracks, "daily", None, None, "nope")
