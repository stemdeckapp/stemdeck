from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.models import Job, JobCancelled
from app.core.registry import _jobs
from app.pipeline.runner import _extract_video_track, run_local_pipeline, run_pipeline


def _ffmpeg_available() -> bool:
    import shutil

    return shutil.which("ffmpeg") is not None


@pytest.mark.asyncio
async def test_pipeline_transitions_to_error_on_stage_failure(tmp_path: Path):
    job = Job(id="abcdefabcdef")

    def boom(*args, **kwargs):
        raise RuntimeError("download blew up")

    with patch("app.pipeline.runner._run_blocking", side_effect=boom):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "error"
    assert job.error  # generic message returned to client; detail is in server logs


@pytest.mark.asyncio
async def test_pipeline_marks_done_on_success(tmp_path: Path):
    job = Job(id="abcdefabcdee")

    with patch("app.pipeline.runner._run_blocking", return_value=None):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "done"
    assert job.progress == 1.0


@pytest.mark.asyncio
async def test_pipeline_handles_jobcancelled(tmp_path: Path):
    job = Job(id="abcdefabcdec")
    job.cancel_requested = True

    def cancel(*args, **kwargs):
        raise JobCancelled()

    with patch("app.pipeline.runner._run_blocking", side_effect=cancel):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "cancelled"
    # Partial job dir is removed.
    assert not (tmp_path / job.id).exists()


@pytest.mark.asyncio
async def test_pipeline_handles_wrapped_cancel(tmp_path: Path):
    """yt-dlp wraps hook exceptions in DownloadError; the runner must still
    treat it as a cancel when the flag is set."""
    job = Job(id="abcdefabcdeb")
    job.cancel_requested = True

    def wrapped(*args, **kwargs):
        raise RuntimeError("yt-dlp DownloadError wrapping JobCancelled")

    with patch("app.pipeline.runner._run_blocking", side_effect=wrapped):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "cancelled"


@pytest.mark.asyncio
async def test_pipeline_recovers_from_mkdir_failure(tmp_path: Path):
    """If something pre-lock raises, the job must transition to error
    instead of staying stuck on `queued`."""
    job = Job(id="abcdefabcdea")
    bad_jobs_dir = tmp_path / "blocked"
    # Make jobs_dir a regular file so mkdir(parents=True) under it raises.
    bad_jobs_dir.write_bytes(b"not a directory")

    await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", bad_jobs_dir)

    assert job.status == "error"


@pytest.mark.asyncio
async def test_pipeline_error_cleans_up_job_dir(tmp_path: Path):
    """#82: failed pipeline must remove the job directory so no orphan is left."""
    job = Job(id="abcdefabcde9")

    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg died")

    with patch("app.pipeline.runner._run_blocking", side_effect=boom):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "error"
    assert not (tmp_path / job.id).exists(), "job dir should be removed on error"


@pytest.mark.asyncio
async def test_pipeline_error_calls_persist(tmp_path: Path):
    """#83: persist is called after an error so the registry stays consistent."""
    job = Job(id="abcdefabcde8")
    _jobs[job.id] = job
    persist_calls = []

    def boom(*args, **kwargs):
        raise RuntimeError("separated badly")

    def fake_persist(jobs_dir):
        persist_calls.append(jobs_dir)

    with (
        patch("app.pipeline.runner._run_blocking", side_effect=boom),
        patch("app.pipeline.runner.persist_registry", side_effect=fake_persist),
    ):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "error"
    assert len(persist_calls) == 1


@pytest.mark.asyncio
async def test_local_pipeline_error_cleans_up_job_dir(tmp_path: Path):
    """#82: local upload error path also removes the job directory."""
    job = Job(id="abcdefabcde7")
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True)
    source = job_dir / "source.mp3"
    source.write_bytes(b"ID3")

    def boom(*args, **kwargs):
        raise RuntimeError("demucs blew up")

    with patch("app.pipeline.runner._run_local_blocking", side_effect=boom):
        await run_local_pipeline(job, source, tmp_path)

    assert job.status == "error"
    assert not (tmp_path / job.id).exists(), "job dir should be removed on local error"


