"""Audition a search result before committing minutes of separation to it.

The stream is proxied rather than handed to the page. `media-src 'self' blob:
data:` in the CSP blocks a googlevideo.com URL in an <audio> tag, and widening
that would let any injected string in the webview pull media from anywhere. So
the browser never talks to YouTube: it talks to us, and we fetch a URL that
yt-dlp resolved from an already-allowlisted page.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api import search as api_search
from app.pipeline import preview as preview_mod

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

AUDIO_LOW = {
    "url": "https://rr1.googlevideo.com/low",
    "protocol": "https",
    "acodec": "opus",
    "vcodec": "none",
    "abr": 50,
    "ext": "webm",
    "filesize": 1231355,
    "http_headers": {"User-Agent": "yt-dlp", "Accept": "*/*", "Empty": ""},
}
AUDIO_HIGH = {**AUDIO_LOW, "url": "https://rr1.googlevideo.com/high", "abr": 160}
VIDEO = {
    "url": "https://rr1.googlevideo.com/video",
    "protocol": "https",
    "acodec": "mp4a",
    "vcodec": "avc1",
    "abr": 10,
    "ext": "mp4",
}
HLS = {
    "url": "https://rr1.googlevideo.com/hls.m3u8",
    "protocol": "m3u8_native",
    "acodec": "mp4a",
    "vcodec": "none",
    "abr": 1,
    "ext": "mp4",
}


class _FakeYDL:
    formats: list[dict] = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, url, download=False):
        return {"title": "A Song", "duration": 213, "formats": list(_FakeYDL.formats)}


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    _FakeYDL.formats = [VIDEO, AUDIO_HIGH, AUDIO_LOW, HLS]
    monkeypatch.setattr(preview_mod, "YoutubeDL", _FakeYDL)
    preview_mod._clear_cache()


def test_the_cheapest_audio_only_stream_wins():
    """This is an audition, not a bounce. The lowest bitrate audio-only stream
    is a fraction of the bytes and tells the user what they need to know."""
    assert preview_mod.resolve(URL)["url"] == AUDIO_LOW["url"]


def test_video_formats_are_refused():
    """A progressive video format would drag the picture down the wire to play
    a sound."""
    _FakeYDL.formats = [VIDEO]
    with pytest.raises(preview_mod.PreviewUnavailable):
        preview_mod.resolve(URL)


def test_hls_is_refused():
    """m3u8 needs a manifest rewrite and a segment proxy. Out of scope for an
    audition button, and silently serving a manifest as audio would just fail
    in the player instead."""
    _FakeYDL.formats = [HLS]
    with pytest.raises(preview_mod.PreviewUnavailable):
        preview_mod.resolve(URL)


def test_formats_without_a_url_are_skipped():
    _FakeYDL.formats = [{**AUDIO_LOW, "url": None}, AUDIO_HIGH]
    assert preview_mod.resolve(URL)["url"] == AUDIO_HIGH["url"]


def test_negotiated_headers_are_carried_and_blanks_dropped():
    """A googlevideo URL fetched without the headers yt-dlp negotiated is
    frequently refused."""
    headers = preview_mod.resolve(URL)["headers"]
    assert headers["User-Agent"] == "yt-dlp"
    assert "Empty" not in headers


def test_mime_follows_the_container():
    assert preview_mod.resolve(URL)["mime"] == "audio/webm"
    preview_mod._clear_cache()
    _FakeYDL.formats = [{**AUDIO_LOW, "ext": "m4a"}]
    assert preview_mod.resolve(URL)["mime"] == "audio/mp4"


def test_resolution_is_cached():
    """Resolving is a real extraction, one to two seconds. Pressing play twice
    must not pay for it twice."""
    calls = []
    real = _FakeYDL.extract_info

    def counted(self, url, download=False):
        calls.append(url)
        return real(self, url, download)

    with patch.object(_FakeYDL, "extract_info", counted):
        preview_mod.resolve(URL)
        preview_mod.resolve(URL)
    assert len(calls) == 1


def test_an_expired_entry_is_refetched(monkeypatch):
    preview_mod.resolve(URL)
    monkeypatch.setattr(preview_mod, "_TTL_SEC", -1)
    preview_mod._clear_cache()
    _FakeYDL.formats = [AUDIO_HIGH]
    assert preview_mod.resolve(URL)["url"] == AUDIO_HIGH["url"]


def test_the_cache_is_bounded():
    for i in range(preview_mod._MAX_ENTRIES + 10):
        preview_mod.resolve(f"https://www.youtube.com/watch?v=dQw4w9WgXc{i:02d}"[:43])
    assert len(preview_mod._cache) <= preview_mod._MAX_ENTRIES


def test_a_url_outside_the_allowlist_never_reaches_yt_dlp():
    """The proxy is the one place that fetches an arbitrary host, so the URL it
    is asked about has to clear the #173 boundary first."""
    from app.pipeline.download import InvalidYouTubeURL

    with pytest.raises(InvalidYouTubeURL):
        preview_mod.resolve("https://evil.example.com/track.mp3")


# ── endpoint ─────────────────────────────────────────────────────────


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_endpoint_rejects_a_foreign_host(client):
    r = client.get("/api/search/preview", params={"url": "https://evil.example.com/a.mp3"})
    assert r.status_code == 422


def test_endpoint_404s_when_nothing_is_playable(client):
    _FakeYDL.formats = [HLS]
    r = client.get("/api/search/preview", params={"url": URL})
    assert r.status_code == 404
    assert "No preview" in r.json()["detail"]


def test_endpoint_does_not_leak_upstream_errors(client):
    def boom(_url):
        raise RuntimeError("googlevideo said 403 for token abc123")

    with patch.object(api_search, "resolve_preview", boom):
        r = client.get("/api/search/preview", params={"url": URL})
    assert r.status_code == 502
    assert "abc123" not in r.text
    assert r.json()["detail"] == "Could not load a preview"


def test_range_is_forwarded_upstream(client):
    """Seeking the time bar must cost one short request, not a re-download."""
    seen = {}

    class FakeUpstream:
        status = 206
        headers = {"Content-Range": "bytes 100-199/1000", "Content-Length": "100"}

        def read(self, _n):
            return b""

        def close(self):
            pass

    def fake_urlopen(req, timeout=None):
        seen["range"] = req.get_header("Range")
        seen["ua"] = req.get_header("User-agent")
        return FakeUpstream()

    with patch.object(api_search.urllib.request, "urlopen", fake_urlopen):
        r = client.get(
            "/api/search/preview", params={"url": URL}, headers={"Range": "bytes=100-199"}
        )
    assert r.status_code == 206
    assert seen["range"] == "bytes=100-199"
    assert seen["ua"] == "yt-dlp", "yt-dlp's negotiated headers were dropped"
    assert r.headers["content-range"] == "bytes 100-199/1000"
    assert r.headers["accept-ranges"] == "bytes"
