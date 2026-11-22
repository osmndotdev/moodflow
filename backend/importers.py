"""Public playlist importers (no login): YouTube via yt-dlp, Spotify via the public embed page,
Apple Music via the public playlist page."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .schemas import Platform, Track

MAX_ITEMS = 20
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

_YT_LIST_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_APPLE_ID_RE = re.compile(r"^pl\.(u-)?[A-Za-z0-9]{8,64}$")  # catalog (pl.xxx) or shared user (pl.u-xxx)
_STOREFRONT_RE = re.compile(r"^[a-z]{2}$")


class PlaylistImportError(Exception):
    """Raised with a user-facing (Turkish) message."""


@dataclass
class ImportResult:
    platform: Platform
    title: str | None
    tracks: list[Track]
    warnings: list[str]


# --------------------------------------------------------------------------- URL parsing

def parse_playlist_url(url: str) -> tuple[Platform, str]:
    url = (url or "").strip()
    if not url:
        raise PlaylistImportError("Lütfen bir playlist URL'si girin.")
    if url.startswith("spotify:playlist:"):
        pid = url.rsplit(":", 1)[-1]
        if _SPOTIFY_ID_RE.match(pid):
            return "spotify", pid
        raise PlaylistImportError("Spotify playlist kimliği geçersiz.")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host.endswith("spotify.com"):
        parts = [p for p in parsed.path.split("/") if p]
        # /playlist/<id>, /embed/playlist/<id>, /intl-tr/playlist/<id>
        if "playlist" in parts:
            idx = parts.index("playlist")
            if idx + 1 < len(parts) and _SPOTIFY_ID_RE.match(parts[idx + 1]):
                return "spotify", parts[idx + 1]
        raise PlaylistImportError("Bu bir Spotify playlist bağlantısı değil (open.spotify.com/playlist/... bekleniyor).")

    if host.endswith("music.apple.com") or host == "itunes.apple.com":
        parts = [p for p in parsed.path.split("/") if p]
        # /{storefront}/playlist/{slug}/{id}  or  /{storefront}/playlist/{id}
        if "playlist" in parts and parts[-1] and _APPLE_ID_RE.match(parts[-1]):
            storefront = parts[0] if _STOREFRONT_RE.match(parts[0]) else "us"
            return "apple", f"{storefront}/{parts[-1]}"
        raise PlaylistImportError(
            "Bu bir Apple Music playlist bağlantısı değil (music.apple.com/.../playlist/... bekleniyor)."
        )

    if host in {"youtu.be"} or host.endswith("youtube.com"):
        list_id = parse_qs(parsed.query).get("list", [None])[0]
        if list_id and _YT_LIST_RE.match(list_id):
            return "youtube", list_id
        raise PlaylistImportError("YouTube bağlantısında playlist ('list=' parametresi) bulunamadı.")

    raise PlaylistImportError(
        "Yalnızca herkese açık YouTube, Spotify veya Apple Music playlist bağlantıları destekleniyor."
    )


# --------------------------------------------------------------------------- YouTube

_BRACKET_RE = re.compile(r"[\(\[\{]([^\(\)\[\]\{\}]*)[\)\]\}]")
_NOISE_WORDS = re.compile(
    r"\b(official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|remaster(ed)?|m/?v|clip|music video)\b", re.I
)


def _strip_noise(title: str) -> str:
    out = _BRACKET_RE.sub(lambda m: "" if _NOISE_WORDS.search(m.group(1)) else m.group(0), title)
    return re.sub(r"\s{2,}", " ", out).strip(" -|")


_CHANNEL_NOISE_RE = re.compile(r"(\s*-\s*topic$|vevo$|\s+official$|\s+offiziell$|\s*tv$)", re.I)


def split_artist_title(raw_title: str, channel: str | None) -> tuple[str, str]:
    """'a-ha - Take On Me (Official Video) [4K]' -> ('a-ha', 'Take On Me')."""
    title = _strip_noise(raw_title or "")
    for sep in (" - ", " – ", " — ", " | "):
        if sep in title:
            artist, _, rest = title.partition(sep)
            if artist.strip() and rest.strip():
                return artist.strip(), rest.strip()
    artist = _CHANNEL_NOISE_RE.sub("", (channel or "").strip()).strip()
    return artist or "Unknown", title or raw_title


def _ydl_flat_options(limit: int) -> dict:
    return {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "playlistend": limit,
        "extractor_retries": 1,
        "retries": 1,
        "socket_timeout": 15,
    }


def _youtube_error_message(err: Exception, list_id: str = "") -> str:
    msg = str(err).replace(list_id, "").lower()  # the id itself may contain words like "private"
    if "private" in msg:
        return "Bu YouTube playlisti gizli. Yalnızca herkese açık listeler okunabilir."
    if any(s in msg for s in ("does not exist", "404", "400", "invalid argument", "not found")):
        return "YouTube playlisti bulunamadı veya erişilemiyor (silinmiş ya da gizli olabilir)."
    return "YouTube playlisti okunamadı. Bağlantıyı ve listenin herkese açık olduğunu kontrol edin."


def import_youtube(list_id: str, limit: int = MAX_ITEMS, ydl_cls=YoutubeDL) -> ImportResult:
    url = f"https://www.youtube.com/playlist?list={list_id}"
    try:
        with ydl_cls(_ydl_flat_options(limit)) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as err:
        raise PlaylistImportError(_youtube_error_message(err, list_id)) from err

    entries = list((info or {}).get("entries") or [])[:limit]
    if not entries:
        raise PlaylistImportError("YouTube playlisti boş veya erişilemiyor.")

    tracks: list[Track] = []
    warnings: list[str] = []
    for pos, entry in enumerate(entries, start=1):
        vid = (entry or {}).get("id")
        raw_title = (entry or {}).get("title") or ""
        if not vid or raw_title.strip("[]").lower() in {"private video", "deleted video"}:
            warnings.append(f"{pos}. öğe erişilemiyor (gizli/silinmiş video) ve atlandı.")
            continue
        channel = entry.get("channel") or entry.get("uploader")
        artist, title = split_artist_title(raw_title, channel)
        tracks.append(Track(
            occurrence_id=f"item-{pos:03d}",
            platform="youtube",
            source_id=vid,
            title=title,
            artist=artist,
            duration_sec=entry.get("duration"),
            original_position=pos,
            youtube_video_id=vid,
            match_status="matched",
            source_url=f"https://www.youtube.com/watch?v={vid}",
        ))
    if not tracks:
        raise PlaylistImportError("Playlistteki videoların hiçbirine erişilemiyor.")
    return ImportResult("youtube", info.get("title"), tracks, warnings)


# --------------------------------------------------------------------------- Spotify

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


class SpotifyPublicAdapter:
    """Reads a public playlist from Spotify's embed page (no OAuth, no API key).

    The embed page ships the first ~50-100 tracks as JSON in ``__NEXT_DATA__``.
    That's plenty for the 20-item demo limit.
    """

    EMBED_URL = "https://open.spotify.com/embed/playlist/{id}"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}
        )

    def fetch_html(self, playlist_id: str) -> str:
        try:
            resp = self.client.get(self.EMBED_URL.format(id=playlist_id))
        except httpx.HTTPError as err:
            raise PlaylistImportError("Spotify'a bağlanılamadı. Daha sonra tekrar deneyin.") from err
        if resp.status_code == 404:
            raise PlaylistImportError("Spotify playlisti bulunamadı veya herkese açık değil.")
        if resp.status_code >= 400:
            raise PlaylistImportError(f"Spotify playlisti okunamadı (HTTP {resp.status_code}).")
        return resp.text

    @staticmethod
    def parse_html(html: str, limit: int = MAX_ITEMS) -> tuple[str | None, list[dict]]:
        m = _NEXT_DATA_RE.search(html)
        if not m:
            raise PlaylistImportError("Spotify sayfası beklenen formatta değil; girişsiz okuma şu an çalışmıyor.")
        try:
            data = json.loads(m.group(1))
            entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        except (KeyError, TypeError, json.JSONDecodeError) as err:
            raise PlaylistImportError(
                "Spotify playlisti okunamadı (liste gizli, bölgesel olarak kısıtlı veya format değişmiş)."
            ) from err
        items = entity.get("trackList") or []
        if not items:
            raise PlaylistImportError("Spotify playlisti boş veya erişilemiyor.")
        return entity.get("name") or entity.get("title"), items[:limit]

    def fetch(self, playlist_id: str, limit: int = MAX_ITEMS) -> ImportResult:
        name, items = self.parse_html(self.fetch_html(playlist_id), limit)
        tracks: list[Track] = []
        warnings: list[str] = []
        for pos, item in enumerate(items, start=1):
            uri = item.get("uri") or ""
            if not uri.startswith("spotify:track:"):
                warnings.append(f"{pos}. öğe şarkı değil (podcast/yerel dosya) ve atlandı.")
                continue
            artists = [a.strip() for a in re.split(r",\s*", (item.get("subtitle") or "").replace("\xa0", " ")) if a.strip()]
            duration_ms = item.get("duration")
            tracks.append(Track(
                occurrence_id=f"item-{pos:03d}",
                platform="spotify",
                source_id=uri.rsplit(":", 1)[-1],
                title=item.get("title") or "Unknown",
                artist=", ".join(artists) or "Unknown",
                duration_sec=round(duration_ms / 1000, 1) if duration_ms else None,
                original_position=pos,
                source_url=f"https://open.spotify.com/track/{uri.rsplit(':', 1)[-1]}",
            ))
        if not tracks:
            raise PlaylistImportError("Spotify playlistinde okunabilir şarkı yok.")
        return ImportResult("spotify", name, tracks, warnings)


# --------------------------------------------------------------------------- Apple Music

_APPLE_DATA_RE = re.compile(r'<script type="application/json" id="serialized-server-data">(.*?)</script>', re.S)


class AppleMusicPublicAdapter:
    """Reads a public Apple Music playlist from its web page (no login, no developer token).

    The page ships its first ~100 tracks as JSON in ``serialized-server-data``; the track-list
    section holds title, artist, duration and the catalog song id. Unofficial, like the Spotify
    embed reader: if Apple changes the page, the import fails with a clear error.
    """

    PAGE_URL = "https://music.apple.com/{storefront}/playlist/{id}"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}
        )

    def fetch_html(self, ref: str) -> str:
        storefront, pid = ref.split("/", 1)
        try:
            resp = self.client.get(self.PAGE_URL.format(storefront=storefront, id=pid))
        except httpx.HTTPError as err:
            raise PlaylistImportError("Apple Music'e bağlanılamadı. Daha sonra tekrar deneyin.") from err
        if resp.status_code == 404:
            raise PlaylistImportError("Apple Music playlisti bulunamadı veya herkese açık değil.")
        if resp.status_code >= 400:
            raise PlaylistImportError(f"Apple Music playlisti okunamadı (HTTP {resp.status_code}).")
        return resp.text

    @staticmethod
    def parse_html(html: str, limit: int = MAX_ITEMS) -> tuple[str | None, list[dict]]:
        m = _APPLE_DATA_RE.search(html)
        if not m:
            raise PlaylistImportError("Apple Music sayfası beklenen formatta değil; girişsiz okuma şu an çalışmıyor.")
        try:
            data = json.loads(m.group(1))
            page = data["data"][0]["data"]
            sections = page["sections"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as err:
            raise PlaylistImportError("Apple Music playlisti okunamadı (format değişmiş olabilir).") from err
        title = None
        items: list[dict] = []
        for sec in sections:
            kind = sec.get("itemKind")
            if kind == "containerDetailHeaderLockup" and sec.get("items"):
                title = sec["items"][0].get("title")
            elif kind == "trackLockup":
                items.extend(sec.get("items") or [])
        if not items:
            raise PlaylistImportError("Apple Music playlisti boş veya erişilemiyor.")
        return title, items[:limit]

    def fetch(self, ref: str, limit: int = MAX_ITEMS) -> ImportResult:
        name, items = self.parse_html(self.fetch_html(ref), limit)
        tracks: list[Track] = []
        warnings: list[str] = []
        for pos, item in enumerate(items, start=1):
            desc = item.get("contentDescriptor") or {}
            song_id = (desc.get("identifiers") or {}).get("storeAdamID")
            title = item.get("title")
            if desc.get("kind") not in ("song", "musicVideo") or not song_id or not title:
                warnings.append(f"{pos}. öğe şarkı değil ve atlandı.")
                continue
            if item.get("isDisabled"):
                warnings.append(f"{pos}. öğe ({title}) bu bölgede kullanılamıyor ve atlandı.")
                continue
            artist = item.get("artistName") or ", ".join(
                a.get("title", "") for a in item.get("subtitleLinks") or [] if a.get("title")
            )
            duration_ms = item.get("duration")
            tracks.append(Track(
                occurrence_id=f"item-{pos:03d}",
                platform="apple",
                source_id=str(song_id),
                title=title,
                artist=artist or "Unknown",
                duration_sec=round(duration_ms / 1000, 1) if duration_ms else None,
                original_position=pos,
                source_url=desc.get("url"),
            ))
        if not tracks:
            raise PlaylistImportError("Apple Music playlistinde okunabilir şarkı yok.")
        return ImportResult("apple", name, tracks, warnings)


def import_playlist(url: str) -> ImportResult:
    platform, pid = parse_playlist_url(url)
    if platform == "youtube":
        return import_youtube(pid)
    if platform == "apple":
        return AppleMusicPublicAdapter().fetch(pid)
    return SpotifyPublicAdapter().fetch(pid)
