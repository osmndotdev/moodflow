"""Save a MoodFlow order as a playlist on the user's own Spotify or YouTube (Music) account.

* The user connects an account with OAuth (authorization code flow, client secret kept server-side).
  The browser opens the provider's consent page in a popup and polls ``state`` until the callback
  has stored the tokens; it then holds only an opaque ``connection_id`` (no tokens reach the page).
* Tokens live in ``tmp/oauth/connections.json`` (mode 0600) so backend reloads don't disconnect
  anyone; access tokens are refreshed when they expire, a rejected refresh drops the connection.
* Spotify (Web API, Development Mode rules of Feb 2026): ``GET /me``, ``GET /search`` (limit ≤ 10),
  ``POST /me/playlists``, ``POST /playlists/{id}/items`` (≤ 100 URIs per call). Spotify-imported
  songs keep their track id; others are searched and scored with :func:`features.match_score`.
* YouTube Music has no public write API; playlists created with the YouTube Data API v3 show up in
  the YouTube Music library. Songs are matched with yt-dlp (no quota), while ``playlists.insert`` and
  each ``playlistItems.insert`` cost 50 of the project's 10,000 daily quota units.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import httpx

from . import audio_sources
from .features import match_duration, match_score
from .schemas import ExportItem, ExportStatus, Track

log = logging.getLogger(__name__)

PROVIDERS = ("spotify", "youtube")
PROVIDER_LABEL = {"spotify": "Spotify", "youtube": "YouTube Music"}
MATCH_THRESHOLD = 0.65
PENDING_TTL_SEC = 10 * 60
SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_PAREN_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")


class ExportError(Exception):
    """Shown to the user as is."""


class NotConnected(ExportError):
    """The connection is unknown, expired or revoked; the page should forget it and offer to reconnect."""


class QuotaExceeded(ExportError):
    pass


# --------------------------------------------------------------------------- configuration

@dataclass
class OAuthConfig:
    provider: str
    client_id: str
    client_secret: str
    redirect_uri: str
    auth_url: str
    token_url: str
    scopes: list[str]
    auth_params: dict = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def authorize_url(self, state: str) -> str:
        params = {
            "client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes), "state": state, **self.auth_params,
        }
        return f"{self.auth_url}?{urlencode(params)}"


def public_url() -> str:
    """Origin the providers redirect back to (must match the redirect URI registered with them).
    Spotify accepts only loopback IPs or HTTPS, never ``localhost``, hence 127.0.0.1."""
    url = os.getenv("MOODFLOW_PUBLIC_URL", "").strip().rstrip("/")
    return url or f"http://127.0.0.1:{os.getenv('MOODFLOW_PORT', '8000')}"


def load_configs(base_url: str | None = None) -> dict[str, OAuthConfig]:
    base = (base_url or public_url()).rstrip("/")
    return {
        "spotify": OAuthConfig(
            provider="spotify",
            client_id=os.getenv("SPOTIFY_CLIENT_ID", "").strip(),
            client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", "").strip(),
            redirect_uri=f"{base}/api/export/spotify/callback",
            auth_url="https://accounts.spotify.com/authorize",
            token_url="https://accounts.spotify.com/api/token",
            scopes=["playlist-modify-private", "playlist-modify-public"],
            auth_params={"show_dialog": "true"},  # lets the user pick another account
        ),
        "youtube": OAuthConfig(
            provider="youtube",
            client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip(),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
            redirect_uri=f"{base}/api/export/youtube/callback",
            auth_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/youtube"],
            # offline + consent: always get a refresh token, also on a reconnect
            auth_params={"access_type": "offline", "prompt": "consent select_account",
                         "include_granted_scopes": "true"},
        ),
    }


# --------------------------------------------------------------------------- token storage

class ConnectionStore:
    """connection_id → {provider, access_token, refresh_token, expires_at, account}; JSON file, 0600."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, dict] = {}
        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text())
            except (OSError, ValueError):
                log.warning("unreadable OAuth connection store %s; starting empty", path)

    def get(self, cid: str) -> dict | None:
        return self.data.get(cid)

    def put(self, cid: str, conn: dict) -> None:
        self.data[cid] = conn
        self.save()

    def delete(self, cid: str) -> None:
        if self.data.pop(cid, None) is not None:
            self.save()

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- provider API clients

