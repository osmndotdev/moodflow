"""Five features (BPM, Key/Camelot, Mood, Energy, Genre): provider priority + fallback.

Order per field (plan v1.8 §6.1):
  BPM     FreqBlog → GetSongBPM → local preview beat tracking
  Key     FreqBlog → GetSongBPM                      (no unverified local key estimate)
  Mood    FreqBlog → coarse mood derived from FreqBlog valence+energy, else null
  Energy  FreqBlog → local loudness, min-max normalised within the playlist
  Genre   FreqBlog → Last.fm tags → MusicBrainz artist genres
Fallbacks are only called for fields that are still missing. Nothing is invented: a field
no source could fill stays null and is listed in ``missing_features``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from .schemas import Features, Track

if TYPE_CHECKING:
    from .feature_providers import ProviderResult

log = logging.getLogger(__name__)

FIVE = ("bpm", "key", "mood", "energy", "genre")

# --------------------------------------------------------------------------- text matching

VERSION_TAGS = ("live", "cover", "remix", "slowed", "reverb", "sped up", "nightcore", "acoustic",
                "instrumental", "karaoke", "8d", "edit", "mashup", "extended")
_FEAT_RE = re.compile(r"[\(\[]?\b(feat\.?|ft\.?|featuring|with)\b[^\)\]]*[\)\]]?", re.I)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = _FEAT_RE.sub(" ", text.lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    """0..1 similarity robust to word order and extra words."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(SequenceMatcher(None, na, nb).ratio(), overlap * 0.95)


def version_tags(text: str) -> set[str]:
    n = f" {normalize_text(text)} "
    return {t for t in VERSION_TAGS if f" {t} " in n}


def match_score(want_title: str, want_artist: str, want_duration: float | None,
                cand_title: str, cand_artist: str, cand_duration: float | None) -> float:
    """Score how likely a candidate (YouTube video / provider hit) is the wanted recording."""
    primary_artist = re.split(r",|&| x | and ", want_artist or "", maxsplit=1)[0]
    blob = f"{cand_artist} {cand_title}"
    title_sim = max(similarity(want_title, cand_title), similarity(want_title, blob) * 0.95)
    artist_sim = max(similarity(primary_artist, cand_artist), similarity(primary_artist, blob))
    score = 0.6 * title_sim + 0.4 * artist_sim
    unwanted = version_tags(cand_title) - version_tags(want_title)
    score -= 0.25 * len(unwanted)
    if want_duration and cand_duration:
        diff = abs(want_duration - cand_duration)
        if diff > 30:
            score -= min(0.4, (diff - 30) / 150)
    return max(0.0, min(1.0, score))


# --------------------------------------------------------------------------- music helpers

_PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_CAMELOT_MAJOR = [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1]   # by pitch class: C=8B, C#=3B, ...
_CAMELOT_MINOR = [5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10]   # Cm=5A, C#m=12A, ...


def camelot_for(pitch_class: int, is_major: bool) -> str:
    table = _CAMELOT_MAJOR if is_major else _CAMELOT_MINOR
    return f"{table[pitch_class % 12]}{'B' if is_major else 'A'}"


