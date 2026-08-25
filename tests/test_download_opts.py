"""Every YoutubeDL in download.py must be built from the same base (#435).

The four call sites -- playlist expansion, the metadata probe, the audio fetch
and the MP4 video fetch -- used to build their options independently and had
already drifted. These tests capture what each one actually hands to
YoutubeDL, so an option added to fix one caller can't silently miss the rest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.models import Job
from app.pipeline import download as dl_mod


class _FakeYDL:
    """Records the options dict and returns just enough to get through."""

    captured: list[dict] = []

    def __init__(self, opts):
        _FakeYDL.captured.append(opts)
        self._opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, url, download=False):
        return {"title": "t", "duration": 10, "entries": []}


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    _FakeYDL.captured = []
    monkeypatch.setattr(dl_mod, "YoutubeDL", _FakeYDL)
    # Nothing bundled by default, so the base carries no runtime/cookie keys
    # unless a test opts in.
    monkeypatch.setattr(dl_mod, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl_mod, "get_cookies_file", lambda: None)
    return _FakeYDL


def _all_call_sites(tmp_path: Path) -> list[dict]:
    """Drive every YoutubeDL construction in the module once."""
    _FakeYDL.captured = []
    dl_mod.expand_playlist("https://www.youtube.com/playlist?list=PL123", 10)
    job = Job(id="abcdefabc435")
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "source.webm").write_bytes(b"x")
    dl_mod.download(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", job_dir)
    return list(_FakeYDL.captured)


def test_every_call_site_is_covered(tmp_path):
    """Playlist, probe, audio fetch, video fetch: four constructions."""
    opts = _all_call_sites(tmp_path)
    assert len(opts) == 4, f"expected 4 YoutubeDL constructions, saw {len(opts)}"


def test_ssrf_allowlist_is_set_everywhere(tmp_path):
    """The extractor allowlist is the #173 SSRF boundary. No call site may
    omit it, and none may fall back to the permissive playlist list."""
    opts = _all_call_sites(tmp_path)
    for o in opts:
        assert o.get("allowed_extractors"), "a call site has no extractor allowlist"
    # Only the playlist expansion may use the wider list.
    wide = [o for o in opts if o["allowed_extractors"] is dl_mod._ALLOWED_PLAYLIST_EXTRACTORS]
    assert len(wide) == 1


def test_socket_timeout_is_set_everywhere(tmp_path):
    """#279: a stalled TCP connection must not hang a job at any call site."""
    for o in _all_call_sites(tmp_path):
        assert o.get("socket_timeout") == dl_mod._SOCKET_TIMEOUT_SEC


def test_cookies_reach_every_call_site(tmp_path, monkeypatch):
    """The bot check hits the probe first, so a cookie file that only reached
    the audio fetch would never be used (#432)."""
    monkeypatch.setattr(dl_mod, "get_cookies_file", lambda: "/tmp/cookies.txt")
    for o in _all_call_sites(tmp_path):
        assert o.get("cookiefile") == "/tmp/cookies.txt"


def test_no_cookie_key_when_unset(tmp_path):
    """Absent, not empty-string: yt-dlp treats a falsy cookiefile differently
    from an unset one, and the default must be a plain unauthenticated fetch."""
    for o in _all_call_sites(tmp_path):
        assert "cookiefile" not in o


def test_js_runtime_reaches_every_call_site(tmp_path, monkeypatch):
    """Format resolution happens in the probe and both fetches, so the solver
    has to be configured for all of them (#432)."""
    monkeypatch.setattr(dl_mod, "bundled_js_runtime", lambda: ("quickjs", Path("/opt/js/qjs")))
    for o in _all_call_sites(tmp_path):
        assert "quickjs" in o.get("js_runtimes", {})


def test_no_js_runtime_key_when_nothing_bundled(tmp_path):
    """Docker and source checkouts resolve a runtime from PATH; passing an
    empty dict would clear yt-dlp's own defaults instead."""
    for o in _all_call_sites(tmp_path):
        assert "js_runtimes" not in o


def test_per_call_options_do_not_clobber_the_base(tmp_path):
    """The base is spread first so a caller can layer on top of it. That also
    means a caller could overwrite a security option by accident -- this is
    the guard that it hasn't happened."""
    opts = _all_call_sites(tmp_path)
    for o in opts:
        assert o["socket_timeout"] == dl_mod._SOCKET_TIMEOUT_SEC
        assert o["allowed_extractors"] in (
            dl_mod._ALLOWED_EXTRACTORS,
            dl_mod._ALLOWED_PLAYLIST_EXTRACTORS,
        )
