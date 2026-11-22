import httpx
import pytest

from backend.feature_providers import (
    FreqBlogProvider, GetSongBPMProvider, LastFmProvider, ProviderAuthError, ProviderResult, filter_genre_tags,
)
from backend.features import FeatureService, camelot_from_key_name, camelot_from_open_key
from backend.schemas import Track

BLINDING = {  # example response from the FreqBlog OpenAPI docs
    "track_name": "Blinding Lights", "artist_name": "The Weeknd", "duration_ms": 201570,
    "bpm": 85.39, "bpm_alt": 170.78, "bpm_confidence": 3.61, "key": "F-Minor", "key_confidence": 0.91,
    "camelot": "4A", "energy": 1.0, "valence": 0.42, "mood": "tense", "genre": "synthwave",
    "feature_source": "essentia_preview",
}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _track(title="Blinding Lights", artist="The Weeknd", duration=200.0, i=1):
    return Track(occurrence_id=f"item-{i:03d}", platform="spotify", source_id="x" * 22, title=title,
                 artist=artist, duration_sec=duration, original_position=i)


# ------------------------------------------------------------------ FreqBlog

def test_freqblog_normalises_five_features():
    seen = {}

    def handler(req):
        seen["key"] = req.headers.get("X-Api-Key")
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=BLINDING)

    res = FreqBlogProvider("k123", client=_client(handler)).lookup("Blinding Lights", "The Weeknd", 200)
    assert seen == {"key": "k123", "params": {"track": "Blinding Lights", "artist": "The Weeknd"}}
    f = res.fields
    assert (f["bpm"], f["bpm_alt"], f["camelot"], f["key"]) == (85.4, 170.8, "4A", "F-Minor")
    assert f["mood"] == ["tense"] and f["genre"] == ["synthwave"]
    assert f["energy"] == 1.0 and f["valence"] == 0.42
    assert f["bpm_confidence"] == 3.61 and f["key_confidence"] == 0.91  # kept apart, different scales


def test_freqblog_retries_202_then_succeeds():
    calls, sleeps = [], []

    def handler(req):
        calls.append(1)
        return httpx.Response(202 if len(calls) < 3 else 200, json=BLINDING)

    p = FreqBlogProvider("k", client=_client(handler), sleep=sleeps.append)
    assert p.lookup("Blinding Lights", "The Weeknd", None) is not None
    assert len(calls) == 3 and sleeps == [2.0, 2.0]


def test_freqblog_gives_up_on_persistent_202():
    p = FreqBlogProvider("k", client=_client(lambda r: httpx.Response(202)), retries_202=2, sleep=lambda s: None)
    assert p.lookup("Unknown", "Nobody", None) is None


def test_freqblog_respects_retry_after_on_429():
    calls, sleeps = [], []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json=BLINDING)

    p = FreqBlogProvider("k", client=_client(handler), max_retry_after=5, sleep=sleeps.append)
    assert p.lookup("Blinding Lights", "The Weeknd", None) is not None
    assert sleeps == [5]  # capped so a demo request never hangs a minute


def test_freqblog_rejected_key_disables_provider():
    p = FreqBlogProvider("bad", client=_client(lambda r: httpx.Response(401, json={"error_code": "KEY_MISSING"})))
    with pytest.raises(ProviderAuthError):
        p.lookup("x", "y", None)
    assert not p.enabled


def test_freqblog_without_key_is_skipped():
    p = FreqBlogProvider(None, client=_client(lambda r: pytest.fail("must not call network")))
    assert not p.enabled and p.lookup("x", "y", None) is None


@pytest.mark.parametrize("override", [
    {"artist_name": "Some Cover Band", "track_name": "Totally Different"},
    {"duration_ms": 260000},                           # 60 s longer than the playlist item
    {"track_name": "Blinding Lights (Live)"},           # wrong version
])
def test_freqblog_rejects_mismatched_hits(override):
    body = {**BLINDING, **override}
    p = FreqBlogProvider("k", client=_client(lambda r: httpx.Response(200, json=body)))
    assert p.lookup("Blinding Lights", "The Weeknd", 200) is None


# ------------------------------------------------------------------ GetSongBPM / Last.fm / keys

def test_getsongbpm_search_then_song():
    def handler(req):
        if req.url.path == "/search/":
            assert req.headers["X-API-KEY"] == "g"
            return httpx.Response(200, json={"search": [{"id": "abc", "title": "Take On Me", "artist": {"name": "a-ha"}}]})
        return httpx.Response(200, json={"song": {"id": "abc", "tempo": "169", "key_of": "A", "open_key": "4d"}})

    res = GetSongBPMProvider("g", client=_client(handler)).lookup("Take On Me", "a-ha", None)
    assert res.fields == {"bpm": 169.0, "camelot": "11B", "key": "A"}


