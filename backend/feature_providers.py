"""External music-feature adapters (server-side developer keys only, never user auth).

Priority (see ``features.py``):
  1. FreqBlog ``/lookup``            → bpm, bpm_alt, key, camelot, mood, valence, energy, genre
  2. GetSongBPM ``/search`` + ``/song`` → missing bpm / key
  3. Last.fm ``track.getTopTags``    → missing genre
  4. MusicBrainz artist genres        → missing genre (keyless, 1 req/s)
  5. Local preview analysis           → missing bpm, raw loudness for a playlist-relative energy

Every adapter returns ``None`` instead of raising for "not found"; an adapter that is
mis-configured (no key / rejected key) reports ``enabled == False`` and is skipped.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from .features import camelot_from_key_name, camelot_from_open_key, fold_bpm, match_score, version_tags

log = logging.getLogger(__name__)

USER_AGENT = "MoodFlowDemo/0.1 (hackathon demo; local)"
DURATION_TOLERANCE_SEC = float(os.getenv("MOODFLOW_DURATION_TOLERANCE", 20))
MIN_MATCH = 0.6


@dataclass
class ProviderResult:
    source: str
    fields: dict[str, Any] = field(default_factory=dict)


class ProviderAuthError(Exception):
    pass


def _accept_match(title: str, artist: str, duration: float | None,
                  m_title: str | None, m_artist: str | None, m_duration: float | None) -> bool:
    """Reject a provider hit whose artist/title/version or length doesn't fit the playlist item."""
    if not m_title:
        return True  # provider didn't echo identity; trust the query
    if duration and m_duration and abs(duration - m_duration) > DURATION_TOLERANCE_SEC:
        return False
    if version_tags(title) != version_tags(m_title):  # live / remix / cover … must agree
        return False
    return match_score(title, artist, None, m_title, m_artist or "", None) >= MIN_MATCH


# --------------------------------------------------------------------------- FreqBlog

class FreqBlogProvider:
    name = "freqblog"
    BASE = "https://api.freqblog.com"

    def __init__(self, api_key: str | None, client: httpx.Client | None = None,
                 retries_202: int = 3, wait_202: float = 2.0, max_retry_after: float = 10.0,
                 concurrency: int = 2, sleep=time.sleep):
        self.api_key = api_key or None
        self.client = client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
        self.retries_202 = retries_202
        self.wait_202 = wait_202
        self.max_retry_after = max_retry_after
        self._sem = threading.Semaphore(concurrency)  # API returns 429 on too many concurrent lookups
        self._sleep = sleep
        self.disabled_reason: str | None = None if self.api_key else "FREQBLOG_API_KEY tanımlı değil"

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    def lookup(self, title: str, artist: str, duration: float | None) -> ProviderResult | None:
        if not self.enabled:
            return None
        params = {"track": title, "artist": artist}
        attempts_202 = attempts_429 = 0
        while True:
            with self._sem:
                res = self.client.get(f"{self.BASE}/lookup", params=params,
                                      headers={"X-Api-Key": self.api_key})
            if res.status_code == 202:
                attempts_202 += 1
                if attempts_202 > self.retries_202:
                    return None  # still ingesting; leave fields to the fallbacks
                self._sleep(self.wait_202)
                continue
            if res.status_code == 429:
                attempts_429 += 1
                if attempts_429 > 2:
                    return None
                try:
                    wait = float(res.headers.get("Retry-After", 2))
                except ValueError:
                    wait = 2.0
                self._sleep(min(wait, self.max_retry_after))
                continue
            if res.status_code == 404:
                return None
            if res.status_code in (401, 403):
                self.disabled_reason = "FreqBlog API anahtarı reddedildi"
                raise ProviderAuthError(self.disabled_reason)
            res.raise_for_status()
            return self._normalise(res.json(), title, artist, duration)

    @staticmethod
    def _normalise(x: dict, title: str, artist: str, duration: float | None) -> ProviderResult | None:
        m_dur = x.get("duration_ms") / 1000 if x.get("duration_ms") else None
        if not _accept_match(title, artist, duration, x.get("track_name"), x.get("artist_name"), m_dur):
            log.info("freqblog mismatch: %s - %s -> %s - %s", artist, title, x.get("artist_name"), x.get("track_name"))
            return None
        f: dict[str, Any] = {}
        if x.get("bpm"):
            f["bpm"] = round(float(x["bpm"]), 1)
            if x.get("bpm_alt"):
                f["bpm_alt"] = round(float(x["bpm_alt"]), 1)
            if x.get("bpm_confidence") is not None:
                f["bpm_confidence"] = float(x["bpm_confidence"])  # NOT a 0..1 scale
        if x.get("camelot"):
            f["camelot"] = str(x["camelot"]).upper()
            if x.get("key"):
                f["key"] = x["key"]
            if x.get("key_confidence") is not None:
                f["key_confidence"] = float(x["key_confidence"])  # 0..1
        if x.get("energy") is not None:
            f["energy"] = float(np.clip(x["energy"], 0, 1))
        if x.get("valence") is not None:
            f["valence"] = float(np.clip(x["valence"], 0, 1))
        if x.get("mood"):
            mood = x["mood"]
            f["mood"] = [mood] if isinstance(mood, str) else list(mood)
        if x.get("genre"):
            g = x["genre"]
            f["genre"] = [g] if isinstance(g, str) else list(g)
        return ProviderResult("freqblog", f)


