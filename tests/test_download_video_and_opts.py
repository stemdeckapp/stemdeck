"""The video-track fetch, the cookie escalation, and the options a portable
build has to add.

video_status exists because has_video alone collapses two different answers
into the same silent absence: a track that never had video and one whose fetch
broke (#436). A user who imported something specifically to export a karaoke
video cannot tell those apart, so the distinction is the feature and each branch
of it is asserted here.

The options half covers what only a portable build sets -- a bundled ffmpeg, a
bundled JS runtime, a cookie file on the retry -- none of which a source
checkout ever reaches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.models import Job, JobCancelled
from app.pipeline import download as dl

VID = "dQw4w9WgXcQ"
WATCH = f"https://www.youtube.com/watch?v={VID}"


@pytest.fixture
def job():
    return Job(id="abcdefabc436")


@pytest.fixture
def job_dir(tmp_path):
    d = tmp_path / "abcdefabc436"
    d.mkdir()
    return d


# --------------------------------------------------------------------------
# base options
# --------------------------------------------------------------------------


def test_a_bundled_ffmpeg_is_pointed_at_explicitly(monkeypatch, tmp_path):
    """Portable builds have no ffmpeg on PATH, and a DASH stream needs one to
    remux."""
    ffmpeg_dir = tmp_path / "ffmpeg"
    ffmpeg_dir.mkdir()
    monkeypatch.setattr(dl, "FFMPEG_DIR", ffmpeg_dir)
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: None)

    opts = dl._base_ydl_opts(["youtube"])

    assert opts["ffmpeg_location"] == str(ffmpeg_dir)


def test_no_ffmpeg_location_when_nothing_is_bundled(monkeypatch, tmp_path):
    """yt-dlp resolves its own from PATH; naming a directory that is not there
    would make it fail instead."""
    monkeypatch.setattr(dl, "FFMPEG_DIR", tmp_path / "absent")
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: None)

    assert "ffmpeg_location" not in dl._base_ydl_opts(["youtube"])


def test_a_cookie_file_is_only_attached_on_an_explicit_retry(monkeypatch, tmp_path):
    """Never on the first attempt: sending cookies unprompted is what #432 was
    about. The escalation is deliberate."""
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# cookies", encoding="utf-8")
    monkeypatch.setattr(dl, "FFMPEG_DIR", tmp_path / "absent")
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: cookies)

    assert "cookiefile" not in dl._base_ydl_opts(["youtube"])
    assert dl._base_ydl_opts(["youtube"], use_cookies=True)["cookiefile"] == cookies


def test_no_cookie_file_on_a_retry_when_none_is_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(dl, "FFMPEG_DIR", tmp_path / "absent")
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: None)

    assert "cookiefile" not in dl._base_ydl_opts(["youtube"], use_cookies=True)


# --------------------------------------------------------------------------
# more link shapes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["/shorts/", "/live/", "/embed/"])
def test_the_other_youtube_url_shapes_resolve_to_the_same_video(prefix):
    """A premiere or creator livestream keeps its /live/ URL once it ends and
    becomes an ordinary VOD -- common for concert and DJ-set recordings."""
    assert dl.normalize_youtube_url(f"https://www.youtube.com{prefix}{VID}") == WATCH


@pytest.mark.parametrize("prefix", ["/shorts/", "/live/", "/embed/"])
def test_one_of_those_shapes_carrying_a_non_id_is_left_alone(prefix):
    """It breaks out rather than trying the remaining prefixes; yt-dlp gets the
    URL as typed and reports its own error."""
    url = f"https://www.youtube.com{prefix}not-a-valid-video-id-at-all"

    assert dl.normalize_youtube_url(url) == url


def test_a_nocookie_embed_resolves_too():
    assert dl.normalize_youtube_url(f"https://www.youtube-nocookie.com/embed/{VID}") == WATCH


# --------------------------------------------------------------------------
# the video track fetch
# --------------------------------------------------------------------------


class _YDL:
    """Drives _download_video_track's one extract_info call."""

    behaviour = "ok"
    written: Path | None = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, url, download=False):
        if _YDL.behaviour == "raise":
            raise RuntimeError("no mp4 stream available")
        if _YDL.behaviour == "ok" and _YDL.written is not None:
            _YDL.written.write_bytes(b"\0" * 64)
        return {}


