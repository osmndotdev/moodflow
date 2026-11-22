import json

import httpx
import pytest
from yt_dlp.utils import DownloadError

from backend.importers import (
    PlaylistImportError, SpotifyPublicAdapter, import_youtube, parse_playlist_url, split_artist_title,
)


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/playlist?list=PLGBuKfnErZlAKsNDquW0QoEYZle-NMmDL", ("youtube", "PLGBuKfnErZlAKsNDquW0QoEYZle-NMmDL")),
    ("https://youtube.com/watch?v=djV11Xbc914&list=PLGBuKfnErZlAKsNDquW0QoEYZle-NMmDL&index=2", ("youtube", "PLGBuKfnErZlAKsNDquW0QoEYZle-NMmDL")),
    ("music.youtube.com/playlist?list=OLAK5uy_abcdefghijk", ("youtube", "OLAK5uy_abcdefghijk")),
    ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=xyz", ("spotify", "37i9dQZF1DXcBWIGoYBM5M")),
    ("https://open.spotify.com/intl-tr/playlist/37i9dQZF1DXcBWIGoYBM5M", ("spotify", "37i9dQZF1DXcBWIGoYBM5M")),
    ("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", ("spotify", "37i9dQZF1DXcBWIGoYBM5M")),
])
def test_parse_valid_urls(url, expected):
    assert parse_playlist_url(url) == expected


@pytest.mark.parametrize("url", [
    "", "https://example.com/playlist?list=abc", "https://www.youtube.com/watch?v=djV11Xbc914",
    "https://open.spotify.com/album/37i9dQZF1DXcBWIGoYBM5M", "https://open.spotify.com/playlist/short",
])
def test_parse_invalid_urls(url):
    with pytest.raises(PlaylistImportError):
        parse_playlist_url(url)


def test_split_artist_title():
    assert split_artist_title("a-ha - Take On Me (Official Video) [4K]", "a-ha") == ("a-ha", "Take On Me")
    assert split_artist_title("Maniac", "Michael Sembello") == ("Michael Sembello", "Maniac")
    assert split_artist_title("Song", "Some Band - Topic") == ("Some Band", "Song")
    # non-noise brackets stay
    assert split_artist_title("X - Sweet Dreams (Are Made Of This)", None) == ("X", "Sweet Dreams (Are Made Of This)")


class FakeYDL:
    info: dict | None = None
    error: Exception | None = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if self.error:
            raise self.error
        return self.info


def test_import_youtube_skips_unavailable_items():
    class Y(FakeYDL):
        info = {"title": "List", "entries": [
            {"id": "djV11Xbc914", "title": "a-ha - Take On Me (Official Video)", "channel": "a-ha", "duration": 244},
            {"id": "xxxxxxxxxxx", "title": "[Private video]"},
            {"id": "aGCdLKXNF3w", "title": "Everybody Wants To Rule The World", "channel": "Tears For Fears", "duration": 291},
        ]}
    res = import_youtube("PLtest12345", ydl_cls=Y)
    assert [t.youtube_video_id for t in res.tracks] == ["djV11Xbc914", "aGCdLKXNF3w"]
    assert res.tracks[1].artist == "Tears For Fears"
    assert res.tracks[1].original_position == 3
    assert len(res.warnings) == 1


def test_import_youtube_limit():
    from backend.importers import MAX_ITEMS

    class Y(FakeYDL):
        info = {"entries": [{"id": f"vid{i:08d}", "title": f"A - Song {i}"} for i in range(80)]}
    assert MAX_ITEMS == 50
    assert len(import_youtube("PLtest12345", ydl_cls=Y).tracks) == 50


def test_import_youtube_error_ignores_words_in_list_id():
    class Y(FakeYDL):
        error = DownloadError("ERROR: [youtube:tab] PLprivate0000: HTTP Error 400: Bad Request")
    with pytest.raises(PlaylistImportError, match="bulunamadı"):
        import_youtube("PLprivate0000", ydl_cls=Y)


@pytest.mark.parametrize("msg,needle", [
    ("ERROR: This playlist is private", "gizli"),
    ("ERROR: Unable to download API page: HTTP Error 400: Bad Request", "bulunamadı"),
])
def test_import_youtube_errors(msg, needle):
    class Y(FakeYDL):
        error = DownloadError(msg)
    with pytest.raises(PlaylistImportError, match=needle):
        import_youtube("PLtest12345", ydl_cls=Y)


