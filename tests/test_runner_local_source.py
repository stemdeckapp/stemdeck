"""Preparing a local upload before it reaches Demucs.

_prepare_local_source had no coverage. It is the whole reason an uploaded file
works at all: Demucs processes a 24-bit, 32-bit-float, high-sample-rate or
multi-channel WAV *silently* and outputs silence, so the normalisation here is
the difference between a working import and six empty stems with no error
anywhere. That failure mode is invisible to every other test in the suite,
because nothing else listens to the output.

Driven against real ffmpeg, since the point is what the transcode actually
produces rather than which arguments were assembled.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.core.models import Job
from app.pipeline import runner


@pytest.fixture
def job():
    return Job(id="abcdefabcdef")


@pytest.fixture
def job_dir(tmp_path):
    d = tmp_path / "abcdefabcdef"
    d.mkdir()
    return d


def _probe(path: Path, entries: str) -> str:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            entries,
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _write_mp4(path: Path, seconds=0.4):
    """A real mp4 carrying a video stream and an audio track, so the .mp4 branch
    is exercised against the container it is actually about."""
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=64x64:rate=10:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _write(path: Path, *, rate=44100, channels=2, subtype="PCM_16", seconds=0.4):
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    tone = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    data = np.column_stack([tone] * channels) if channels > 1 else tone
    sf.write(str(path), data, rate, subtype=subtype)
    return path


# --------------------------------------------------------------------------
# the formats Demucs would otherwise turn into silence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw, why",
    [
        ({"subtype": "PCM_24"}, "24-bit"),
        ({"subtype": "FLOAT"}, "32-bit float"),
        ({"rate": 48000}, "a non-44.1k sample rate"),
        ({"rate": 96000}, "a high sample rate"),
        ({"channels": 1}, "mono"),
        ({"channels": 4}, "more channels than stereo"),
    ],
)
def test_an_upload_is_normalised_to_what_demucs_expects(job, job_dir, tmp_path, kw, why):
    src = _write(tmp_path / "upload.wav", **kw)

    dest = runner._prepare_local_source(job, src, job_dir)

    assert dest == job_dir / "source.wav"
    assert _probe(dest, "stream=sample_rate") == "44100", why
    assert _probe(dest, "stream=channels") == "2", why
    assert _probe(dest, "stream=sample_fmt") == "s16", why


def test_the_audio_survives_the_transcode(job, job_dir, tmp_path):
    """Normalising to silence would pass every format assertion above and still
    be the exact bug this function exists to prevent."""
    src = _write(tmp_path / "upload.wav", subtype="FLOAT")

    dest = runner._prepare_local_source(job, src, job_dir)

    data, _ = sf.read(str(dest))
    assert float(np.max(np.abs(data))) > 0.05, "the prepared source is silent"


def test_the_original_upload_is_deleted_once_it_is_transcoded(job, job_dir, tmp_path):
    """The upload is up to 400 MB and nothing reads it again; keeping it would
    double what every local import costs on disk."""
    src = _write(tmp_path / "upload.wav")

    runner._prepare_local_source(job, src, job_dir)

    assert not src.exists()


def test_a_source_already_in_place_is_left_alone(job, job_dir):
    """Re-running the stage must not transcode source.wav onto itself, which
    ffmpeg would refuse and which would delete the only copy."""
    src = _write(job_dir / "source.wav")
    before = src.read_bytes()

    dest = runner._prepare_local_source(job, src, job_dir)

    assert dest == src
    assert src.read_bytes() == before


def test_the_user_is_told_what_is_happening(job, job_dir, tmp_path):
    """Transcoding a long upload takes long enough that a silent UI reads as a
    hang."""
    src = _write(tmp_path / "upload.wav")

    runner._prepare_local_source(job, src, job_dir)

    assert job.stage_message == "Preparing audio..."


def test_a_file_ffmpeg_cannot_read_fails_with_its_reason(job, job_dir, tmp_path):
    """The message reaches the job's error record, so a user who uploaded
    something odd has something to go on."""
    src = tmp_path / "upload.wav"
    src.write_bytes(b"not audio at all")

    with pytest.raises(RuntimeError, match="ffmpeg transcode failed"):
        runner._prepare_local_source(job, src, job_dir)


def test_a_failed_transcode_keeps_the_original(job, job_dir, tmp_path):
    """Deleting the upload on failure would make the error unrecoverable and
    leave nothing to diagnose."""
    src = tmp_path / "upload.wav"
    src.write_bytes(b"not audio at all")

    with pytest.raises(RuntimeError):
        runner._prepare_local_source(job, src, job_dir)

    assert src.exists()


# --------------------------------------------------------------------------
# .mp4 uploads keep their video for the MP4 export
# --------------------------------------------------------------------------


def test_an_mp4_upload_has_its_video_track_preserved(job, job_dir, tmp_path, monkeypatch):
    """has_video drives the "Export Mix (with video)" option; the extraction
    runs before the audio transcode because the transcode deletes the source."""
    src = _write_mp4(tmp_path / "clip.mp4")
    seen = {}

    def _extract(j, source, jd):
        seen["called_with"] = (source, jd)
        assert source.exists(), "the video track is extracted before the source is deleted"

    monkeypatch.setattr(runner, "_extract_video_track", _extract)

    runner._prepare_local_source(job, src, job_dir)

    assert seen["called_with"] == (src, job_dir)


def test_a_non_mp4_upload_never_looks_for_video(job, job_dir, tmp_path, monkeypatch):
    src = _write(tmp_path / "clip.wav")

    def _never(*a, **kw):
        raise AssertionError("video extraction was attempted for a non-mp4 upload")

    monkeypatch.setattr(runner, "_extract_video_track", _never)

    runner._prepare_local_source(job, src, job_dir)


def test_the_extension_check_is_case_insensitive(job, job_dir, tmp_path, monkeypatch):
    """Files off a camera or a Windows share routinely arrive as .MP4."""
    src = _write_mp4(tmp_path / "clip.MP4")
    called = []
    monkeypatch.setattr(runner, "_extract_video_track", lambda *a: called.append(1))

    runner._prepare_local_source(job, src, job_dir)

    assert called, ".MP4 was not recognised as an mp4 upload"


# --------------------------------------------------------------------------
# the two pipeline entry points
# --------------------------------------------------------------------------


def test_a_local_job_is_prepared_rather_than_downloaded(job, job_dir, tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(
        runner, "_prepare_local_source", lambda j, s, d: order.append("prepare") or s
    )
    monkeypatch.setattr(runner, "_run_common", lambda j, s, d: order.append("common"))
    monkeypatch.setattr(
        runner, "download", lambda *a: (_ for _ in ()).throw(AssertionError("downloaded"))
    )
    src = _write(tmp_path / "upload.wav")

    runner._run_local_blocking(job, src, job_dir)

    assert order == ["prepare", "common"]
    assert "prepare" in job.stage_timings


def test_a_url_job_is_downloaded_rather_than_prepared(job, job_dir, monkeypatch):
    order = []
    monkeypatch.setattr(runner, "download", lambda j, u, d: order.append("download") or Path("x"))
    monkeypatch.setattr(runner, "_run_common", lambda j, s, d: order.append("common"))
    monkeypatch.setattr(
        runner,
        "_prepare_local_source",
        lambda *a: (_ for _ in ()).throw(AssertionError("prepared a url job")),
    )

    runner._run_blocking(job, "https://youtu.be/abc", job_dir)

    assert order == ["download", "common"]
    assert "download" in job.stage_timings


def test_a_job_cancelled_before_it_starts_does_no_work(job, job_dir, monkeypatch):
    """Cancelling while queued must not spend a download or a transcode."""
    from app.core.models import JobCancelled

    job.cancel_requested = True
    monkeypatch.setattr(
        runner, "download", lambda *a: (_ for _ in ()).throw(AssertionError("downloaded"))
    )
    monkeypatch.setattr(
        runner,
        "_prepare_local_source",
        lambda *a: (_ for _ in ()).throw(AssertionError("prepared")),
    )

    with pytest.raises(JobCancelled):
        runner._run_blocking(job, "https://youtu.be/abc", job_dir)
    with pytest.raises(JobCancelled):
        runner._run_local_blocking(job, Path("x"), job_dir)