def test_getsongbpm_no_result():
    p = GetSongBPMProvider("g", client=_client(lambda r: httpx.Response(200, json={"search": {"error": "no result"}})))
    assert p.lookup("x", "y", None) is None


def test_lastfm_filters_non_genre_tags():
    tags = [{"name": "seen live", "count": 100}, {"name": "synthpop", "count": 90}, {"name": "80s", "count": 80},
            {"name": "new wave", "count": 70}, {"name": "female vocalists", "count": 60}, {"name": "pop", "count": 50},
            {"name": "rock", "count": 3}]
    assert filter_genre_tags(tags) == ["synthpop", "new wave", "pop"]
    body = {"toptags": {"tag": tags}}
    res = LastFmProvider("l", client=_client(lambda r: httpx.Response(200, json=body))).lookup("Take On Me", "a-ha", None)
    assert res.fields["genre"] == ["synthpop", "new wave", "pop"]


@pytest.mark.parametrize("open_key,camelot", [("1d", "8B"), ("1m", "8A"), ("6d", "1B"), ("12m", "7A"), ("x", None)])
def test_open_key_to_camelot(open_key, camelot):
    assert camelot_from_open_key(open_key) == camelot


@pytest.mark.parametrize("name,camelot", [("Am", "8A"), ("C", "8B"), ("F-Minor", "4A"), ("C♯m", "12A"),
                                          ("Eb major", "5B"), ("H", None)])
def test_key_name_to_camelot(name, camelot):
    assert camelot_from_key_name(name) == camelot


# ------------------------------------------------------------------ priority chain

class Fake:
    def __init__(self, name, fields=None, exc=None, enabled=True):
        self.name, self.fields, self.exc = name, fields, exc
        self.disabled_reason = None if enabled else f"{name} key yok"
        self.calls = 0

    @property
    def enabled(self):
        return self.disabled_reason is None

    def lookup(self, title, artist, duration):
        self.calls += 1
        if self.exc:
            raise self.exc
        f = self.fields(title) if callable(self.fields) else self.fields
        return ProviderResult(self.name, dict(f)) if f is not None else None


def test_fallbacks_only_fill_missing_fields():
    fb = Fake("freqblog", {"bpm": 120.0, "camelot": "8A", "key": "A-Minor", "energy": 0.7, "valence": 0.3})
    gs = Fake("getsongbpm", {"bpm": 99.0, "camelot": "1B"})
    lf = Fake("lastfm", {"genre": ["synthpop"]})
    mb = Fake("musicbrainz", {"genre": ["rock"]})
    t = _track()
    FeatureService([fb, gs, lf, mb]).enrich([t])
    f = t.features
    assert (f.bpm, f.camelot, f.genre) == (120.0, "8A", ["synthpop"])
    assert gs.calls == 0, "bpm/key present → GetSongBPM must not be called"
    assert mb.calls == 0, "genre filled by Last.fm → MusicBrainz must not be called"
    assert f.mood == ["energetic"] or f.mood == ["tense"]  # derived from FreqBlog valence+energy
    assert f.sources == {"bpm": "freqblog", "key": "freqblog", "energy": "freqblog", "genre": "lastfm",
                         "mood": "derived(freqblog valence+energy)"}
    assert f.missing_features == []


def test_nothing_is_invented_when_all_sources_fail():
    providers = [Fake("freqblog", None), Fake("getsongbpm", exc=httpx.ConnectError("down")),
                 Fake("lastfm", None), Fake("local_preview", exc=RuntimeError("bad audio"))]
    t = _track()
    warnings = FeatureService(providers).enrich([t])
    f = t.features
    assert (f.bpm, f.camelot, f.mood, f.energy, f.genre) == (None, None, None, None, None)
    assert f.missing_features == ["bpm", "key", "mood", "energy", "genre"]
    assert any("getsongbpm" in w for w in warnings)


def test_local_preview_gives_relative_energy_but_no_mood_or_key():
    loud = {"Quiet": -20.0, "Mid": -14.0, "Loud": -8.0}
    lp = Fake("local_preview", lambda title: {"bpm": 100.0, "loudness_db": loud[title]})
    tracks = [_track(title=k, artist="A", i=i) for i, k in enumerate(loud, 1)]
    FeatureService([Fake("freqblog", None), lp]).enrich(tracks)
    energies = [t.features.energy for t in tracks]
    assert energies == sorted(energies) and energies[0] < energies[-1]
    for t in tracks:
        assert t.features.camelot is None and t.features.mood is None
        assert t.features.sources["energy"].startswith("local_rms")
        assert t.features.missing_features == ["key", "mood", "genre"]


def test_disabled_providers_reported_and_cached_results_reused(tmp_path):
    fb = Fake("freqblog", {"bpm": 120.0})
    off = Fake("lastfm", enabled=False)
    svc = FeatureService([fb, off], cache_file=tmp_path / "f.json")
    warnings = svc.enrich([_track()])
    assert any("lastfm key yok" in w for w in warnings) and off.calls == 0
    svc.enrich([_track()])  # mode switches / re-imports must not hit the API again
    assert fb.calls == 1
    assert FeatureService([fb], cache_file=tmp_path / "f.json").enrich([_track()]) is not None and fb.calls == 1


