"""Five features (BPM, Key/Camelot, Mood, Energy, Genre): provider priority + fallback.

Order per field (plan v1.8 §6.1):
  BPM     FreqBlog → ReccoBeats → GetSongBPM → (mix only) beat tracking on the downloaded audio
  Key     FreqBlog → ReccoBeats → GetSongBPM         (no unverified local key estimate)
  Mood    FreqBlog → coarse mood derived from FreqBlog valence+energy, else null
  Energy  FreqBlog → (mix only) downloaded-audio loudness, min-max scaled among the mix tracks
  Genre   FreqBlog → Last.fm tags → MusicBrainz artist genres
Fallbacks are only called for fields that are still missing. Nothing is invented: a field
no source could fill stays null and is listed in ``missing_features``. Each feature also
carries ``sources[f]`` and ``status[f]``; confidences are kept only where the provider gives
them (``bpm_confidence`` on FreqBlog's own scale, ``key_confidence`` 0..1 — never merged).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
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

def match_duration(track: Track) -> float | None:
    """Duration usable for identity checks. Spotify / Apple Music durations are recording lengths;
    YouTube lengths are video lengths (intros, outros, skits) and often differ from the recording
    by 30-60 s, so they aren't compared."""
    return track.duration_sec if track.platform in ("spotify", "apple") else None


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
    "reccobeats": {"bpm", "key", "camelot"},
    "getsongbpm": {"bpm", "key", "camelot"},
    "lastfm": {"genre"},
    "musicbrainz": {"genre"},
}
# field group → the Features attribute that decides whether it's still missing
GROUP_OF = {"bpm": "bpm", "bpm_alt": "bpm", "bpm_confidence": "bpm", "key": "camelot", "camelot": "camelot",
            "key_confidence": "camelot", "mood": "mood", "valence": "mood", "energy": "energy",
            "genre": "genre"}
SOURCE_KEY = {"bpm": "bpm", "camelot": "key", "mood": "mood", "energy": "energy", "genre": "genre"}
FEATURE_OF_GROUP = SOURCE_KEY  # field group → one of the FIVE feature names

# Per-feature status (plan v1.8 §6.5.4):
#   ok        value from an external provider
#   derived   computed from other provider values (coarse mood from FreqBlog valence + energy)
#   local     measured locally from the downloaded recording (mix pipeline only)
#   missing   no source had it
#   error     still missing because a provider that could fill it failed (retried next import)


def feature_value(f: Features, name: str):
    return {"bpm": f.bpm, "key": f.camelot, "mood": f.mood, "energy": f.energy, "genre": f.genre}[name]


def compute_status(f: Features, errored: set[str] | None = None) -> None:
    """Fill ``f.status`` and ``f.missing_features`` from values + sources."""
    errored = errored or set()
    f.status = {}
    for name in FIVE:
        src = f.sources.get(name, "")
        if feature_value(f, name) in (None, []):
            f.status[name] = "error" if name in errored else "missing"
        elif src.startswith("derived"):
            f.status[name] = "derived"
        elif src.startswith("local"):
            f.status[name] = "local"
        else:
            f.status[name] = "ok"
    f.missing_features = [n for n in FIVE if f.status[n] in ("missing", "error")]


def fill_from_downloaded_audio(items: list[tuple[Track, float | None, float | None]]) -> dict[str, Features]:
    """Local fallback on the *downloaded* recordings (plan v1.8 §6.1 / §6.4).

    items: (track, measured_bpm, mean_loudness_db) for every downloaded track of a mix.
    Only fills BPM / energy that no provider supplied. Energy is the track's loudness
    min-max scaled among the downloaded tracks — playlist-relative, not on FreqBlog's scale.
    Returns occurrence_id → updated Features for the tracks that changed.
    """
    loud = [db for _, _, db in items if db is not None]
    lo, hi = (min(loud), max(loud)) if loud else (0.0, 0.0)
    span = max(hi - lo, 3.0)  # < 3 dB spread → don't exaggerate tiny differences
    updates: dict[str, Features] = {}
    for track, bpm, db in items:
        f = track.features.model_copy(deep=True)
        changed = False
        if f.bpm is None and bpm:
            f.bpm, f.sources["bpm"] = fold_bpm(bpm), "local_audio(downloaded)"
            changed = True
        if f.energy is None and db is not None and len(loud) >= 2:
            f.energy = round(0.15 + 0.7 * (db - lo) / span, 2)
            f.sources["energy"] = "local_rms(downloaded, playlist-relative)"
            changed = True
        if changed:
            compute_status(f)
            updates[track.occurrence_id] = f
    return updates


