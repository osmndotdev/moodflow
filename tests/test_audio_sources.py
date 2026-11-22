from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

from backend import audio_sources
from backend.audio_sources import AudioSourceError, fetch_audio, find_youtube_match, validate_video_id
from backend.schemas import Track


def _spotify_track(title="Take On Me", artist="a-ha", duration=225):
    return Track(occurrence_id="item-001", platform="spotify", source_id="x" * 22, title=title,
                 artist=artist, duration_sec=duration, original_position=1)


def search_ydl(entries):
    class Y:
        calls = []

        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, query, download=False):
            Y.calls.append(query)
            return {"entries": entries}
    return Y


@pytest.mark.parametrize("vid", ["", "short", "djV11Xbc914&x", "../../etc/pa", "djV11Xbc914x"])
def test_validate_video_id_rejects(vid):
    with pytest.raises(AudioSourceError):
        validate_video_id(vid)


def test_match_picks_official_audio_over_cover():
    Y = search_ydl([
        {"id": "cover000001", "title": "Take On Me (Acoustic Cover)", "channel": "Random Guy", "duration": 230},
        {"id": "djV11Xbc914", "title": "a-ha - Take On Me (Official Audio)", "channel": "a-ha", "duration": 226},
        {"id": "live0000001", "title": "a-ha - Take On Me (Live at Wembley)", "channel": "a-ha", "duration": 300},
    ])
    vid, score = find_youtube_match(_spotify_track(), ydl_cls=Y)
    assert vid == "djV11Xbc914" and score >= audio_sources.MATCH_THRESHOLD
    assert Y.calls == ["ytsearch3:a-ha - Take On Me audio"]


def test_wrong_match_is_not_used():
    Y = search_ydl([
        {"id": "aaaaaaaaaaa", "title": "Completely Different Song", "channel": "Someone", "duration": 200},
        {"id": "bbbbbbbbbbb", "title": "Take On Me (slowed + reverb)", "channel": "lofi edits", "duration": 320},
    ])
    vid, _ = find_youtube_match(_spotify_track(), ydl_cls=Y)
    assert vid is None


def test_youtube_items_skip_search():
    Y = search_ydl([])
    t = _spotify_track()
    t.youtube_video_id = "djV11Xbc914"
    assert find_youtube_match(t, ydl_cls=Y) == ("djV11Xbc914", 1.0)
    assert Y.calls == []


def download_ydl(fail: bool = False):
    class Y:
        downloads = 0

        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            Y.downloads += 1
            assert self.opts["noplaylist"] and self.opts["postprocessors"][0]["preferredcodec"] == "wav"
            if fail:
                raise DownloadError("ERROR: Video unavailable")
            vid = urls[0].rsplit("=", 1)[-1]
            Path(self.opts["outtmpl"].replace("%(ext)s", "wav").replace(f"{vid}.", f"{vid}.", 1)).write_bytes(b"RIFF....")
    return Y


def test_fetch_audio_downloads_once_and_caches(tmp_path):
    Y = download_ydl()
    p1 = fetch_audio("djV11Xbc914", tmp_path, ydl_cls=Y)
    p2 = fetch_audio("djV11Xbc914", tmp_path, ydl_cls=Y)
    assert p1 == p2 == tmp_path / "djV11Xbc914.wav"
    assert Y.downloads == 1


def test_fetch_audio_failure_raises_friendly_error(tmp_path):
    with pytest.raises(AudioSourceError, match="erişilemiyor"):
        fetch_audio("djV11Xbc914", tmp_path, ydl_cls=download_ydl(fail=True))


def test_fetch_audio_rejects_bad_id_before_network(tmp_path):
    Y = download_ydl()
    with pytest.raises(AudioSourceError):
        fetch_audio("not-an-id", tmp_path, ydl_cls=Y)
    assert Y.downloads == 0
