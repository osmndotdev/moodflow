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