def _retry_after(res: httpx.Response, default: float = 2.0) -> float:
    try:
        return float(res.headers.get("Retry-After", default))
    except ValueError:
        return default


class SpotifyClient:
    API = "https://api.spotify.com/v1"

    def __init__(self, token: Callable[[bool], str], http: httpx.Client, sleep=time.sleep):
        self.token = token
        self.http = http
        self.sleep = sleep

    def _req(self, method: str, path: str, **kw) -> dict:
        force = False
        for _ in range(4):
            res = self.http.request(method, self.API + path,
                                    headers={"Authorization": f"Bearer {self.token(force)}"}, **kw)
            if res.status_code == 401 and not force:
                force = True  # token revoked or expired early: refresh once
                continue
            if res.status_code == 429:
                body = _json(res)
                if (body.get("error") or {}).get("reason") == "QUOTA_EXCEEDED":
                    raise QuotaExceeded("Spotify API kotası doldu; bir süre sonra tekrar deneyin.")
                wait = _retry_after(res)
                if wait > 20:
                    raise ExportError(f"Spotify istek sınırına ulaşıldı; {int(wait)} sn sonra tekrar deneyin.")
                self.sleep(wait)
                continue
            if res.status_code == 401:
                raise NotConnected("Spotify oturumu geçersiz; hesabı yeniden bağlayın.")
            if res.status_code == 403:
                raise ExportError(
                    "Spotify bu hesaba izin vermedi. Uygulama Development Mode'daysa hesap, Spotify Developer "
                    "Dashboard → User Management listesinde olmalı (en fazla 5 kullanıcı).")
            if res.status_code >= 400:
                msg = (_json(res).get("error") or {}).get("message") or f"HTTP {res.status_code}"
                raise ExportError(f"Spotify isteği başarısız: {msg}")
            return _json(res)
        raise ExportError("Spotify istek sınırına ulaşıldı; biraz sonra tekrar deneyin.")

    def account(self) -> str:
        me = self._req("GET", "/me")
        return me.get("display_name") or me.get("id") or "Spotify"

    def find(self, track: Track) -> tuple[str | None, float]:
        if track.platform == "spotify" and SPOTIFY_ID_RE.match(track.source_id or ""):
            return f"spotify:track:{track.source_id}", 1.0
        primary = re.split(r",|&| x | and ", track.artist or "", maxsplit=1)[0].strip()
        title = _PAREN_RE.sub(" ", track.title).strip() or track.title
        best_uri, best = None, 0.0
        for q in (f"track:{title} artist:{primary}", f"{primary} {title}"):
            data = self._req("GET", "/search", params={"q": q, "type": "track", "limit": 10})
            for it in (data.get("tracks") or {}).get("items") or []:
                if not it or not it.get("uri"):
                    continue
                dur = it.get("duration_ms")
                s = match_score(track.title, track.artist, match_duration(track), it.get("name") or "",
                                ", ".join(a.get("name", "") for a in it.get("artists") or []),
                                dur / 1000 if dur else None)
                if s > best:
                    best_uri, best = it["uri"], s
            if best >= MATCH_THRESHOLD:
                break
        return (best_uri, best) if best >= MATCH_THRESHOLD else (None, best)

    def create_playlist(self, name: str, description: str, public: bool) -> tuple[str, str]:
        pl = self._req("POST", "/me/playlists",
                       json={"name": name[:100], "description": description[:300], "public": public})
        url = (pl.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/playlist/{pl['id']}"
        return pl["id"], url

    def add_items(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            self._req("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[i:i + 100]})


class YouTubeClient:
    API = "https://www.googleapis.com/youtube/v3"

    def __init__(self, token: Callable[[bool], str], http: httpx.Client,
                 matcher: Callable[[Track], tuple[str | None, float]] = audio_sources.find_youtube_match):
        self.token = token
        self.http = http
        self.matcher = matcher

    def _req(self, method: str, path: str, **kw) -> dict:
        force = False
        for _ in range(2):
            res = self.http.request(method, self.API + path,
                                    headers={"Authorization": f"Bearer {self.token(force)}"}, **kw)
            if res.status_code == 401 and not force:
                force = True
                continue
            break
        if res.status_code < 400:
            return _json(res)
        err = _json(res).get("error") or {}
        reasons = {e.get("reason") for e in err.get("errors") or []}
        if reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
            raise QuotaExceeded("YouTube API günlük kotası doldu (Pasifik saatiyle gece yarısı sıfırlanır).")
        if "youtubeSignupRequired" in reasons:
            raise ExportError("Bu Google hesabının YouTube kanalı yok; playlist için önce bir kanal oluşturun.")
        if res.status_code == 401:
            raise NotConnected("YouTube oturumu geçersiz; hesabı yeniden bağlayın.")
        if res.status_code == 403 and "accessNotConfigured" in reasons:
            raise ExportError("Google Cloud projesinde YouTube Data API v3 etkin değil.")
        raise ExportError(f"YouTube isteği başarısız: {err.get('message') or f'HTTP {res.status_code}'}")

    def account(self) -> str:
        items = self._req("GET", "/channels", params={"part": "snippet", "mine": "true"}).get("items") or []
        if not items:
            raise ExportError("Bu Google hesabının YouTube kanalı yok; playlist için önce bir kanal oluşturun.")
        return (items[0].get("snippet") or {}).get("title") or "YouTube"

    def find(self, track: Track) -> tuple[str | None, float]:
        return self.matcher(track)

    def create_playlist(self, name: str, description: str, public: bool) -> tuple[str, str]:
        body = {"snippet": {"title": name[:150], "description": description[:5000]},
                "status": {"privacyStatus": "public" if public else "private"}}
        pl = self._req("POST", "/playlists", params={"part": "snippet,status"}, json=body)
        return pl["id"], f"https://music.youtube.com/playlist?list={pl['id']}"

    def add_item(self, playlist_id: str, video_id: str) -> None:
        body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        self._req("POST", "/playlistItems", params={"part": "snippet"}, json=body)


def _json(res: httpx.Response) -> dict:
    try:
        data = res.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- OAuth

class OAuthManager:
    def __init__(self, configs: dict[str, OAuthConfig], store: ConnectionStore,
                 http: httpx.Client | None = None, clock: Callable[[], float] = time.time,
                 yt_matcher: Callable[[Track], tuple[str | None, float]] = audio_sources.find_youtube_match):
        self.configs = configs
        self.store = store
        self.http = http or httpx.Client(timeout=20)
        self.clock = clock
        self.yt_matcher = yt_matcher
        self.pending: dict[str, dict] = {}  # state → {provider, created, status, connection_id?, account?, error?}
        self.lock = threading.RLock()

    def config(self, provider: str) -> OAuthConfig:
        cfg = self.configs.get(provider)
        if not cfg:
            raise ExportError("Bilinmeyen hedef platform.")
        if not cfg.configured:
            raise ExportError(f"{PROVIDER_LABEL[provider]} dışa aktarma sunucuda ayarlı değil (.env'de istemci "
                              "kimliği ve sırrı yok).")
        return cfg

    def providers(self) -> dict[str, dict]:
        return {p: {"configured": c.configured, "redirect_uri": c.redirect_uri} for p, c in self.configs.items()}

    def _prune(self) -> None:
        now = self.clock()
        for s in [s for s, p in self.pending.items() if now - p["created"] > PENDING_TTL_SEC]:
            del self.pending[s]

    def begin(self, provider: str) -> dict:
        cfg = self.config(provider)
        state = secrets.token_urlsafe(24)
        with self.lock:
            self._prune()
            self.pending[state] = {"provider": provider, "created": self.clock(), "status": "pending"}
        return {"state": state, "auth_url": cfg.authorize_url(state)}

    def complete(self, provider: str, state: str | None, code: str | None, error: str | None) -> dict:
        """Callback: exchange the code. Returns the pending entry (status connected / failed)."""
        with self.lock:
            self._prune()
            entry = self.pending.get(state or "")
            if not entry or entry["provider"] != provider or entry["status"] != "pending":
                raise ExportError("Bağlantı isteği bulunamadı veya süresi doldu; MoodFlow'dan yeniden deneyin.")
            entry["status"] = "exchanging"  # a replayed callback can't use the same state twice
        try:
            if error or not code:
                raise ExportError("İzin verilmedi." if error == "access_denied" else f"Bağlantı başarısız ({error}).")
            tokens = self._token_request(provider, {"grant_type": "authorization_code", "code": code,
                                                    "redirect_uri": self.config(provider).redirect_uri})
            if not tokens.get("refresh_token"):
                raise ExportError("Sağlayıcı yenileme anahtarı vermedi; yeniden bağlanmayı deneyin.")
            cid = uuid.uuid4().hex
            conn = {"provider": provider, "access_token": tokens["access_token"],
                    "refresh_token": tokens["refresh_token"],
                    "expires_at": self.clock() + float(tokens.get("expires_in", 3600)), "account": None}
            with self.lock:
                self.store.data[cid] = conn  # needed by client() below; saved once the account is known
            try:
                conn["account"] = self.client(provider, cid).account()
            except ExportError:
                with self.lock:
                    self.store.data.pop(cid, None)
                raise
            with self.lock:
                self.store.put(cid, conn)
                entry.update(status="connected", connection_id=cid, account=conn["account"])
        except ExportError as err:
            with self.lock:
                entry.update(status="failed", error=str(err))
        except httpx.HTTPError as err:
            log.warning("%s token exchange failed: %s", provider, err)
            with self.lock:
                entry.update(status="failed", error=f"{PROVIDER_LABEL[provider]} sunucusuna ulaşılamadı.")
        return dict(entry)

    def poll(self, provider: str, state: str) -> dict:
        with self.lock:
            self._prune()
            entry = self.pending.get(state)
            if not entry or entry["provider"] != provider:
                return {"status": "expired"}
            status = "pending" if entry["status"] == "exchanging" else entry["status"]
            if entry["status"] in ("connected", "failed"):
                del self.pending[state]  # the connection id is handed out once
            return {"status": status, **{k: entry.get(k) for k in ("connection_id", "account", "error")}}

    def _token_request(self, provider: str, data: dict) -> dict:
        cfg = self.config(provider)
        res = self.http.post(cfg.token_url, data={**data, "client_id": cfg.client_id,
                                                  "client_secret": cfg.client_secret})
        body = _json(res)
        if res.status_code >= 400 or "access_token" not in body:
            if body.get("error") in ("invalid_grant", "invalid_token"):
                raise NotConnected(f"{PROVIDER_LABEL[provider]} bağlantısının süresi doldu; yeniden bağlayın.")
            if body.get("error") == "invalid_client":
                raise ExportError(f"{PROVIDER_LABEL[provider]} istemci kimliği/sırrı geçersiz (.env).")
            raise ExportError(f"{PROVIDER_LABEL[provider]} bağlantısı başarısız: "
                              f"{body.get('error_description') or body.get('error') or res.status_code}")
        return body

    def connection(self, provider: str, cid: str | None) -> dict:
        with self.lock:
            conn = self.store.get(cid or "")
        if not conn or conn["provider"] != provider:
            raise NotConnected(f"{PROVIDER_LABEL.get(provider, provider)} hesabı bağlı değil.")
        return conn

    def access_token(self, provider: str, cid: str, force: bool = False) -> str:
        with self.lock:
            conn = self.connection(provider, cid)
            if not force and conn["expires_at"] - 60 > self.clock():
                return conn["access_token"]
            try:
                tokens = self._token_request(provider, {"grant_type": "refresh_token",
                                                        "refresh_token": conn["refresh_token"]})
            except NotConnected:
                self.store.delete(cid)
                raise
            conn["access_token"] = tokens["access_token"]
            conn["refresh_token"] = tokens.get("refresh_token") or conn["refresh_token"]  # Spotify may rotate it
            conn["expires_at"] = self.clock() + float(tokens.get("expires_in", 3600))
            if cid in self.store.data:
                self.store.save()
            return conn["access_token"]

    def client(self, provider: str, cid: str):
        token = lambda force=False: self.access_token(provider, cid, force)  # noqa: E731
        if provider == "spotify":
            return SpotifyClient(token, self.http)
        return YouTubeClient(token, self.http, matcher=self.yt_matcher)

    def disconnect(self, provider: str, cid: str | None) -> None:
        with self.lock:
            conn = self.store.get(cid or "")
            if conn and conn["provider"] == provider:
                self.store.delete(cid)


# --------------------------------------------------------------------------- export jobs

class ExportService:
    def __init__(self, oauth: OAuthManager, match_workers: int = 4):
        self.oauth = oauth
        self.match_workers = match_workers
        self.jobs: dict[str, ExportStatus] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="export")

    def start(self, provider: str, cid: str | None, tracks: list[Track], name: str, description: str,
              public: bool, run_async: bool = True) -> ExportStatus:
        self.oauth.connection(provider, cid)  # NotConnected before a job is created
        if not tracks:
            raise ExportError("Aktarılacak şarkı yok.")
        name = name.strip() or "MoodFlow"
        export_id = uuid.uuid4().hex[:12]
        job = ExportStatus(
            export_id=export_id, provider=provider, status="processing", stage="matching", playlist_name=name,
            items=[ExportItem(occurrence_id=t.occurrence_id, title=t.title, artist=t.artist) for t in tracks],
        )
        with self.lock:
            self._prune()
            self.jobs[export_id] = job
        args = (export_id, provider, cid, tracks, name, description, public)
        if run_async:
            self.executor.submit(self._run, *args)
        else:
            self._run(*args)
        return self.status(export_id)

    def _prune(self) -> None:
        if len(self.jobs) > 50:
            for k in list(self.jobs)[: len(self.jobs) - 50]:
                del self.jobs[k]

    def status(self, export_id: str) -> ExportStatus | None:
        with self.lock:
            job = self.jobs.get(export_id)
            return job.model_copy(deep=True) if job else None

    def _update(self, export_id: str, **changes) -> None:
        with self.lock:
            self.jobs[export_id] = self.jobs[export_id].model_copy(update=changes)

    def _item(self, export_id: str, idx: int, **changes) -> None:
        with self.lock:
            item = self.jobs[export_id].items[idx]
            for k, v in changes.items():
                setattr(item, k, v)

    def _run(self, export_id: str, provider: str, cid: str, tracks: list[Track], name: str,
             description: str, public: bool) -> None:
        client = self.oauth.client(provider, cid)
        label = PROVIDER_LABEL[provider]
        try:
            # 1. match every song (the source platform's own ids need no lookup)
            def match(idx: int) -> None:
                self._item(export_id, idx, status="matching")
                try:
                    target, score = client.find(tracks[idx])
                except (NotConnected, QuotaExceeded):
                    raise
                except ExportError as err:
                    self._item(export_id, idx, status="failed", message=str(err))
                    return
                if target:
                    self._item(export_id, idx, status="matched", target_id=target)
                else:
                    self._item(export_id, idx, status="not_found",
                               message=f"{label} üzerinde güvenilir eşleşme yok (skor {score:.2f}).")

            with ThreadPoolExecutor(self.match_workers, thread_name_prefix="export-match") as pool:
                for fut in [pool.submit(match, i) for i in range(len(tracks))]:
                    fut.result()

            matched = [(i, it.target_id) for i, it in enumerate(self.status(export_id).items)
                       if it.status == "matched" and it.target_id]
            if not matched:
                raise ExportError(f"Hiçbir şarkı {label} üzerinde bulunamadı; playlist oluşturulmadı.")

            # 2. create the playlist, 3. add the songs in the MoodFlow order
            self._update(export_id, stage="creating")
            playlist_id, url = client.create_playlist(name, description, public)
            self._update(export_id, stage="adding", playlist_url=url)
            if provider == "spotify":
                client.add_items(playlist_id, [t for _, t in matched])
                for i, _ in matched:
                    self._item(export_id, i, status="added")
            else:
                for n, (i, video_id) in enumerate(matched):
                    try:
                        client.add_item(playlist_id, video_id)
                    except QuotaExceeded as err:
                        for j, _ in matched[n:]:
                            self._item(export_id, j, status="failed", message="Kota doldu; eklenmedi.")
                        self._finish(export_id, warning=f"{err} Playlist eksik kaldı.")
                        return
                    except NotConnected:
                        raise
                    except ExportError as err:
                        self._item(export_id, i, status="failed", message=str(err))
                        continue
                    self._item(export_id, i, status="added")
            self._finish(export_id)
        except ExportError as err:
            self._update(export_id, status="failed", stage="failed", error=str(err))
        except Exception as err:  # noqa: BLE001 — surface any failure to the UI
            log.exception("export %s failed", export_id)
            self._update(export_id, status="failed", stage="failed", error=f"Dışa aktarma başarısız: {err}")

    def _finish(self, export_id: str, warning: str | None = None) -> None:
        job = self.status(export_id)
        added = sum(it.status == "added" for it in job.items)
        warnings = list(job.warnings) + ([warning] if warning else [])
        self._update(export_id, status="done", stage="done", added=added, warnings=warnings)
