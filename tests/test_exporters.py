import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from backend import exporters
from backend.exporters import (
    ConnectionStore, ExportService, NotConnected, OAuthManager, SpotifyClient, load_configs,
)
from backend.schemas import Track


def _track(i, platform="youtube", title="Take On Me", artist="a-ha", duration=225, source_id=None, video=None):
    return Track(occurrence_id=f"item-{i:03d}", platform=platform, source_id=source_id or f"src{i}",
                 title=title, artist=artist, duration_sec=duration, original_position=i,
                 youtube_video_id=video)


class FakeProviders:
    """Spotify + Google token, Web API and Data API endpoints behind one MockTransport."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.token_n = 0
        self.refresh_ok = True
        self.yt_quota_after: int | None = None
        self.yt_inserted: list[str] = []
        self.sp_added: list[str] = []
        self.search_items: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = None
        if req.content:
            try:
                body = json.loads(req.content)
            except ValueError:
                body = parse_qs(req.content.decode())
        self.calls.append((req.method, str(req.url), body))
        host, path = req.url.host, req.url.path
        if path in ("/api/token", "/token"):
            grant = body["grant_type"][0]
            if grant == "refresh_token" and not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.token_n += 1
            out = {"access_token": f"at{self.token_n}", "expires_in": 3600}
            if grant == "authorization_code":
                out["refresh_token"] = "rt"
            return httpx.Response(200, json=out)
        if host == "api.spotify.com":
            if path == "/v1/me":
                return httpx.Response(200, json={"id": "u1", "display_name": "Osman"})
            if path == "/v1/search":
                return httpx.Response(200, json={"tracks": {"items": self.search_items}})
            if path == "/v1/me/playlists":
                return httpx.Response(201, json={"id": "PL1", "external_urls": {"spotify": "https://open.spotify.com/playlist/PL1"}})
            if path == "/v1/playlists/PL1/items":
                self.sp_added += body["uris"]
                return httpx.Response(201, json={"snapshot_id": "s"})
        if host == "www.googleapis.com":
            if path == "/youtube/v3/channels":
                return httpx.Response(200, json={"items": [{"snippet": {"title": "Osman Kanal"}}]})
            if path == "/youtube/v3/playlists":
                return httpx.Response(200, json={"id": "YTPL"})
            if path == "/youtube/v3/playlistItems":
                if self.yt_quota_after is not None and len(self.yt_inserted) >= self.yt_quota_after:
                    return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}], "message": "q"}})
                self.yt_inserted.append(body["snippet"]["resourceId"]["videoId"])
                return httpx.Response(200, json={"id": "item"})
        return httpx.Response(404, json={})


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k, v in {"SPOTIFY_CLIENT_ID": "sid", "SPOTIFY_CLIENT_SECRET": "ssec",
                 "GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gsec"}.items():
        monkeypatch.setenv(k, v)
    fake = FakeProviders()
    clock = {"t": 1000.0}
    store = ConnectionStore(tmp_path / "oauth" / "connections.json")
    matcher = lambda t: (t.youtube_video_id or f"vid{t.original_position:08d}", 0.9)  # noqa: E731
    oauth = OAuthManager(load_configs("http://127.0.0.1:8000"), store,
                         http=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                         clock=lambda: clock["t"], yt_matcher=matcher)
    return fake, oauth, store, clock, tmp_path


def _connect(oauth, provider):
    started = oauth.begin(provider)
    oauth.complete(provider, started["state"], "CODE", None)
    res = oauth.poll(provider, started["state"])
    assert res["status"] == "connected"
    return res["connection_id"]


def test_redirect_uris_use_loopback_ip_and_public_url(monkeypatch):
    monkeypatch.delenv("MOODFLOW_PUBLIC_URL", raising=False)
    monkeypatch.setenv("MOODFLOW_PORT", "8123")
    cfg = load_configs()
    assert cfg["spotify"].redirect_uri == "http://127.0.0.1:8123/api/export/spotify/callback"
    monkeypatch.setenv("MOODFLOW_PUBLIC_URL", "https://moodflow.example/")
    assert load_configs()["youtube"].redirect_uri == "https://moodflow.example/api/export/youtube/callback"


def test_unconfigured_provider_is_refused(env, monkeypatch):
    _, oauth, *_ = env
    oauth.configs["spotify"].client_secret = ""
    assert oauth.providers()["spotify"]["configured"] is False
    with pytest.raises(exporters.ExportError, match="ayarlı değil"):
        oauth.begin("spotify")


def test_oauth_flow_stores_tokens_and_hands_out_id_once(env):
    fake, oauth, store, _, tmp = env
    started = oauth.begin("spotify")
    q = parse_qs(urlparse(started["auth_url"]).query)
    assert q["state"] == [started["state"]] and q["client_id"] == ["sid"]
    assert set(q["scope"][0].split()) == {"playlist-modify-private", "playlist-modify-public"}
    assert oauth.poll("spotify", started["state"])["status"] == "pending"

    oauth.complete("spotify", started["state"], "CODE", None)
    res = oauth.poll("spotify", started["state"])
    assert res["status"] == "connected" and res["account"] == "Osman"
    assert oauth.poll("spotify", started["state"])["status"] == "expired"  # one-time

    path = tmp / "oauth" / "connections.json"
    assert (path.stat().st_mode & 0o777) == 0o600
    saved = json.loads(path.read_text())[res["connection_id"]]
    assert saved["refresh_token"] == "rt" and saved["account"] == "Osman"
    # survives a restart
    assert ConnectionStore(path).get(res["connection_id"])["provider"] == "spotify"


def test_callback_rejects_unknown_replayed_or_cross_provider_state(env):
    _, oauth, *_ = env
    with pytest.raises(exporters.ExportError):
        oauth.complete("spotify", "nope", "CODE", None)
    started = oauth.begin("spotify")
    with pytest.raises(exporters.ExportError):
        oauth.complete("youtube", started["state"], "CODE", None)
    oauth.complete("spotify", started["state"], "CODE", None)
    with pytest.raises(exporters.ExportError):
        oauth.complete("spotify", started["state"], "CODE", None)


def test_denied_consent_reports_failure(env):
    _, oauth, *_ = env
    started = oauth.begin("youtube")
    entry = oauth.complete("youtube", started["state"], None, "access_denied")
    assert entry["status"] == "failed" and "İzin verilmedi" in entry["error"]


def test_pending_state_expires(env):
    _, oauth, _, clock, _ = env
    started = oauth.begin("spotify")
    clock["t"] += exporters.PENDING_TTL_SEC + 1
    assert oauth.poll("spotify", started["state"])["status"] == "expired"


def test_access_token_refreshes_and_drops_revoked_connection(env):
    fake, oauth, store, clock, _ = env
    cid = _connect(oauth, "spotify")
    assert oauth.access_token("spotify", cid) == "at1"
    clock["t"] += 3600
    assert oauth.access_token("spotify", cid) == "at2"
    clock["t"] += 3600
    fake.refresh_ok = False
    with pytest.raises(NotConnected):
        oauth.access_token("spotify", cid)
    assert store.get(cid) is None
    with pytest.raises(NotConnected):
        oauth.connection("youtube", cid)


def test_spotify_find_keeps_spotify_ids_and_scores_search_hits(env):
    fake, oauth, *_ = env
    client = oauth.client("spotify", _connect(oauth, "spotify"))
    sid = "4uLU6hMCjMI75M1A2tKUQC"
    assert client.find(_track(1, "spotify", source_id=sid)) == (f"spotify:track:{sid}", 1.0)

    fake.search_items = [
        {"uri": "spotify:track:live", "name": "Take On Me - Live", "artists": [{"name": "a-ha"}], "duration_ms": 240000},
        {"uri": "spotify:track:good", "name": "Take On Me", "artists": [{"name": "a-ha"}], "duration_ms": 225000},
    ]
    uri, score = client.find(_track(2, "apple"))
    assert uri == "spotify:track:good" and score >= exporters.MATCH_THRESHOLD
    searches = [c for c in fake.calls if "/v1/search" in c[1]]
    assert "limit=10" in searches[-1][1]  # Development Mode allows at most 10

    fake.search_items = [{"uri": "spotify:track:x", "name": "Something Else", "artists": [{"name": "Nobody"}]}]
    assert client.find(_track(3, "youtube"))[0] is None


def test_spotify_403_explains_allowlist():
    def handler(req):
        return httpx.Response(403, json={"error": {"message": "Forbidden"}})
    client = SpotifyClient(lambda force=False: "t", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(exporters.ExportError, match="User Management"):
        client.account()


def test_spotify_export_creates_playlist_in_order(env):
    fake, oauth, *_ = env
    cid = _connect(oauth, "spotify")
    ids = ["4uLU6hMCjMI75M1A2tKUQC", "7ouMYWpwJ422jRcDASZB7P"]
    tracks = [_track(1, "spotify", source_id=ids[1]), _track(2, "youtube", title="Unknown Song", artist="Nobody"),
              _track(3, "spotify", source_id=ids[0])]
    job = ExportService(oauth).start("spotify", cid, tracks, "Mix · MoodFlow", "desc", False, run_async=False)
    assert job.status == "done", job.error
    assert job.playlist_url == "https://open.spotify.com/playlist/PL1" and job.added == 2
    assert fake.sp_added == [f"spotify:track:{ids[1]}", f"spotify:track:{ids[0]}"]
    assert [i.status for i in job.items] == ["added", "not_found", "added"]
    create = next(c for c in fake.calls if c[1].endswith("/v1/me/playlists"))
    assert create[2] == {"name": "Mix · MoodFlow", "description": "desc", "public": False}


def test_export_requires_connection(env):
    _, oauth, *_ = env
    with pytest.raises(NotConnected):
        ExportService(oauth).start("spotify", "missing", [_track(1)], "x", "", False, run_async=False)


def test_youtube_export_uses_video_ids_and_stops_on_quota(env):
    fake, oauth, *_ = env
    cid = _connect(oauth, "youtube")
    tracks = [_track(i, video=f"v{i:010d}") for i in range(1, 5)]
    fake.yt_quota_after = 2
    job = ExportService(oauth).start("youtube", cid, tracks, "Mix", "", True, run_async=False)
    assert job.status == "done" and job.added == 2
    assert fake.yt_inserted == ["v0000000001", "v0000000002"]
    assert job.playlist_url == "https://music.youtube.com/playlist?list=YTPL"
    assert [i.status for i in job.items] == ["added", "added", "failed", "failed"]
    assert any("kota" in w.lower() for w in job.warnings)
    create = next(c for c in fake.calls if c[1].startswith("https://www.googleapis.com/youtube/v3/playlists"))
    assert create[2]["status"] == {"privacyStatus": "public"}


def test_export_with_no_matches_fails_without_creating_playlist(env):
    fake, oauth, *_ = env
    cid = _connect(oauth, "spotify")
    job = ExportService(oauth).start("spotify", cid, [_track(1, title="Zzz", artist="Nobody")], "x", "", False,
                                     run_async=False)
    assert job.status == "failed" and "bulunamadı" in job.error
    assert not any(c[1].endswith("/v1/me/playlists") for c in fake.calls)


def test_callback_endpoint_renders_page_without_secrets(monkeypatch):
    from fastapi.testclient import TestClient

    from backend import main
    res = TestClient(main.app).get("/api/export/spotify/callback", params={"state": "bogus", "code": "c"})
    assert res.status_code == 200 and "bağlanamadı" in res.text
    providers = TestClient(main.app).get("/api/export/providers").json()
    assert set(providers) == {"spotify", "youtube"}
    assert TestClient(main.app).get("/api/export/spotify/account").status_code == 401
