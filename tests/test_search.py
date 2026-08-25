"""Live search for the topbar.

Network is mocked throughout: these cover the parts that are ours, which are
the extractor allowlists, the URL normalisation, and the duration verdict. What
YouTube returns for a given phrase is not something a test can pin down.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api import search as api_search
from app.pipeline import search as search_mod


class _FakeYDL:
    """Captures the options and the target string, returns canned entries."""

    captured: list[tuple[dict, str]] = []
    entries: list[dict] = []

    def __init__(self, opts):
        self._opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, target, download=False):
        _FakeYDL.captured.append((self._opts, target))
        return {"entries": list(_FakeYDL.entries)}


@contextlib.contextmanager
def _limit(seconds: int):
    """Both the pipeline and the API imported get_max_duration_sec by value, so
    each holds its own reference and both have to be patched."""
    with (
        patch.object(search_mod, "get_max_duration_sec", return_value=seconds),
        patch.object(api_search, "get_max_duration_sec", return_value=seconds),
    ):
        yield


@pytest.fixture(autouse=True)
def _fake_ydl(monkeypatch):
    _FakeYDL.captured = []
    _FakeYDL.entries = []
    monkeypatch.setattr(search_mod, "YoutubeDL", _FakeYDL)
    api_search._clear_cache()
    return _FakeYDL


# ── source / kind support ────────────────────────────────────────────


def test_soundcloud_has_no_playlist_search():
    """yt-dlp exposes exactly one SoundCloud search key (scsearch, tracks
    only), so the UI must not offer a tab that can only ever be empty."""
    assert search_mod.supported("soundcloud", "track")
    assert not search_mod.supported("soundcloud", "playlist")
    assert search_mod.supported("youtube", "track")
    assert search_mod.supported("youtube", "playlist")


def test_unsupported_pair_raises():
    with pytest.raises(search_mod.UnsupportedSearch):
        search_mod.search("daft punk", "soundcloud", "playlist")


def test_short_query_raises():
    with pytest.raises(search_mod.UnsupportedSearch):
        search_mod.search("a", "youtube", "track")


def test_unknown_source_raises():
    with pytest.raises(search_mod.UnsupportedSearch):
        search_mod.search("daft punk", "spotify", "track")


# ── the SSRF boundary ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "kind"),
    [("youtube", "track"), ("youtube", "playlist"), ("soundcloud", "track")],
)
def test_generic_extractor_is_never_allowed(source, kind):
    """The whole point of #173. A search must not be the way in."""
    search_mod.search("daft punk", source, kind)
    opts, _ = _FakeYDL.captured[0]
    assert "generic" not in opts["allowed_extractors"]
    assert opts["allowed_extractors"], "a search with no allowlist is unrestricted"


def test_track_search_cannot_reach_the_tab_extractor():
    """Track search has no business expanding a channel or a playlist page."""
    search_mod.search("daft punk", "youtube", "track")
    opts, _ = _FakeYDL.captured[0]
    assert "youtube:tab" not in opts["allowed_extractors"]


def test_search_targets():
    search_mod.search("daft punk", "youtube", "track", limit=5)
    assert _FakeYDL.captured[0][1] == "ytsearch5:daft punk"
    _FakeYDL.captured.clear()
    search_mod.search("daft punk", "soundcloud", "track", limit=3)
    assert _FakeYDL.captured[0][1] == "scsearch3:daft punk"
    _FakeYDL.captured.clear()
    search_mod.search("daft punk", "youtube", "playlist", limit=4)
    target = _FakeYDL.captured[0][1]
    assert target.startswith("https://www.youtube.com/results?search_query=daft%20punk")
    assert "sp=" in target, "without the filter this returns videos, not playlists"


def test_query_is_url_encoded_into_the_search_page():
    search_mod.search("a&b=c d", "youtube", "playlist")
    target = _FakeYDL.captured[0][1]
    assert "a%26b%3Dc%20d" in target


# ── result normalisation ─────────────────────────────────────────────


def test_soundcloud_permalink_is_preferred_over_the_api_url():
    """SoundCloud search returns `url` as an api.soundcloud.com endpoint, which
    is not on the allowlisted host set and is rejected on sight. Reading `url`
    first (as expand_playlist does) silently drops every SoundCloud result."""
    _FakeYDL.entries = [
        {
            "title": "Around the World",
            "duration": 429.0,
            "url": "https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A254112221",
            "webpage_url": "https://soundcloud.com/daftpunkofficial/around-the-world",
        }
    ]
    out = search_mod.search("daft punk", "soundcloud", "track")
    assert len(out["items"]) == 1
    assert out["items"][0]["url"] == "https://soundcloud.com/daftpunkofficial/around-the-world"