def _embed_html(track_list):
    data = {"props": {"pageProps": {"state": {"data": {"entity": {"name": "My List", "trackList": track_list}}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def test_spotify_adapter_parses_embed():
    items = [
        {"uri": "spotify:track:4HCOHRCSQelz2GPX8iTamO", "title": "Song A", "subtitle": "Artist 1, Artist 2", "duration": 224877},
        {"uri": "spotify:episode:abc", "title": "Podcast"},
        {"uri": "spotify:track:1111111111111111111111", "title": "Song B", "subtitle": "Solo", "duration": 180000},
    ]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=_embed_html(items)))
    res = SpotifyPublicAdapter(httpx.Client(transport=transport)).fetch("37i9dQZF1DXcBWIGoYBM5M")
    assert res.title == "My List"
    assert [t.title for t in res.tracks] == ["Song A", "Song B"]
    assert res.tracks[0].artist == "Artist 1, Artist 2"
    assert res.tracks[0].duration_sec == 224.9
    assert res.tracks[0].youtube_video_id is None and res.tracks[0].match_status == "pending"
    assert len(res.warnings) == 1


@pytest.mark.parametrize("status,body", [(404, ""), (200, "<html>no data</html>"), (200, _embed_html([]))])
def test_spotify_adapter_errors(status, body):
    transport = httpx.MockTransport(lambda req: httpx.Response(status, text=body))
    with pytest.raises(PlaylistImportError):
        SpotifyPublicAdapter(httpx.Client(transport=transport)).fetch("37i9dQZF1DXcBWIGoYBM5M")


# ------------------------------------------------------------------ Apple Music

from backend.importers import AppleMusicPublicAdapter  # noqa: E402


@pytest.mark.parametrize("url,expected", [
    ("https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb",
     ("apple", "us/pl.f4d106fed2bd41149aaacabb233eb5eb")),
    ("https://music.apple.com/tr/playlist/pl.f4d106fed2bd41149aaacabb233eb5eb?l=tr",
     ("apple", "tr/pl.f4d106fed2bd41149aaacabb233eb5eb")),
    ("music.apple.com/gb/playlist/my-mix/pl.u-AkAmPlyUxXkWJ9", ("apple", "gb/pl.u-AkAmPlyUxXkWJ9")),
])
def test_parse_apple_urls(url, expected):
    assert parse_playlist_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://music.apple.com/us/album/stupid-song/1889992111",
    "https://music.apple.com/us/playlist/x/not-a-playlist-id",
])
def test_parse_apple_invalid(url):
    with pytest.raises(PlaylistImportError):
        parse_playlist_url(url)


def _apple_item(title, artist, song_id, duration=200000, kind="song", **extra):
    return {"title": title, "artistName": artist, "duration": duration,
            "subtitleLinks": [{"title": artist}],
            "contentDescriptor": {"kind": kind, "identifiers": {"storeAdamID": song_id},
                                  "url": f"https://music.apple.com/us/album/x/1?i={song_id}"}, **extra}


def _apple_html(items, title="Today’s Hits"):
    data = {"data": [{"intent": {}, "data": {"sections": [
        {"itemKind": "containerDetailHeaderLockup", "items": [{"title": title}]},
        {"itemKind": "trackLockup", "items": items},
        {"itemKind": "bubbleLockup", "items": [{"title": "Featured artist"}]},
    ]}}]}
    return f'<html><script type="application/json" id="serialized-server-data">{json.dumps(data)}</script></html>'


def test_apple_adapter_parses_page():
    items = [
        _apple_item("stupid song", "Olivia Rodrigo", "1889992115", 209680),
        _apple_item("Some Episode", "Podcaster", "1", kind="podcastEpisode"),
        _apple_item("Region Locked", "X", "2", isDisabled=True),
        _apple_item("Solar Eclipse", "Drake & Don Toliver", "6818209989", 218389),
    ]
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, text=_apple_html(items))

    res = AppleMusicPublicAdapter(httpx.Client(transport=httpx.MockTransport(handler))).fetch(
        "tr/pl.f4d106fed2bd41149aaacabb233eb5eb")
    assert seen["url"] == "https://music.apple.com/tr/playlist/pl.f4d106fed2bd41149aaacabb233eb5eb"
    assert res.platform == "apple" and res.title == "Today’s Hits"
    assert [(t.title, t.artist, t.source_id) for t in res.tracks] == [
        ("stupid song", "Olivia Rodrigo", "1889992115"), ("Solar Eclipse", "Drake & Don Toliver", "6818209989")]
    assert res.tracks[0].duration_sec == 209.7 and res.tracks[0].original_position == 1
    assert res.tracks[1].original_position == 4 and res.tracks[1].occurrence_id == "item-004"
    assert res.tracks[0].youtube_video_id is None and res.tracks[0].match_status == "pending"
    assert len(res.warnings) == 2


def test_apple_adapter_limit():
    items = [_apple_item(f"Song {i}", "A", str(1000 + i)) for i in range(80)]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=_apple_html(items))))
    assert len(AppleMusicPublicAdapter(client).fetch("us/pl.abcdefgh12345").tracks) == 50


@pytest.mark.parametrize("status,body", [(404, ""), (200, "<html>no data</html>"), (200, _apple_html([]))])
def test_apple_adapter_errors(status, body):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=body)))
    with pytest.raises(PlaylistImportError):
        AppleMusicPublicAdapter(client).fetch("us/pl.abcdefgh12345")


def test_apple_durations_are_used_for_identity_checks():
    from backend.features import match_duration
    from backend.schemas import Track

    t = Track(occurrence_id="item-001", platform="apple", source_id="1", title="x", artist="y",
              duration_sec=210, original_position=1)
    assert match_duration(t) == 210
