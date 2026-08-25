"""A failed video fetch has to be distinguishable from a source with no video (#436).

`_download_video_track` and `_extract_video_track` are both best-effort by
design: the audio pipeline must not fail because a video stream could not be
had. But collapsing every outcome into `has_video = False` meant a user who
imported a track specifically to export a karaoke video could not tell "this
never had video" from "the video fetch broke", and nothing surfaced either.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from app.core.models import Job
from app.pipeline import download as dl_mod
from app.pipeline import runner as runner_mod


class _FakeYDL:
    """Writes a video file, or raises, depending on `behaviour`."""

    behaviour = "ok"
    job_dir: Path

    def __init__(self, opts):
        self._opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, url, download=False):
        if _FakeYDL.behaviour == "raise":
            raise RuntimeError("ERROR: [youtube] abc: Requested format is not available")
        if _FakeYDL.behaviour == "ok":
            (_FakeYDL.job_dir / "video.mp4").write_bytes(b"fake mp4 payload")
        return {}


def _run_video_fetch(tmp_path: Path, behaviour: str) -> Job:
    job = Job(id="abcdefabc436")
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    _FakeYDL.behaviour = behaviour
    _FakeYDL.job_dir = job_dir
    with patch.object(dl_mod, "YoutubeDL", _FakeYDL):
        dl_mod._download_video_track(job, "https://www.youtube.com/watch?v=x", job_dir)
    return job


def test_youtube_video_ok(tmp_path):
    job = _run_video_fetch(tmp_path, "ok")
    assert job.has_video is True
    assert job.video_status == "ok"


def test_youtube_no_video_offered_is_not_a_failure(tmp_path):
    """yt-dlp returned cleanly with nothing to save. Normal, not news."""
    job = _run_video_fetch(tmp_path, "nofile")
    assert job.has_video is False
    assert job.video_status == "unavailable"


def test_youtube_video_fetch_failure_is_recorded(tmp_path):
    """The case the user was never told about."""
    job = _run_video_fetch(tmp_path, "raise")
    assert job.has_video is False
    assert job.video_status == "failed"


def test_a_video_failure_never_fails_the_job(tmp_path):
    """Best-effort stays best-effort: recording the reason must not start
    raising where the old code swallowed."""
    job = _run_video_fetch(tmp_path, "raise")
    assert job.status != "error"


def test_video_status_reaches_the_client(tmp_path):
    job = _run_video_fetch(tmp_path, "raise")
    assert job.to_state()["video_status"] == "failed"


def test_default_is_none_when_video_was_never_attempted():
    """SoundCloud and non-mp4 uploads never try, so there is nothing to say."""
    assert Job(id="abcdefabc999").video_status is None
    assert Job(id="abcdefabc999").to_state()["video_status"] is None


def _run_local_extract(tmp_path: Path, returncode: int, raises=None) -> Job:
    job = Job(id="abcdefabc437")
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    source = job_dir / "in.mp4"
    source.write_bytes(b"x")

    def fake_run(cmd, **kwargs):
        if raises is not None:
            raise raises
        if returncode == 0:
            Path(cmd[-1]).write_bytes(b"fake mp4 payload")
        return subprocess.CompletedProcess(cmd, returncode, b"", b"")

    with patch.object(runner_mod.subprocess, "run", fake_run):
        runner_mod._extract_video_track(job, source, job_dir)
    return job


def test_local_mp4_with_video(tmp_path):
    job = _run_local_extract(tmp_path, 0)
    assert job.has_video is True
    assert job.video_status == "ok"


def test_local_mp4_without_a_video_stream(tmp_path):
    """An audio-only .mp4 container. Expected, so not reported as a failure."""
    job = _run_local_extract(tmp_path, 1)
    assert job.has_video is False
    assert job.video_status == "unavailable"


def test_local_extract_ffmpeg_missing_is_a_failure(tmp_path):
    """ffmpeg absent is a broken install, not a property of the upload."""
    job = _run_local_extract(tmp_path, 0, raises=OSError("ffmpeg not found"))
    assert job.has_video is False
    assert job.video_status == "failed"


def test_local_extract_timeout_is_a_failure(tmp_path):
    job = _run_local_extract(tmp_path, 0, raises=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1))
    assert job.video_status == "failed"
