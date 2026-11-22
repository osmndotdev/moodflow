import time
from pathlib import Path

import numpy as np
import pytest

from backend import arrangement, audio_sources, main, mixing
from backend.schemas import Track


@pytest.fixture(scope="module")
def six_sources(tmp_path_factory):
    from tests.conftest import make_beat_wav
    d = tmp_path_factory.mktemp("src")
    specs = [(120, 220, 0.3), (121, 247, 0.9), (122, 262, 0.5), (90, 196, 1.0), (91, 294, 0.2), (124, 330, 0.7)]
    return {f"item-{i:03d}": make_beat_wav(d / f"s{i}.wav", bpm=b, freq=f, amplitude=a, intro_silence=12)
            for i, (b, f, a) in enumerate(specs, 1)}


@pytest.fixture(scope="module")
def analyses(six_sources):
    return [(k, arrangement.analyze_track(p)) for k, p in six_sources.items()]


def test_analysis_finds_tempo_and_beats(analyses):
    _, a = analyses[0]
    assert a.bpm == pytest.approx(120, rel=0.04)
    assert a.beat_confidence > 0.6


def test_segment_skips_silent_intro(analyses):
    for _, a in analyses:
        start, dur, _ = arrangement.choose_segment(a, 18)
        assert start >= 12 - 0.6  # never inside the 12 s silent intro
        assert 15 <= dur <= 20


def test_transition_types(analyses):
    a120, a121, a90 = analyses[0][1], analyses[1][1], analyses[3][1]
    kind, d = arrangement.plan_transition(a120, a121)
    assert kind == "beat_crossfade" and 3 <= d <= 6
    kind, d = arrangement.plan_transition(a121, a90)
    assert kind == "safe_fade" and 1 <= d <= 3


def test_plan_and_render_six_tracks(tmp_path, analyses, six_sources):
    plan = arrangement.build_plan(analyses)
    assert 60 <= plan["total_duration_sec"] <= 100
    assert len(plan["segments"]) == 6 and len(plan["transitions"]) == 5
    out = mixing.render_mix(plan, six_sources, tmp_path / "preview.mp3")
    assert mixing.probe_duration(out) == pytest.approx(plan["total_duration_sec"], abs=1.0)
    stats = mixing.audio_stats(out)
    assert stats["peak"] < 1.0, "limiter must prevent clipping"
    assert stats["longest_silence_sec"] < 1.0, "no unintended silence between segments"


def test_levels_are_balanced(analyses):
    plan = arrangement.build_plan(analyses)
    gains = {s["id"]: s["gain_db"] for s in plan["segments"]}
    assert gains["item-005"] > gains["item-004"]  # quiet source boosted relative to loud one