def test_auth_error_surfaces_as_warning():
    fb = Fake("freqblog", exc=ProviderAuthError("FreqBlog API anahtarı reddedildi"))
    t = _track()
    warnings = FeatureService([fb]).enrich([t])
    assert any("reddedildi" in w for w in warnings)


# ------------------------------------------------------------------ ReccoBeats

from backend.feature_providers import ReccoBeatsProvider  # noqa: E402

TAKE_ON_ME_FEATS = {"id": "db14", "href": "https://open.spotify.com/track/2WfaOiMkCvy7F5fcp2zZ8L",
                    "tempo": 84.412, "key": 6, "mode": 0, "energy": 0.9, "valence": 0.88}


def _spotify(source_id, title="Take On Me", artist="a-ha", duration=225.0, i=1):
    t = _track(title=title, artist=artist, duration=duration, i=i)
    t.source_id = source_id
    return t


def test_reccobeats_batches_spotify_ids_once():
    calls = []

    def handler(req):
        calls.append((req.url.path, dict(req.url.params)))
        assert req.url.path == "/v1/audio-features"
        return httpx.Response(200, json={"content": [TAKE_ON_ME_FEATS]})

    p = ReccoBeatsProvider(client=_client(handler))
    known, unknown = _spotify("2WfaOiMkCvy7F5fcp2zZ8L"), _spotify("4HCOHRCSQelz2GPX8iTamO", i=2)
    p.begin_batch([known, unknown])
    assert calls == [("/v1/audio-features", {"ids": "2WfaOiMkCvy7F5fcp2zZ8L,4HCOHRCSQelz2GPX8iTamO"})]
    res = p.lookup_track(known)
    assert res.fields == {"bpm": 84.4, "camelot": "11A", "key": "F#-Minor"}  # half-time kept; ranking treats 2x as compatible
    assert len(calls) == 1  # served from the batch, no per-track request


def test_reccobeats_title_search_for_youtube_items():
    def handler(req):
        if req.url.path == "/v1/track/search":
            assert req.url.params["searchText"] == "Take On Me"
            return httpx.Response(200, json={"content": [
                {"id": "cover", "trackTitle": "Take On Me", "artists": [{"name": "Some Cover Band"}], "durationMs": 225000},
                {"id": "live1", "trackTitle": "Take On Me - Live", "artists": [{"name": "a-ha"}], "durationMs": 240000},
                {"id": "db14", "trackTitle": "Take on Me", "artists": [{"name": "a-ha"}], "durationMs": 225280},
            ]})
        assert req.url.path == "/v1/track/db14/audio-features"
        return httpx.Response(200, json=TAKE_ON_ME_FEATS)

    t = _track(title="Take On Me (Official Video)", artist="a-ha", duration=244)
    t.platform = "youtube"
    res = ReccoBeatsProvider(client=_client(handler)).lookup_track(t)
    assert res.fields["camelot"] == "11A" and res.fields["bpm"] == 84.4


def test_reccobeats_rejects_wrong_artist_and_unknown_key():
    search = {"content": [{"id": "x", "trackTitle": "Take On Me", "artists": [{"name": "Someone Else"}], "durationMs": 225000}]}
    p = ReccoBeatsProvider(client=_client(lambda r: httpx.Response(200, json=search)))
    assert p.lookup("Take On Me", "a-ha", 225) is None
    assert ReccoBeatsProvider._normalise({"tempo": 0, "key": -1, "mode": 1}) is None


def test_reccobeats_ranks_after_freqblog():
    fb = Fake("freqblog", {"bpm": 85.4, "camelot": "4A"})
    rb = Fake("reccobeats", {"bpm": 120.0, "camelot": "8A"})
    t = _track()
    FeatureService([fb, rb]).enrich([t])
    assert (t.features.bpm, t.features.camelot) == (85.4, "4A") and rb.calls == 0
    fb2, rb2 = Fake("freqblog", None), Fake("reccobeats", {"bpm": 120.0, "camelot": "8A"})
    t2 = _track()
    FeatureService([fb2, rb2]).enrich([t2])
    assert t2.features.sources["bpm"] == "reccobeats" and t2.features.sources["key"] == "reccobeats"


def test_youtube_durations_are_not_used_for_identity_checks():
    seen = []

    class Rec(Fake):
        def lookup(self, title, artist, duration):
            seen.append(duration)
            return None

    yt = _track(duration=291)
    yt.platform = "youtube"
    sp = _track(title="Save Your Tears", duration=251, i=2)
    FeatureService([Rec("freqblog")]).enrich([yt, sp])
    assert sorted(seen, key=lambda d: d or 0) == [None, 251]