def camelot_from_open_key(open_key: str | None) -> str | None:
    """Open Key '1d' (C major) → '8B', '1m' (A minor) → '8A'."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*([dmDM])\s*", open_key or "")
    if not m or not 1 <= int(m.group(1)) <= 12:
        return None
    return f"{(int(m.group(1)) + 6) % 12 + 1}{'B' if m.group(2).lower() == 'd' else 'A'}"


def camelot_from_key_name(name: str | None) -> str | None:
    """'C♯m', 'Am', 'F-Minor', 'Eb major' → Camelot code."""
    if not name:
        return None
    s = name.strip().replace("♯", "#").replace("♭", "b")
    m = re.fullmatch(r"([A-Ga-g])\s*([#b]?)\s*[- ]?\s*(m|min|minor|maj|major)?", s, re.I)
    if not m:
        return None
    pc = _PITCH[m.group(1).upper()] + (1 if m.group(2) == "#" else -1 if m.group(2) == "b" else 0)
    quality = (m.group(3) or "").lower()
    return camelot_for(pc, not quality.startswith("m") or quality.startswith("maj"))


def fold_bpm(bpm: float) -> float:
    while bpm and bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    return round(bpm, 1)


def mood_labels(valence: float, energy: float) -> list[str]:
    """Coarse mood from *provider* valence + energy (FreqBlog scale: energy median ~0.65, valence ~0.48)."""
    if energy >= 0.65:
        return ["energetic"] if valence >= 0.5 else ["tense"]
    return ["happy"] if valence >= 0.5 else ["melancholic"]


# --------------------------------------------------------------------------- orchestration

class Provider(Protocol):
    name: str
    disabled_reason: str | None

    @property
    def enabled(self) -> bool: ...

    def lookup(self, title: str, artist: str, duration: float | None) -> "ProviderResult | None": ...


class FeatureProvider(Protocol):
    def enrich(self, tracks: list[Track]) -> list[str]:
        """Fill ``track.features`` in place, return user-facing warnings."""


# which fields each provider may fill
PROVIDES = {
    "freqblog": {"bpm", "bpm_alt", "bpm_confidence", "key", "camelot", "key_confidence",
                 "mood", "valence", "energy", "genre"},
    "getsongbpm": {"bpm", "key", "camelot"},
    "lastfm": {"genre"},
    "musicbrainz": {"genre"},
    "local_preview": {"bpm", "loudness_db"},
}
# field group → the Features attribute that decides whether it's still missing
GROUP_OF = {"bpm": "bpm", "bpm_alt": "bpm", "bpm_confidence": "bpm", "key": "camelot", "camelot": "camelot",
            "key_confidence": "camelot", "mood": "mood", "valence": "mood", "energy": "energy",
            "genre": "genre", "loudness_db": "energy"}
SOURCE_KEY = {"bpm": "bpm", "camelot": "key", "mood": "mood", "energy": "energy", "genre": "genre"}


class FeatureService:
    def __init__(self, providers: list[Provider], cache_file: Path | None = None, workers: int = 4):
        self.providers = providers
        self.workers = workers
        self.cache_file = cache_file
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        if cache_file and cache_file.is_file():
            try:
                self._cache = json.loads(cache_file.read_text())
            except (OSError, json.JSONDecodeError):
                self._cache = {}

    def status(self) -> dict[str, str | None]:
        return {p.name: p.disabled_reason for p in self.providers}

    # -- public
    def enrich(self, tracks: list[Track]) -> list[str]:
        warnings: list[str] = []
        for p in self.providers:
            if hasattr(p, "begin_batch"):
                p.begin_batch()
        with ThreadPoolExecutor(self.workers) as pool:
            results = list(pool.map(self._collect, tracks))
        loudness: dict[int, float] = {}
        for i, (track, (data, sources, problems)) in enumerate(zip(tracks, results)):
            if "energy" not in data and data.get("loudness_db") is not None:
                loudness[i] = data["loudness_db"]
            track.features = self._to_features(data, sources)
            for p in problems:
                warnings.append(f"{track.artist} – {track.title}: {p}")
        self._apply_relative_energy(tracks, loudness)
        for t in tracks:
            t.features.missing_features = [f for f in FIVE if self._missing(t.features, f)]
        disabled = [f"{p.name}: {p.disabled_reason}" for p in self.providers if p.disabled_reason]
        if disabled:
            warnings.insert(0, "Atlanan kaynaklar → " + "; ".join(disabled))
        self._save_cache()
        return warnings

    # -- internals
    @staticmethod
    def cache_key(track: Track) -> str:
        versions = ",".join(sorted(version_tags(track.title)))
        return f"{normalize_text(track.artist)}|{normalize_text(track.title)}|{versions}"

    def _collect(self, track: Track) -> tuple[dict, dict, list[str]]:
        key = self.cache_key(track)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return dict(cached["data"]), dict(cached["sources"]), []

        from .feature_providers import ProviderAuthError

        data: dict[str, Any] = {}
        sources: dict[str, str] = {}
        problems: list[str] = []
        filled: set[str] = set()  # field groups already provided by a higher-priority source
        for p in self.providers:
            if not p.enabled:
                continue
            wanted = {GROUP_OF[f] for f in PROVIDES.get(p.name, set())} - filled
            if not wanted:
                continue
            try:
                res = p.lookup(track.title, track.artist, track.duration_sec)
            except ProviderAuthError as err:
                problems.append(str(err))
                continue
            except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as err:
                log.warning("%s failed for %s - %s: %s", p.name, track.artist, track.title, err)
                problems.append(f"{p.name} yanıt vermedi")
                continue
            except Exception as err:  # noqa: BLE001 — e.g. undecodable preview audio
                log.warning("%s crashed for %s - %s: %s", p.name, track.artist, track.title, err)
                continue
            if res is None:
                continue
            newly: set[str] = set()
            for f, v in res.fields.items():
                g = GROUP_OF.get(f)
                if v is None or g not in wanted:
                    continue
                data[f] = v
                newly.add(g)
            for g in newly:
                if g in SOURCE_KEY:
                    sources[SOURCE_KEY[g]] = res.source
            filled |= newly
        # coarse mood only from provider valence + energy (both FreqBlog)
        if "mood" not in data and data.get("valence") is not None and sources.get("energy") == "freqblog":
            data["mood"] = mood_labels(data["valence"], data["energy"])
            sources["mood"] = "derived(freqblog valence+energy)"
        cut_short = "genre" not in data and any(getattr(p, "exhausted", False) for p in self.providers if p.enabled)
        if not cut_short:  # a lookup skipped for time budget should be retried on the next import
            with self._lock:
                self._cache[key] = {"data": data, "sources": sources}
        return data, sources, problems

    @staticmethod
    def _to_features(data: dict, sources: dict) -> Features:
        allowed = set(Features.model_fields) - {"sources", "missing_features"}
        return Features(**{k: v for k, v in data.items() if k in allowed}, sources=sources)

    @staticmethod
    def _apply_relative_energy(tracks: list[Track], loudness: dict[int, float]) -> None:
        """Local energy = loudness min-max scaled within this playlist (not comparable to FreqBlog's scale)."""
        if len(loudness) < 2:
            return
        lo, hi = min(loudness.values()), max(loudness.values())
        span = max(hi - lo, 3.0)  # < 3 dB spread → don't exaggerate tiny differences
        for i, db in loudness.items():
            tracks[i].features.energy = round(0.15 + 0.7 * (db - lo) / span, 2)
            tracks[i].features.sources["energy"] = "local_rms(playlist-relative)"

    @staticmethod
    def _missing(f: Features, name: str) -> bool:
        return {"bpm": f.bpm, "key": f.camelot, "mood": f.mood, "energy": f.energy, "genre": f.genre}[name] in (None, [])

    def _save_cache(self) -> None:
        if not self.cache_file:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self.cache_file.write_text(json.dumps(self._cache))
        except OSError:
            pass


def build_feature_service(cache_file: Path | None = None) -> FeatureService:
    """Providers from environment variables (server-side developer keys)."""
    from .feature_providers import (
        FreqBlogProvider, GetSongBPMProvider, LastFmProvider, LocalPreviewAnalyzer, MusicBrainzGenreProvider,
    )

    flag = lambda name, default="1": os.getenv(name, default).strip().lower() not in ("0", "false", "no", "")  # noqa: E731
    return FeatureService([
        FreqBlogProvider(os.getenv("FREQBLOG_API_KEY")),
        GetSongBPMProvider(os.getenv("GETSONGBPM_API_KEY")),
        LastFmProvider(os.getenv("LASTFM_API_KEY")),
        # local preview before MusicBrainz: no overlapping fields, and it keeps workers busy while
        # MusicBrainz calls wait on their 1 req/s lock
        LocalPreviewAnalyzer(enabled=flag("MOODFLOW_LOCAL_PREVIEW")),
        MusicBrainzGenreProvider(enabled=flag("MOODFLOW_MUSICBRAINZ"),
                                 budget_sec=float(os.getenv("MOODFLOW_MUSICBRAINZ_BUDGET", 15))),
    ], cache_file=cache_file, workers=6)