def test_corrupt_audio_rejected(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFF\x00\x00garbage" * 10)
    with pytest.raises(arrangement.AnalysisError):
        arrangement.analyze_track(bad)


def test_needs_two_sources(analyses, six_sources, tmp_path):
    with pytest.raises(ValueError):
        arrangement.build_plan(analyses[:1])
    with pytest.raises(mixing.RenderError):
        mixing.render_mix({"segments": [{"id": "item-001"}], "transitions": [], "total_duration_sec": 10},
                          six_sources, tmp_path / "x.mp3")


# ------------------------------------------------------------------ full job with fake downloads

def _tracks(n):
    return [Track(occurrence_id=f"item-{i:03d}", platform="youtube", source_id=f"vid{i:08d}", title=f"S{i}",
                  artist="A", original_position=i, youtube_video_id=f"vid{i:08d}", match_status="matched")
            for i in range(1, n + 1)]


def _run_job(monkeypatch, tmp_path, tracks, fake_fetch):
    monkeypatch.setattr(main, "MIX_DIR", tmp_path / "mixes")
    monkeypatch.setattr(main, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(audio_sources, "fetch_audio", fake_fetch)
    res = main.api_mix(main.MixRequest(tracks=tracks, ordered_ids=[t.occurrence_id for t in tracks]))
    for _ in range(300):
        job = main.api_mix_status(res.mix_id)
        if job.status != "processing":
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_job_replaces_failed_downloads(monkeypatch, tmp_path, six_sources):
    srcs = list(six_sources.values())
    bad = tmp_path / "corrupt.wav"
    bad.write_bytes(b"not audio")

    def fake_fetch(video_id, dest):
        idx = int(video_id[3:])
        if idx == 2:
            raise audio_sources.AudioSourceError("Video erişilemiyor; atlandı.")
        if idx == 4:
            return bad
        return srcs[idx % len(srcs)]

    job = _run_job(monkeypatch, tmp_path, _tracks(9), fake_fetch)
    assert job.status == "done", job.error
    stages = {t.occurrence_id: t.stage for t in job.tracks}
    assert stages["item-002"] == "failed" and stages["item-004"] == "failed"
    assert sum(s == "ready" for s in stages.values()) == 6
    assert [s["id"] for s in job.plan["segments"]] == ["item-001", "item-003", "item-005", "item-006", "item-007", "item-008"]
    assert any("item" not in w and "atlandı" in w for w in job.warnings)
    assert (tmp_path / "mixes" / job.mix_id / "preview.mp3").is_file()
    # tracks had no provider features → BPM / energy measured from the downloaded audio
    upd = job.feature_updates["item-001"]
    assert upd.bpm and upd.energy is not None and upd.status["bpm"] == "local"
    assert set(job.feature_updates) == {"item-001", "item-003", "item-005", "item-006", "item-007", "item-008"}


def test_job_fails_cleanly_with_fewer_than_two_sources(monkeypatch, tmp_path, six_sources):
    first = next(iter(six_sources.values()))

    def fake_fetch(video_id, dest):
        if video_id.endswith("1"):
            return first
        raise audio_sources.AudioSourceError("İndirme başarısız; atlandı.")

    job = _run_job(monkeypatch, tmp_path, _tracks(3), fake_fetch)
    assert job.status == "failed" and "iki" in job.error


# ------------------------------------------------------------------ beat-locked crossfades

def test_local_period_is_sub_frame_precise():
    period = 60 / 123.4
    beats = np.round(np.arange(40) * period / (512 / 22050)) * (512 / 22050) + 3.0  # quantised like librosa
    assert arrangement.local_period(beats, 10.0) == pytest.approx(period, abs=0.0015)


def test_fold_ratio_handles_half_time():
    assert arrangement.fold_ratio(0.5, 0.51) == pytest.approx(1.02)
    assert arrangement.fold_ratio(0.5, 1.02) == pytest.approx(1.02)  # B tracked at half speed


def test_beat_locked_plan_fields(tmp_path):
    from tests.conftest import make_beat_wav
    a = arrangement.analyze_track(make_beat_wav(tmp_path / "a.wav", bpm=120, seconds=70, freq=220))
    b = arrangement.analyze_track(make_beat_wav(tmp_path / "b.wav", bpm=124, seconds=70, freq=330))
    plan = arrangement.build_plan([("A", a), ("B", b)])
    t = plan["transitions"][0]
    assert t["type"] == "beat_crossfade" and t["beats"] in (8, 16, 4)
    assert t["tempo_change_pct"] == pytest.approx((120 / 124 - 1) * 100, abs=0.6)
    seg_a, seg_b = plan["segments"]
    assert seg_b["duration_sec"] == pytest.approx(seg_b["source_duration_sec"] / seg_b["stretch"], abs=1e-3)
    end_a = seg_a["start_sec"] + seg_a["source_duration_sec"]
    assert np.min(np.abs(a.beat_times - end_a)) < 1e-3  # A ends exactly on one of its beats
    assert abs(seg_b["stretch"] - 1) <= arrangement.MAX_STRETCH


def test_rendered_crossfade_has_no_doubled_hits(tmp_path):
    """End to end through FFmpeg: two kick patterns at 120 and 124 BPM with a random phase
    offset must play as one pattern during the overlap (no flams / doubled hits)."""
    import librosa

    from tests.conftest import make_beat_wav
    pa = make_beat_wav(tmp_path / "a.wav", bpm=120, seconds=70, freq=220)
    pb = make_beat_wav(tmp_path / "b.wav", bpm=124, seconds=70, freq=330)
    # shift B's beat phase so a naive cut would land between beats
    y, sr = sf_read(pb)
    sf_write(pb, y[int(0.17 * sr):], sr)
    a, b = arrangement.analyze_track(pa), arrangement.analyze_track(pb)
    plan = arrangement.build_plan([("A", a), ("B", b)])
    t = plan["transitions"][0]
    assert t["type"] == "beat_crossfade"
    out = mixing.render_mix(plan, {"A": pa, "B": pb}, tmp_path / "mix.mp3")
    y, sr = librosa.load(str(out), sr=22050)
    seg_a = plan["segments"][0]
    ov0 = seg_a["duration_sec"] - t["duration_sec"]
    clip = y[int((ov0 + 0.2) * sr): int((ov0 + t["duration_sec"] - 0.2) * sr)]
    on = librosa.onset.onset_detect(y=clip, sr=sr, hop_length=128, units="time", backtrack=False)
    ioi = np.diff(on)
    period = 0.5  # A's 120 BPM, which B is stretched to
    assert len(on) >= 4
    assert ioi.min() > 0.6 * period, f"doubled hits in the overlap: {np.round(ioi, 3)}"
    assert np.abs(ioi - period).max() < 0.03, f"uneven beats in the overlap: {np.round(ioi, 3)}"


def sf_read(path):
    import soundfile as sf
    return sf.read(path)


def sf_write(path, y, sr):
    import soundfile as sf
    sf.write(path, y, sr)