class ProviderCache:
    """Persistent cache of each provider's answer per track (JSON file, atomic writes).

    One entry per (provider, provider cache version, artist|title|version[, platform id]):
      * a hit with fields  → kept ``positive_ttl`` (features of a recording don't change)
      * "not found"/mismatch → kept ``negative_ttl``, then asked again (catalogues grow)
    "Try again later" outcomes (FreqBlog 202 still ingesting, rate limits, time budget,
    network errors, rejected keys) are never stored. Keys never contain API keys.
    """

    def __init__(self, path: Path | None, positive_ttl: float = 30 * 86400, negative_ttl: float = 7 * 86400,
                 clock=time.time):
        self.path = path
        self.positive_ttl = positive_ttl
        self.negative_ttl = negative_ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._dirty = False
        if path and path.is_file():
            try:
                raw = json.loads(path.read_text())
                self._data = {k: e for k, e in raw.items()
                              if isinstance(e, dict) and isinstance(e.get("t"), (int, float)) and "fields" in e}
            except (OSError, json.JSONDecodeError, AttributeError):
                self._data = {}

    @staticmethod
    def key(provider: Provider, track: Track) -> str:
        versions = ",".join(sorted(version_tags(track.title)))
        k = f"{provider.name}@{getattr(provider, 'cache_version', 1)}|" \
            f"{normalize_text(track.artist)}|{normalize_text(track.title)}|{versions}"
        if getattr(provider, "uses_platform_id", False):
            k += f"|{track.platform}:{track.source_id}"
        return k

    _MISS = object()

    def get(self, provider: Provider, track: Track):
        """Cached fields dict, ``None`` for a cached "not found", or ``ProviderCache._MISS``."""
        with self._lock:
            e = self._data.get(self.key(provider, track))
        if not e:
            return self._MISS
        ttl = self.positive_ttl if e["fields"] is not None else self.negative_ttl
        return e["fields"] if self._clock() - e["t"] <= ttl else self._MISS

    def has(self, provider: Provider, track: Track) -> bool:
        return self.get(provider, track) is not self._MISS

    def put(self, provider: Provider, track: Track, fields: dict | None) -> None:
        with self._lock:
            self._data[self.key(provider, track)] = {"t": self._clock(), "fields": fields}
            self._dirty = True

    def __len__(self) -> int:
        return len(self._data)

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            if not self._dirty:
                return
            now = self._clock()
            # drop expired entries so the file doesn't grow forever
            self._data = {k: e for k, e in self._data.items()
                          if now - e["t"] <= (self.positive_ttl if e["fields"] is not None else self.negative_ttl)}
            payload = json.dumps(self._data)
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(payload)
            os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written cache
        except OSError as err:
            log.warning("provider cache not saved: %s", err)