@pytest.mark.asyncio
async def test_pipeline_error_quarantines_evidence(tmp_path: Path):
    """#277: a failed job's dir moves to jobs/failed/<id> with error.txt
    (device, cause, stderr tail) and the heavy audio payloads stripped."""
    from app.pipeline.errors import SeparationError

    job = Job(id="abcdefabcde6")
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)
    (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF" + b"\x00" * 64)
    (job_dir / "source.wav").write_bytes(b"RIFF" + b"\x00" * 64)
    source = job_dir / "source.wav"
    job.stage_timings = {"download": 1.2}

    def boom(*args, **kwargs):
        raise SeparationError(
            "demucs failed: MPS backend out of memory",
            tail=["progress 50%", "RuntimeError: MPS backend out of memory"],
            device="mps",
        )

    with patch("app.pipeline.runner._run_local_blocking", side_effect=boom):
        await run_local_pipeline(job, source, tmp_path)

    assert job.status == "error"
    assert job.error_detail is not None
    assert job.error_detail.startswith("out-of-memory")
    # Original dir gone; quarantine holds error.txt but no audio payloads.
    assert not job_dir.exists()
    quarantined = tmp_path / "failed" / job.id
    report = (quarantined / "error.txt").read_text(encoding="utf-8")
    assert "device: mps" in report
    assert "cause: out-of-memory" in report
    assert "MPS backend out of memory" in report
    assert '"download": 1.2' in report
    assert not (quarantined / "source.wav").exists()
    assert not (quarantined / "stems").exists()


@pytest.mark.asyncio
async def test_pipeline_success_logs_timing_summary(tmp_path: Path, caplog):
    """#293: successful jobs emit a one-line stage-timing summary."""
    import logging

    job = Job(id="abcdefabcde5")

    def fake_stages(j, url, job_dir):
        j.stage_timings = {"download": 2.0, "analyze": 1.0, "separate": 30.0, "post": 3.5}
        j.compute_device = "cpu"

    with (
        patch("app.pipeline.runner._run_blocking", side_effect=fake_stages),
        caplog.at_level(logging.INFO, logger="stemdeck.pipeline"),
    ):
        await run_pipeline(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmp_path)

    assert job.status == "done"
    summary = next(r.message for r in caplog.records if "done device=" in r.message)
    assert "device=cpu" in summary
    assert "separate=30.0s" in summary
    assert "total=36.5s" in summary
    # Timings + device persist into metadata.json for later diagnostics.
    import json as _json

    meta = _json.loads((tmp_path / job.id / "metadata.json").read_text(encoding="utf-8"))
    assert meta["compute_device"] == "cpu"
    assert meta["stage_timings"]["separate"] == 30.0


def test_extract_video_track_from_mp4(tmp_path: Path):
    """#219: an mp4 with a video stream yields video.mp4 and sets has_video."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not available")
    import subprocess

    job = Job(id="vid000000001")
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True)
    source = job_dir / "source.mp4"
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
            "color=c=black:s=64x64:d=0.3:r=10",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        timeout=30,
    )

    _extract_video_track(job, source, job_dir)

    assert job.has_video is True
    assert (job_dir / "video.mp4").is_file()
    assert (job_dir / "video.mp4").stat().st_size > 0


def test_extract_video_track_audio_only_mp4(tmp_path: Path):
    """An mp4 with no video stream leaves has_video false and no video.mp4."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not available")
    import subprocess

    job = Job(id="vid000000002")
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True)
    source = job_dir / "source.mp4"
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
            "anullsrc=r=44100:cl=stereo:d=0.3",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        timeout=30,
    )

    _extract_video_track(job, source, job_dir)

    assert job.has_video is False
    assert not (job_dir / "video.mp4").exists()