@pytest.fixture
def video_ydl(monkeypatch, job_dir):
    _YDL.behaviour = "ok"
    _YDL.written = job_dir / "video.mp4"
    monkeypatch.setattr(dl, "YoutubeDL", _YDL)
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: None)
    monkeypatch.setattr(dl, "get_video_max_height", lambda: 720)
    return _YDL


def test_a_preserved_video_track_is_recorded_as_ok(job, job_dir, video_ydl):
    dl._download_video_track(job, WATCH, job_dir)

    assert job.has_video is True
    assert job.video_status == "ok"


def test_a_video_the_source_simply_does_not_have_reads_as_unavailable(job, job_dir, video_ydl):
    """yt-dlp succeeded and wrote nothing -- an audio-only upload. Not something
    worth telling the user about."""
    video_ydl.written = None

    dl._download_video_track(job, WATCH, job_dir)

    assert job.has_video is False
    assert job.video_status == "unavailable"


def test_a_video_fetch_that_broke_reads_as_failed(job, job_dir, video_ydl, caplog):
    """The distinction has_video alone cannot make (#436): this is the case a
    user who wanted a karaoke video needs to know about."""
    video_ydl.behaviour = "raise"

    dl._download_video_track(job, WATCH, job_dir)

    assert job.has_video is False
    assert job.video_status == "failed"
    assert "video track unavailable" in caplog.text


def test_a_partial_leftover_is_removed_so_the_export_sees_nothing(job, job_dir, video_ydl):
    """A truncated or non-mp4 file left behind would be offered as a video
    export and fail at the ffmpeg graph instead."""
    video_ydl.written = None
    (job_dir / "video.webm").write_bytes(b"partial")
    (job_dir / "video.mp4.part").write_bytes(b"partial")

    dl._download_video_track(job, WATCH, job_dir)

    assert not list(job_dir.glob("video.*"))


def test_a_zero_byte_video_is_not_treated_as_a_video(job, job_dir, video_ydl):
    """An empty file is what a cut-off fetch leaves; has_video would otherwise
    offer an export of nothing."""
    video_ydl.written = None
    (job_dir / "video.mp4").write_bytes(b"")

    dl._download_video_track(job, WATCH, job_dir)

    assert job.has_video is False
    assert not (job_dir / "video.mp4").exists()


def test_cancelling_during_the_video_fetch_is_a_cancellation_not_a_failure(job, job_dir, video_ydl):
    """yt-dlp wraps the hook's JobCancelled in its own DownloadError, so the
    flag is what tells the two apart."""
    video_ydl.behaviour = "raise"
    job.cancel_requested = True

    with pytest.raises(JobCancelled):
        dl._download_video_track(job, WATCH, job_dir)


def test_a_cancellation_raised_straight_out_is_re_raised(job, job_dir, monkeypatch):
    """When the hook's exception is not wrapped it must pass through untouched
    rather than being logged as an unavailable video."""

    class _Cancels(_YDL):
        def extract_info(self, url, download=False):
            raise JobCancelled()

    monkeypatch.setattr(dl, "YoutubeDL", _Cancels)
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: None)
    monkeypatch.setattr(dl, "get_video_max_height", lambda: 720)

    with pytest.raises(JobCancelled):
        dl._download_video_track(job, WATCH, job_dir)

    assert job.video_status is None, "a cancelled fetch is not a verdict about the video"


# --------------------------------------------------------------------------
# the cookie escalation carrying over
# --------------------------------------------------------------------------


def test_a_probe_that_needed_cookies_skips_the_round_trip_proving_it_again(
    job, job_dir, monkeypatch
):
    """The bot check lands on the metadata probe. Rediscovering it on the fetch
    costs the user another failed request for the same answer."""
    calls: list[bool] = []

    class _Recording(_YDL):
        def extract_info(self, url, download=False):
            if download:
                calls.append(bool(self.opts.get("cookiefile")))
                (job_dir / "source.webm").write_bytes(b"x")
            return {"title": "t", "duration": 10}

    cookies = job_dir / "cookies.txt"
    cookies.write_text("# cookies", encoding="utf-8")
    monkeypatch.setattr(dl, "YoutubeDL", _Recording)
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: cookies)
    monkeypatch.setattr(dl, "get_video_max_height", lambda: 720)
    # The probe reports that it had to escalate.
    monkeypatch.setattr(dl, "_with_cookie_fallback", lambda j, fn, what: (fn(True), True))

    dl.download(job, WATCH, job_dir)

    assert calls, "the fetch never ran"
    assert calls[0] is True, "the fetch re-discovered the cookie requirement"
