"""Audio acquisition through the yt-dlp Python API (no shell, no uploads).

* YouTube playlist items already carry a ``video_id``.
* Spotify items are matched to a YouTube video via ``ytsearch3:`` + scoring;
  an unreliable match is never downloaded.
"""
from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .features import match_score
from .schemas import Track

log = logging.getLogger(__name__)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MATCH_THRESHOLD = 0.65
MAX_DURATION_SEC = 12 * 60
MAX_FILESIZE = 80 * 1024 * 1024

_download_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


class AudioSourceError(Exception):
    pass


def validate_video_id(video_id: str) -> str:
    if not video_id or not VIDEO_ID_RE.match(video_id):
        raise AudioSourceError(f"Geçersiz YouTube video kimliği: {video_id!r}")
    return video_id


# --------------------------------------------------------------------------- matching

def search_candidates(artist: str, title: str, n: int = 3, ydl_cls=YoutubeDL) -> list[dict]:
    query = f"ytsearch{n}:{artist} - {title} audio"
    opts = {"extract_flat": "in_playlist", "skip_download": True, "quiet": True,
            "no_warnings": True, "socket_timeout": 15, "extractor_retries": 1}
    try:
        with ydl_cls(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except DownloadError as err:
        log.warning("youtube search failed for %s - %s: %s", artist, title, err)
        return []
    return [e for e in (info or {}).get("entries") or [] if e and e.get("id")]


def score_candidate(track: Track, cand: dict) -> float:
    return match_score(
        track.title, track.artist, track.duration_sec,
        cand.get("title") or "", cand.get("channel") or cand.get("uploader") or "", cand.get("duration"),
    )


def find_youtube_match(track: Track, ydl_cls=YoutubeDL) -> tuple[str | None, float]:
    """Return (video_id, score) for the best reliable candidate, or (None, best_score)."""
    if track.youtube_video_id:
        return track.youtube_video_id, 1.0
    best_id, best = None, 0.0
    for cand in search_candidates(track.artist, track.title, ydl_cls=ydl_cls):
        if cand.get("duration") and cand["duration"] > MAX_DURATION_SEC:
            continue
        s = score_candidate(track, cand)
        if s > best:
            best_id, best = cand["id"], s
    if best_id and best >= MATCH_THRESHOLD and VIDEO_ID_RE.match(best_id):
        return best_id, best
    return None, best


# --------------------------------------------------------------------------- download

def _duration_filter(info: dict, *, incomplete: bool) -> str | None:
    dur = info.get("duration")
    if dur and dur > MAX_DURATION_SEC:
        return f"too long ({dur}s)"
    if info.get("is_live"):
        return "live stream"
    return None


def fetch_audio(video_id: str, destination: Path, ydl_cls=YoutubeDL) -> Path:
    """Download one YouTube video's audio as 44.1 kHz stereo WAV, cached by video id."""
    validate_video_id(video_id)
    destination.mkdir(parents=True, exist_ok=True)
    wav = destination / f"{video_id}.wav"
    with _locks_guard:
        lock = _download_locks[video_id]
    with lock:  # the same video requested twice downloads once
        if wav.is_file() and wav.stat().st_size > 0:
            return wav
        options = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": str(destination / f"{video_id}.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
            "postprocessor_args": {"extractaudio": ["-ar", "44100", "-ac", "2"]},
            "match_filter": _duration_filter,
            "max_filesize": MAX_FILESIZE,
            "socket_timeout": 20,
            "retries": 2,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        try:
            with ydl_cls(options) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        except DownloadError as err:
            raise AudioSourceError(_friendly(err)) from err
        if not wav.is_file():
            raise AudioSourceError("Ses alınamadı (video filtrelendi veya dönüştürülemedi).")
        return wav


def _friendly(err: Exception) -> str:
    msg = str(err).lower()
    if "sign in" in msg or "age" in msg:
        return "Video giriş/yaş doğrulaması istiyor; atlandı."
    if "unavailable" in msg or "private" in msg:
        return "Video erişilemiyor; atlandı."
    if "copyright" in msg or "blocked" in msg:
        return "Video bu bölgede engelli; atlandı."
    return "İndirme başarısız; atlandı."