def test_youtube_urls_are_normalised():
    _FakeYDL.entries = [
        {"title": "x", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc&t=42"}
    ]
    out = search_mod.search("query", "youtube", "track")
    assert out["items"][0]["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_unvalidatable_entries_are_dropped_not_shown():
    """A result we cannot hand to the pipeline must not be offered."""
    _FakeYDL.entries = [
        {"title": "evil", "url": "https://evil.example.com/watch?v=aaaaaaaaaaa"},
        {"title": "ok", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        {"title": "nothing", "url": None},
        "not even a dict",
    ]
    out = search_mod.search("query", "youtube", "track")
    assert [i["title"] for i in out["items"]] == ["ok"]


def test_playlist_entries_go_through_the_playlist_validator():
    _FakeYDL.entries = [
        {"title": "album", "url": "https://www.youtube.com/playlist?list=PLSdoVPM5Wnnd"},
        {"title": "radio", "url": "https://www.youtube.com/playlist?list=RDdQw4w9WgXcQ"},
    ]
    out = search_mod.search("query", "youtube", "playlist")
    # Radio playlists are endless and viewer-specific, so validate_playlist_url
    # rejects them and they must not appear as importable.
    assert [i["title"] for i in out["items"]] == ["album"]


def test_limit_is_honoured_and_clamped():
    _FakeYDL.entries = [
        {"title": str(i), "url": f"https://www.youtube.com/watch?v=dQw4w9WgXc{i}"}
        for i in range(12)
    ]
    assert len(search_mod.search("query", "youtube", "track", limit=3)["items"]) == 3
    assert (
        len(search_mod.search("query", "youtube", "track", limit=999)["items"])
        <= search_mod.MAX_LIMIT
    )


def test_smallest_usable_thumbnail_is_chosen():
    """The dropdown renders these at 64px. Pulling the 720p variant would cost
    bandwidth and decode time for nothing."""
    _FakeYDL.entries = [
        {
            "title": "x",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "thumbnails": [
                {"url": "tiny.jpg", "width": 48},
                {"url": "small.jpg", "width": 168},
                {"url": "huge.jpg", "width": 1280},
            ],
        }
    ]
    assert search_mod.search("query", "youtube", "track")["items"][0]["thumbnail"] == "small.jpg"


def test_missing_thumbnail_is_none_not_an_error():
    _FakeYDL.entries = [{"title": "x", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}]
    assert search_mod.search("query", "youtube", "track")["items"][0]["thumbnail"] is None


# ── the duration verdict ─────────────────────────────────────────────


def test_over_the_limit_is_flagged():
    """Over the configured limit the pipeline refuses the job outright, so the
    UI has to say "cannot import", not "this may be slow"."""
    _FakeYDL.entries = [
        {"title": "short", "duration": 200, "url": "https://www.youtube.com/watch?v=dQw4w9WgXc1"},
        {"title": "long", "duration": 4275, "url": "https://www.youtube.com/watch?v=dQw4w9WgXc2"},
    ]
    with patch.object(search_mod, "get_max_duration_sec", return_value=1200):
        out = search_mod.search("query", "youtube", "track")
    assert out["max_duration_sec"] == 1200
    assert [i["too_long"] for i in out["items"]] == [False, True]


def test_the_verdict_follows_the_users_configured_limit():
    """Not a hardcoded 20 minutes: the limit is a setting, 1 to 60 minutes."""
    _FakeYDL.entries = [
        {"title": "x", "duration": 2000, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    ]
    with patch.object(search_mod, "get_max_duration_sec", return_value=1200):
        assert search_mod.search("query", "youtube", "track")["items"][0]["too_long"] is True
    with patch.object(search_mod, "get_max_duration_sec", return_value=3600):
        assert search_mod.search("query", "youtube", "track")["items"][0]["too_long"] is False


def test_unknown_duration_is_not_treated_as_too_long():
    """Playlists have no duration. They must not all be marked unimportable."""
    _FakeYDL.entries = [
        {"title": "album", "url": "https://www.youtube.com/playlist?list=PLSdoVPM5Wnnd"}
    ]
    out = search_mod.search("query", "youtube", "playlist")
    assert out["items"][0]["duration"] is None
    assert out["items"][0]["too_long"] is False


# ── the endpoint ─────────────────────────────────────────────────────


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_endpoint_happy_path(client):
    _FakeYDL.entries = [
        {"title": "x", "duration": 200, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    ]
    r = client.post("/api/search", json={"query": "daft punk"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["cached"] is False


def test_endpoint_rejects_a_short_query(client):
    assert client.post("/api/search", json={"query": "a"}).status_code == 422


def test_endpoint_rejects_an_unsupported_pair(client):
    r = client.post(
        "/api/search", json={"query": "daft punk", "source": "soundcloud", "kind": "playlist"}
    )
    assert r.status_code == 422
    assert "no playlist search" in r.json()["detail"]


def test_endpoint_rejects_an_unknown_source(client):
    r = client.post("/api/search", json={"query": "daft punk", "source": "spotify"})
    assert r.status_code == 422


def test_endpoint_rejects_a_silly_limit(client):
    assert client.post("/api/search", json={"query": "qq", "limit": 500}).status_code == 422


def test_endpoint_rejects_invalid_json(client):
    r = client.post(
        "/api/search", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422


def test_a_repeat_query_is_served_from_cache(client):
    """Word-boundary triggering means backspacing walks straight back through
    earlier queries. Those must not each cost a round trip."""
    _FakeYDL.entries = [
        {"title": "x", "duration": 200, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    ]
    assert client.post("/api/search", json={"query": "daft punk"}).json()["cached"] is False
    calls = len(_FakeYDL.captured)
    second = client.post("/api/search", json={"query": "daft punk"}).json()
    assert second["cached"] is True
    assert len(_FakeYDL.captured) == calls, "a cache hit must not reach yt-dlp"
    assert second["items"] == [i for i in second["items"]]


def test_the_cache_key_ignores_case_but_not_source(client):
    _FakeYDL.entries = [
        {"title": "x", "duration": 200, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    ]
    client.post("/api/search", json={"query": "Daft Punk"})
    assert client.post("/api/search", json={"query": "daft punk"}).json()["cached"] is True
    # Same words, different service: must not be served the YouTube results.
    assert (
        client.post("/api/search", json={"query": "daft punk", "source": "soundcloud"}).json()[
            "cached"
        ]
        is False
    )


def test_an_expired_entry_is_not_served(client, monkeypatch):
    _FakeYDL.entries = [
        {"title": "x", "duration": 200, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    ]
    client.post("/api/search", json={"query": "daft punk"})
    monkeypatch.setattr(api_search, "_CACHE_TTL_SEC", -1.0)
    api_search._clear_cache()
    assert client.post("/api/search", json={"query": "daft punk"}).json()["cached"] is False


def test_the_cache_is_bounded(client):
    """A long session of typing must not grow the cache without limit."""
    _FakeYDL.entries = [
        {"title": "x", "duration": 200, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    ]
    for i in range(api_search._CACHE_MAX + 20):
        client.post("/api/search", json={"query": f"query number {i}"})
    assert len(api_search._cache) <= api_search._CACHE_MAX


def test_raising_the_duration_limit_invalidates_the_cache(client):
    """too_long is computed against a live setting. A cached verdict that
    outlives a Settings change leaves rows greyed out and unclickable for up to
    a minute after the user has explicitly allowed them."""
    _FakeYDL.entries = [
        {"title": "long", "duration": 2000, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    ]
    with _limit(1200):
        first = client.post("/api/search", json={"query": "daft punk"}).json()
    assert first["items"][0]["too_long"] is True

    with _limit(3600):
        second = client.post("/api/search", json={"query": "daft punk"}).json()
    assert second["cached"] is False, "the old verdict was served from cache"
    assert second["items"][0]["too_long"] is False


def test_the_same_limit_still_caches(client):
    """The key gained a field; it must not have stopped caching altogether."""
    _FakeYDL.entries = [
        {"title": "x", "duration": 200, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    ]
    with _limit(1200):
        client.post("/api/search", json={"query": "daft punk"})
        assert client.post("/api/search", json={"query": "daft punk"}).json()["cached"] is True


def test_a_backend_failure_is_a_502_not_a_stack_trace(client):
    def boom(*_a, **_k):
        raise RuntimeError("ERROR: [youtube] Sign in to confirm you're not a bot")

    with patch.object(api_search, "search", boom):
        r = client.post("/api/search", json={"query": "daft punk"})
    assert r.status_code == 502
    assert r.json()["detail"] == "Could not reach that service"
    assert "bot" not in r.text, "the upstream message must not leak to the client"


def test_sources_endpoint_reports_what_is_actually_searchable(client):
    body = client.get("/api/search/sources").json()
    by_source = {s["source"]: s["kinds"] for s in body["sources"]}
    assert by_source["youtube"] == ["track", "playlist"]
    assert by_source["soundcloud"] == ["track"]