class FeatureService:
    def __init__(self, providers: list[Provider], cache_file: Path | None = None, workers: int = 4,
                 cache: ProviderCache | None = None):
        self.providers = providers
        self.workers = workers
        self.cache = cache if cache is not None else ProviderCache(cache_file)  # (empty cache is falsy)
        self._stats_lock = threading.Lock()
        self.last_usage: dict[str, dict[str, int]] = {}
        self.total_usage: dict[str, dict[str, int]] = {}

    def status(self) -> dict[str, str | None]:
        return {p.name: p.disabled_reason for p in self.providers}

    # -- public
    def enrich(self, tracks: list[Track]) -> list[str]:
        warnings: list[str] = []
        self.last_usage = {p.name: {"calls": 0, "cached": 0} for p in self.providers if p.enabled}
        for p in self.providers:
            if p.enabled and hasattr(p, "begin_batch"):
                # batch-prefetch only what this provider hasn't answered before
                p.begin_batch([t for t in tracks if not self.cache.has(p, t)])
        with ThreadPoolExecutor(self.workers) as pool:
            results = list(pool.map(self._collect, tracks))
        for track, (data, sources, problems, errored) in zip(tracks, results):
            track.features = self._to_features(data, sources)
            compute_status(track.features, errored)
            for p in problems:
                warnings.append(f"{track.artist} – {track.title}: {p}")
        disabled = [f"{p.name}: {p.disabled_reason}" for p in self.providers if p.disabled_reason]
        if disabled:
            warnings.insert(0, "Atlanan kaynaklar → " + "; ".join(disabled))
        self.cache.save()
        for name, u in self.last_usage.items():
            tot = self.total_usage.setdefault(name, {"calls": 0, "cached": 0})
            tot["calls"] += u["calls"]
            tot["cached"] += u["cached"]
        log.info("feature providers this import: %s", self.last_usage)
        return warnings

    # -- internals
    def _count(self, provider: Provider, kind: str) -> None:
        with self._stats_lock:
            self.last_usage.setdefault(provider.name, {"calls": 0, "cached": 0})[kind] += 1

    def _ask(self, p: Provider, track: Track):
        """Provider answer for this track, from cache when possible. Raises on retry-later/errors."""
        cached = self.cache.get(p, track)
        if cached is not ProviderCache._MISS:
            self._count(p, "cached")
            return cached
        self._count(p, "calls")
        if hasattr(p, "lookup_track"):  # providers that can use platform ids (Spotify id)
            res = p.lookup_track(track)
        else:
            res = p.lookup(track.title, track.artist, match_duration(track))
        fields = res.fields if res is not None else None
        self.cache.put(p, track, fields)  # only definitive answers get here
        return fields

    def _collect(self, track: Track) -> tuple[dict, dict, list[str], set[str]]:
        from .feature_providers import ProviderAuthError, ProviderRetryLater

        data: dict[str, Any] = {}
        sources: dict[str, str] = {}
        problems: list[str] = []
        errored: set[str] = set()  # FIVE-feature names a failing provider could have filled
        filled: set[str] = set()  # field groups already provided by a higher-priority source
        for p in self.providers:
            if not p.enabled:
                continue
            wanted = {GROUP_OF[f] for f in PROVIDES.get(p.name, set())} - filled
            if not wanted:
                continue
            try:
                fields = self._ask(p, track)
            except ProviderRetryLater as err:
                problems.append(f"{p.name}: {err} — sonraki okumada tekrar denenecek")
                errored |= {FEATURE_OF_GROUP[g] for g in wanted}
                continue
            except ProviderAuthError as err:
                problems.append(str(err))
                errored |= {FEATURE_OF_GROUP[g] for g in wanted}
                continue
            except Exception as err:  # noqa: BLE001 — network/parse failure: skip this provider
                log.warning("%s failed for %s - %s: %s", p.name, track.artist, track.title, err)
                problems.append(f"{p.name} yanıt vermedi")
                errored |= {FEATURE_OF_GROUP[g] for g in wanted}
                continue
            if not fields:
                continue
            newly: set[str] = set()
            for f, v in fields.items():
                g = GROUP_OF.get(f)
                if v is None or g not in wanted:
                    continue
                data[f] = v
                newly.add(g)
            for g in newly:
                if g in SOURCE_KEY:
                    sources[SOURCE_KEY[g]] = p.name
            filled |= newly
        # coarse mood only from provider valence + energy (both FreqBlog)
        if "mood" not in data and data.get("valence") is not None and sources.get("energy") == "freqblog":
            data["mood"] = mood_labels(data["valence"], data["energy"])
            sources["mood"] = "derived(freqblog valence+energy)"
        errored -= {FEATURE_OF_GROUP[g] for g in filled}  # a later provider filled it after all
        return data, sources, problems, errored

    @staticmethod
    def _to_features(data: dict, sources: dict) -> Features:
        allowed = set(Features.model_fields) - {"sources", "missing_features", "status"}
        return Features(**{k: v for k, v in data.items() if k in allowed}, sources=sources)


def build_feature_service(cache_file: Path | None = None) -> FeatureService:
    """Providers from environment variables (server-side developer keys)."""
    from .feature_providers import (
        FreqBlogProvider, GetSongBPMProvider, LastFmProvider, MusicBrainzGenreProvider, ReccoBeatsProvider,
    )

    flag = lambda name, default="1": os.getenv(name, default).strip().lower() not in ("0", "false", "no", "")  # noqa: E731
    return FeatureService([
        FreqBlogProvider(os.getenv("FREQBLOG_API_KEY")),
        ReccoBeatsProvider(enabled=flag("MOODFLOW_RECCOBEATS")),
        GetSongBPMProvider(os.getenv("GETSONGBPM_API_KEY")),
        LastFmProvider(os.getenv("LASTFM_API_KEY")),
        MusicBrainzGenreProvider(enabled=flag("MOODFLOW_MUSICBRAINZ"),
                                 budget_sec=float(os.getenv("MOODFLOW_MUSICBRAINZ_BUDGET", 15))),
    ], workers=6, cache=ProviderCache(
        cache_file,
        positive_ttl=float(os.getenv("MOODFLOW_PROVIDER_CACHE_DAYS", 30)) * 86400,
        negative_ttl=float(os.getenv("MOODFLOW_PROVIDER_CACHE_NEGATIVE_DAYS", 7)) * 86400,
    ))