# --------------------------------------------------------------------------- GetSongBPM

class GetSongBPMProvider:
    name = "getsongbpm"
    BASE = "https://api.getsong.co"

    def __init__(self, api_key: str | None, client: httpx.Client | None = None):
        self.api_key = api_key or None
        self.client = client or httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT})
        self.disabled_reason: str | None = None if self.api_key else "GETSONGBPM_API_KEY tanımlı değil"

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    def _get(self, path: str, params: dict) -> dict:
        res = self.client.get(f"{self.BASE}{path}", params=params, headers={"X-API-KEY": self.api_key})
        if res.status_code in (401, 403):
            self.disabled_reason = "GetSongBPM API anahtarı reddedildi"
            raise ProviderAuthError(self.disabled_reason)
        res.raise_for_status()
        return res.json()

    def lookup(self, title: str, artist: str, duration: float | None) -> ProviderResult | None:
        if not self.enabled:
            return None
        data = self._get("/search/", {"type": "both", "lookup": f"song:{title} artist:{artist}"})
        hits = data.get("search")
        if not isinstance(hits, list):
            return None  # {"search": {"error": "no result"}}
        best, best_score = None, 0.0
        for h in hits:
            s = match_score(title, artist, None, h.get("title", ""), (h.get("artist") or {}).get("name", ""), None)
            if s > best_score:
                best, best_score = h, s
        if not best or best_score < MIN_MATCH:
            return None
        song = best
        if not (song.get("tempo") and (song.get("open_key") or song.get("key_of"))) and best.get("id"):
            song = self._get("/song/", {"id": best["id"]}).get("song") or best
        f: dict[str, Any] = {}
        try:
            if song.get("tempo"):
                f["bpm"] = fold_bpm(float(song["tempo"]))
        except (TypeError, ValueError):
            pass
        camelot = camelot_from_open_key(song.get("open_key")) or camelot_from_key_name(song.get("key_of"))
        if camelot:
            f["camelot"] = camelot
            if song.get("key_of"):
                f["key"] = song["key_of"]
        return ProviderResult("getsongbpm", f) if f else None


# --------------------------------------------------------------------------- Last.fm

NON_GENRE_TAGS = {
    "seen live", "favorite", "favorites", "favourite", "favourites", "love", "loved", "awesome", "beautiful",
    "my favorite", "spotify", "youtube", "albums i own", "under 2000 listeners", "male vocalists",
    "female vocalists", "female vocalist", "male vocalist", "female vocals", "male vocals", "singer-songwriter", "catchy", "cool", "sexy",
    "classic", "oldies", "best", "amazing", "good", "fun", "chill", "happy", "sad", "party", "summer",
    "american", "british", "uk", "usa", "english", "swedish", "norwegian", "german", "french", "turkish",
    "italian", "spanish", "canadian", "australian", "irish", "scottish", "dutch", "belgian", "japanese",
    "korean", "classic pop and rock", "producer", "composer", "multiple ipi",
}


def filter_genre_tags(tags: list[dict], limit: int = 3, min_count: int = 10) -> list[str] | None:
    out = []
    for t in tags:
        name = (t.get("name") or "").strip().lower()
        try:
            count = int(t.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        if not name or count < min_count or name in NON_GENRE_TAGS:
            continue
        if re.fullmatch(r"\d{2,4}s?", name) or len(name) > 24:  # decades / sentences
            continue
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out or None


class LastFmProvider:
    name = "lastfm"
    BASE = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self, api_key: str | None, client: httpx.Client | None = None):
        self.api_key = api_key or None
        self.client = client or httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT})
        self.disabled_reason: str | None = None if self.api_key else "LASTFM_API_KEY tanımlı değil"

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    def lookup(self, title: str, artist: str, duration: float | None) -> ProviderResult | None:
        if not self.enabled:
            return None
        res = self.client.get(self.BASE, params={
            "method": "track.getTopTags", "artist": artist, "track": title,
            "api_key": self.api_key, "format": "json", "autocorrect": 1,
        })
        data = res.json() if res.content else {}
        if data.get("error") in (10, 26):  # invalid / suspended key
            self.disabled_reason = "Last.fm API anahtarı reddedildi"
            raise ProviderAuthError(self.disabled_reason)
        tags = (data.get("toptags") or {}).get("tag") or []
        if isinstance(tags, dict):
            tags = [tags]
        genres = filter_genre_tags(tags)
        return ProviderResult("lastfm", {"genre": genres}) if genres else None


# --------------------------------------------------------------------------- MusicBrainz

class MusicBrainzGenreProvider:
    """Artist-level genre tags from MusicBrainz (keyless).

    One artist search per distinct artist (cached). MusicBrainz allows ~1 request/second and
    answers 503 when busy, so calls are serialised with back-off, and each import gets a time
    budget: once it's spent, remaining tracks simply keep ``genre = null``.
    """

    name = "musicbrainz"
    BASE = "https://musicbrainz.org/ws/2"

    def __init__(self, enabled: bool = True, client: httpx.Client | None = None,
                 min_interval: float = 1.1, budget_sec: float = 15.0):
        self.client = client or httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT})
        self.disabled_reason: str | None = None if enabled else "MOODFLOW_MUSICBRAINZ=0"
        self.min_interval = min_interval
        self.budget_sec = budget_sec
        self._deadline = float("inf")
        self._lock = threading.Lock()
        self._last = 0.0
        self._artist_cache: dict[str, list[str] | None] = {}

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    def begin_batch(self) -> None:
        self._deadline = time.monotonic() + self.budget_sec

    @property
    def exhausted(self) -> bool:
        """True once this import's time budget is spent (results after that are incomplete)."""
        return time.monotonic() > self._deadline

    def _get(self, path: str, params: dict, attempts: int = 4) -> dict | None:
        for attempt in range(attempts):
            with self._lock:
                if time.monotonic() > self._deadline:
                    return None
                wait = self.min_interval * (1 + attempt) - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                try:
                    res = self.client.get(f"{self.BASE}{path}", params={**params, "fmt": "json"})
                finally:
                    self._last = time.monotonic()
            if res.status_code != 503:  # 503 = rate limited → back off and retry
                res.raise_for_status()
                return res.json()
        return None

    def lookup(self, title: str, artist: str, duration: float | None) -> ProviderResult | None:
        if not self.enabled:
            return None
        primary = re.split(r",|&| x | feat\.? ", artist, maxsplit=1)[0].strip()
        primary = re.sub(r"\s*[\(\[].*?[\)\]]", "", primary).strip()
        key = primary.lower()
        if key not in self._artist_cache:
            data = self._get("/artist/", {"query": f'artist:"{primary.replace(chr(34), "")}"', "limit": 1})
            if data is None:
                return None  # budget spent / still rate limited: don't cache, a later import may succeed
            genres = None
            for art in data.get("artists") or []:
                if art.get("score", 0) >= 90 and match_score(primary, primary, None, art.get("name", ""),
                                                             art.get("name", ""), None) >= 0.8:
                    tags = sorted(art.get("tags") or [], key=lambda t: -int(t.get("count", 0)))
                    genres = filter_genre_tags(tags, min_count=1)
            self._artist_cache[key] = genres
        genres = self._artist_cache[key]
        return ProviderResult("musicbrainz", {"genre": genres}) if genres else None


# --------------------------------------------------------------------------- local preview analysis

class LocalPreviewAnalyzer:
    """Last resort: analyse a 30 s public preview locally (Deezer search, keyless).

    Yields BPM (librosa beat tracking) and *raw* loudness. Energy is derived from loudness
    relative to the playlist by ``features.py``; key, mood and valence are never derived
    locally (not reliable enough for the demo).
    """

    name = "local_preview"
    DEEZER = "https://api.deezer.com"

    def __init__(self, enabled: bool = True, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self.disabled_reason: str | None = None if enabled else "MOODFLOW_LOCAL_PREVIEW=0"

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    def lookup(self, title: str, artist: str, duration: float | None) -> ProviderResult | None:
        if not self.enabled:
            return None
        clean_title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip() or title
        primary = re.sub(r"\s*[\(\[].*?[\)\]]", "", re.split(r",|&", artist)[0]).strip()
        res = self.client.get(f"{self.DEEZER}/search", params={"q": f"{primary} {clean_title}", "limit": 5})
        res.raise_for_status()
        best, best_score = None, 0.0
        for c in res.json().get("data", []):
            s = match_score(title, artist, duration, c.get("title", ""), (c.get("artist") or {}).get("name", ""),
                            c.get("duration"))
            if s > best_score:
                best, best_score = c, s
        if not best or best_score < 0.62 or not best.get("preview"):
            return None
        audio = self.client.get(best["preview"])
        audio.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".mp3") as tmp:
            tmp.write(audio.content)
            tmp.flush()
            return ProviderResult("local_preview", analyze_clip(Path(tmp.name)))


def analyze_clip(path: Path) -> dict:
    import librosa

    y, sr = librosa.load(str(path), sr=22050, mono=True, duration=35)
    if y.size < sr * 5 or float(np.max(np.abs(y))) < 1e-4:
        raise ValueError("clip too short or silent")
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    rms_db = 20 * np.log10(np.maximum(librosa.feature.rms(y=y)[0], 1e-6))
    return {"bpm": fold_bpm(float(np.atleast_1d(tempo)[0])), "loudness_db": round(float(np.mean(rms_db)), 2)}
