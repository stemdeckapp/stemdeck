from __future__ import annotations

import asyncio
import contextlib
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.models import Job, JobCancelled
from app.core.registry import _jobs
from app.pipeline.runner import (
    _extract_video_track,
    _presence_from_rms,
    _run_common,
    _write_metadata,
    run_local_pipeline,
    run_pipeline,
)


def _ffmpeg_available() -> bool:
    # See tests/ffmpeg_probe.py: PATH is not how the app finds ffmpeg.
    from tests.ffmpeg_probe import ffmpeg_available

    return ffmpeg_available()


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
async def test_download_failure_carries_its_message(tmp_path: Path):
    """#434: a yt-dlp failure has no stderr tail (only SeparationError carries
    one), so error_detail used to arrive as the bare word "unknown". It must
    now classify the cause AND carry the message."""
    job = Job(id="abcdefabcde7")
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True)
    source = job_dir / "source.wav"
    source.write_bytes(b"RIFF" + bytes(64))

    def boom(*args, **kwargs):
        raise RuntimeError("ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you're not a bot.")

    with patch("app.pipeline.runner._run_local_blocking", side_effect=boom):
        await run_local_pipeline(job, source, tmp_path)

    assert job.status == "error"
    assert job.error_detail is not None
    assert job.error_detail.startswith("source-blocked")
    assert "Sign in to confirm" in job.error_detail
    assert job.error_detail != "source-blocked"


@pytest.mark.asyncio
async def test_error_detail_stays_bare_when_exception_has_no_message(tmp_path: Path):
    """The message fallback must not append an empty separator: a bare cause is
    correct when there is genuinely nothing to say."""
    job = Job(id="abcdefabcde8")
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True)
    source = job_dir / "source.wav"
    source.write_bytes(b"RIFF" + bytes(64))

    with patch("app.pipeline.runner._run_local_blocking", side_effect=RuntimeError()):
        await run_local_pipeline(job, source, tmp_path)

    assert job.error_detail == "unknown"


@pytest.mark.asyncio
async def test_download_failure_message_is_redacted(tmp_path: Path):
    """error_detail is served to the client and pasted into public reports, so
    the source URL yt-dlp embeds in its errors must not survive."""
    job = Job(id="abcdefabcde9")
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True)
    source = job_dir / "source.wav"
    source.write_bytes(b"RIFF" + bytes(64))

    def boom(*args, **kwargs):
        raise RuntimeError(
            "ERROR: Unable to download https://www.youtube.com/watch?v=dQw4w9WgXcQ: "
            "Requested format is not available"
        )

    with patch("app.pipeline.runner._run_local_blocking", side_effect=boom):
        await run_local_pipeline(job, source, tmp_path)

    assert job.error_detail is not None
    assert job.error_detail.startswith("source-unavailable")
    assert "youtube.com" not in job.error_detail
    assert "dQw4w9WgXcQ" not in job.error_detail


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
    # Full traceback is captured too (#report-full-stack), not just the
    # classified cause/tail -- named after the function that actually raised.
    assert "--- traceback ---" in report
    assert "in boom" in report
    assert "SeparationError" in report


def test_redact_home_strips_the_users_home_directory():
    """A traceback carries absolute paths, and on Windows the Python install
    path alone embeds the reporter's OS username -- this text is headed for a
    public GitHub issue or Discord message, so it must never reach one raw."""
    from app.core.redact import redact

    home = str(Path.home())
    text = f'File "{home}\\AppData\\Local\\Programs\\Python\\Python312\\Lib\\asyncio\\threads.py", line 25'
    redacted = redact(text)
    assert home not in redacted
    assert "<home>" in redacted
    assert "threads.py" in redacted, "the rest of the path must survive -- it's the useful part"


@pytest.mark.asyncio
async def test_quarantine_redacts_a_source_url_embedded_in_the_exception(tmp_path: Path):
    """yt-dlp errors often embed the URL they were fetching in the message
    itself (e.g. "Unsupported URL: <url>") -- exc!r reaching error.txt
    unredacted would leak it even though title:/source: are already excluded
    from the public API response."""
    job = Job(id="abcdefabcde7", source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)
    (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF" + b"\x00" * 64)
    (job_dir / "source.wav").write_bytes(b"RIFF" + b"\x00" * 64)
    source = job_dir / "source.wav"

    def boom(*args, **kwargs):
        raise RuntimeError("Unsupported URL: https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    with patch("app.pipeline.runner._run_local_blocking", side_effect=boom):
        await run_local_pipeline(job, source, tmp_path)

    report = (tmp_path / "failed" / job.id / "error.txt").read_text(encoding="utf-8")
    lines = report.splitlines()
    source_line = next(line for line in lines if line.startswith("source:"))
    exception_line = next(line for line in lines if line.startswith("exception:"))
    # title:/source: are local-only (never served by the /failure API) and
    # keep the real URL, unredacted, for the person looking at their own disk.
    assert source_line == "source: https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    # exception: IS served by the /failure API, and yt-dlp errors often embed
    # the URL they were fetching in the message itself -- must be redacted.
    assert "youtube.com" not in exception_line
    assert "<source-url-redacted>" in exception_line


def _failed_separation_job(tmp_path: Path, source_url: str) -> tuple[Job, Path, Path]:
    """A job whose folder holds a source, partial stems and partial demucs output."""
    job = Job(id="abcdefabcd42", source_url=source_url)
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)
    (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF" + b"\x00" * 64)
    (job_dir / "htdemucs").mkdir()
    source = job_dir / "source.wav"
    source.write_bytes(b"RIFF" + b"upload" * 32)
    return job, job_dir, source


def _worker_died(*args, **kwargs):
    # What separate() raises when the demucs worker's stderr hits EOF: a crash,
    # an out-of-memory kill, or today a clean shutdown killing the worker.
    from app.pipeline.errors import SeparationError

    raise SeparationError("demucs failed: worker exited", tail=["Killed"], device="cpu")


@pytest.mark.asyncio
async def test_failed_upload_keeps_its_source_in_the_quarantine(tmp_path: Path):
    """Decision 6: an upload is the one copy StemDeck has, so a failed separation
    keeps source.* beside error.txt. Stems and demucs output still go."""
    job, job_dir, source = _failed_separation_job(tmp_path, "local:Hollow Veins")
    original = source.read_bytes()

    with patch("app.pipeline.runner._run_local_blocking", side_effect=_worker_died):
        await run_local_pipeline(job, source, tmp_path)

    assert job.status == "error"
    assert not job_dir.exists()
    quarantined = tmp_path / "failed" / job.id
    assert (quarantined / "source.wav").read_bytes() == original
    assert (quarantined / "error.txt").is_file()
    assert not (quarantined / "stems").exists()
    assert not (quarantined / "htdemucs").exists()


@pytest.mark.asyncio
async def test_failed_link_job_still_drops_its_source(tmp_path: Path):
    """A link can be fetched again, so its failure evidence stays KB-scale."""
    job, _job_dir, source = _failed_separation_job(
        tmp_path, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )

    with patch("app.pipeline.runner._run_blocking", side_effect=_worker_died):
        await run_pipeline(job, job.source_url, tmp_path)

    quarantined = tmp_path / "failed" / job.id
    assert (quarantined / "error.txt").is_file()
    assert list(quarantined.glob("source.*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_url", "folder_kept"),
    [("local:Hollow Veins", True), ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", False)],
)
async def test_a_failed_quarantine_never_deletes_an_upload(
    tmp_path: Path, source_url: str, folder_kept: bool
):
    """When the quarantine itself fails (disk full writing error.txt), its
    fallback removes the job folder. An upload's folder is left in place
    instead, its source intact and its stems already stripped, so restore()
    cannot adopt it as a finished song."""
    job, job_dir, source = _failed_separation_job(tmp_path, source_url)
    original = source.read_bytes()

    with (
        patch("app.pipeline.runner._run_local_blocking", side_effect=_worker_died),
        patch("pathlib.Path.write_text", side_effect=OSError(28, "No space left on device")),
    ):
        await run_local_pipeline(job, source, tmp_path)

    assert job.status == "error"
    assert job_dir.exists() is folder_kept
    if folder_kept:
        assert source.read_bytes() == original
        assert not (job_dir / "stems").exists()
        assert not (job_dir / "htdemucs").exists()


@pytest.mark.asyncio
async def test_failed_job_sweep_expires_a_kept_upload(tmp_path: Path, monkeypatch):
    """The kept upload is not kept forever: the hourly failed-job sweep removes
    it FAILED_TTL_SECONDS after the failure (the folder's mtime)."""
    import time
    from types import SimpleNamespace

    from app.pipeline import collect

    job, _job_dir, source = _failed_separation_job(tmp_path, "local:Hollow Veins")
    with patch("app.pipeline.runner._run_local_blocking", side_effect=_worker_died):
        await run_local_pipeline(job, source, tmp_path)
    quarantined = tmp_path / "failed" / job.id
    failed_at = time.time()

    monkeypatch.setattr(
        collect, "time", SimpleNamespace(time=lambda: failed_at + collect.FAILED_TTL_SECONDS - 60)
    )
    collect.sweep_failed_jobs(tmp_path)
    assert (quarantined / "source.wav").is_file()

    monkeypatch.setattr(
        collect, "time", SimpleNamespace(time=lambda: failed_at + collect.FAILED_TTL_SECONDS + 60)
    )
    collect.sweep_failed_jobs(tmp_path)
    assert not quarantined.exists()


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


# ─── #287: presence normalization (moved from analyze.compute_stem_presence) ─


def test_presence_from_rms_normalizes_to_loudest_stem():
    result = _presence_from_rms({"vocals": 0.5, "drums": 0.25, "bass": 0.0})
    assert result == {"vocals": 100, "drums": 50, "bass": 0}


def test_presence_from_rms_empty_input():
    assert _presence_from_rms({}) == {}


def test_presence_from_rms_all_silent():
    assert _presence_from_rms({"vocals": 0.0, "drums": 0.0}) == {"vocals": 0, "drums": 0}


def _common_stage_patches(job_dir: Path, sections):
    section_patch = (
        patch("app.pipeline.runner.detect_sections", side_effect=sections)
        if isinstance(sections, BaseException)
        else patch("app.pipeline.runner.detect_sections", return_value=sections)
    )
    return (
        patch("app.pipeline.runner.analyze"),
        patch("app.pipeline.runner.separate", return_value=job_dir / "model"),
        patch("app.pipeline.runner.collect", return_value=["bass", "drums", "vocals"]),
        patch("app.pipeline.runner.cleanup_source"),
        patch("app.pipeline.runner.make_original_track", return_value=None),
        patch("app.pipeline.runner.make_selected_mix", return_value=None),
        patch("app.pipeline.runner.compute_stem_peaks", return_value={}),
        patch("app.pipeline.runner.compute_beat_grid"),
        section_patch,
    )


@pytest.mark.parametrize(
    ("source_url", "kept"),
    [
        ("local:my song.mp3", True),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", False),
        # No source recorded at all is not an upload, so it is reclaimed as
        # before. Nothing depends on holding it.
        (None, False),
    ],
)
def test_only_a_source_that_can_be_fetched_again_is_deleted(
    tmp_path: Path, source_url: str | None, kept: bool
):
    """An upload's source is the only copy StemDeck will ever have.

    Deleting it is the bulk of disk reclaim per job, which is why it happens
    at all, but a link can be downloaded again and an upload cannot. Throwing
    an upload's source away means the track can never be separated again, with
    any model, and the person who imported it may no longer have the file.
    """
    job = Job(id="abcdefabc120", duration_sec=60.0, source_url=source_url)
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)
    source = job_dir / "source.mp3"
    source.write_bytes(b"ID3")

    patches = _common_stage_patches(job_dir, [])
    # Index 3 is the cleanup_source patch; let the real one run so this
    # asserts on the file rather than on a call count.
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
    ):
        _run_common(job, source, job_dir)

    assert source.exists() is kept


def test_common_pipeline_stores_automatic_section_suggestions(tmp_path: Path):
    job = Job(id="abcdefabc111", duration_sec=60.0, auto_sections=True)
    job_dir = tmp_path / job.id
    stems_dir = job_dir / "stems"
    stems_dir.mkdir(parents=True)
    suggested = [
        {
            "id": "auto-001",
            "name": "Verse",
            "kind": "verse",
            "start": 0.0,
            "end": 60.0,
            "color": "#00c8a0",
        }
    ]

    patches = _common_stage_patches(job_dir, suggested)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8] as detect,
    ):
        _run_common(job, job_dir / "source.wav", job_dir)

    assert job.sections == suggested
    assert job.sections_source == "automatic"
    detect.assert_called_once_with(job, stems_dir, 60.0)
    assert "sections" in (job.stage_timings or {})


def test_common_pipeline_skips_sections_when_the_user_turned_them_off(tmp_path: Path):
    """The toggle must stop the inference pass, not just hide its result.

    The flag is captured on the job when it is created, not read when this
    stage is reached: the stage is the last thing the pipeline does, and the
    toggle clears itself as soon as the user opens another song.
    """
    job = Job(id="abcdefabc116", duration_sec=60.0, auto_sections=False)
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)

    patches = _common_stage_patches(job_dir, [{"id": "auto-001", "kind": "verse"}])
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8] as detect,
    ):
        _run_common(job, job_dir / "source.wav", job_dir)

    detect.assert_not_called()
    assert job.sections is None
    assert job.sections_source is None


def test_common_pipeline_keeps_section_failure_nonfatal(tmp_path: Path, caplog):
    job = Job(id="abcdefabc112", duration_sec=60.0, auto_sections=True)
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)
    patches = _common_stage_patches(job_dir, RuntimeError("model unavailable"))

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        caplog.at_level("ERROR", logger="stemdeck.pipeline"),
    ):
        _run_common(job, job_dir / "source.wav", job_dir)

    assert job.sections is None
    assert "section analysis stage failed" in caplog.text


def test_common_pipeline_preserves_section_cancellation(tmp_path: Path):
    job = Job(id="abcdefabc113", duration_sec=60.0, auto_sections=True)
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)
    patches = _common_stage_patches(job_dir, JobCancelled())

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        pytest.raises(JobCancelled),
    ):
        _run_common(job, job_dir / "source.wav", job_dir)


def test_common_pipeline_never_reanalyzes_existing_manual_sections(tmp_path: Path):
    manual = [{"id": "custom", "name": "Pre-Chorus"}]
    job = Job(
        id="abcdefabc119",
        duration_sec=60.0,
        sections=manual,
        sections_source="manual",
    )
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)
    patches = _common_stage_patches(job_dir, [{"id": "auto-001"}])

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8] as detect,
    ):
        _run_common(job, job_dir / "source.wav", job_dir)

    detect.assert_not_called()
    assert job.sections == manual
    assert job.sections_source == "manual"


def test_metadata_includes_sections_and_source(tmp_path: Path):
    job = Job(
        id="abcdefabc114",
        sections=[{"id": "auto-001"}],
        sections_source="automatic",
    )
    job_dir = tmp_path / job.id
    job_dir.mkdir()

    _write_metadata(job, job_dir)

    import json as _json

    meta = _json.loads((job_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["sections"] == [{"id": "auto-001"}]
    assert meta["sections_source"] == "automatic"


def test_section_flag_is_captured_at_submit_not_at_the_sections_stage(tmp_path: Path):
    """Turning the toggle off mid-import must not rob the running job.

    The sections stage is the last thing the pipeline does, minutes after the
    user pressed the button, and the toggle now clears itself the moment they
    open another song. Reading the setting here would have let an import
    silently lose a pass its owner had already asked and waited for, so the
    answer is the one captured on the job at creation.
    """
    job = Job(id="abcdefabc117", duration_sec=60.0, auto_sections=True)
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)

    suggested = [{"id": "auto-001", "kind": "verse"}]
    patches = _common_stage_patches(job_dir, suggested)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8] as detect,
        # The setting says off, the way it would after the user opened another
        # song while this import was still running.
        patch("app.core.settings.get_auto_sections", return_value=False),
    ):
        _run_common(job, job_dir / "source.wav", job_dir)

    detect.assert_called_once()
    assert job.sections == suggested


@pytest.mark.parametrize(
    ("auto_lyrics", "looks"),
    [(False, False), (True, True), (None, True)],
)
def test_lyrics_stages_run_only_when_detect_lyrics_was_on(tmp_path: Path, auto_lyrics, looks):
    """Detect lyrics off must stop every lyrics stage, not hide their result:
    no LRCLIB lookup, no transcript, no retime. A job made before the setting
    existed (None) looks for lyrics as it always did."""
    from app.pipeline import runner

    job = Job(id="abcdefabc120", duration_sec=60.0, auto_lyrics=auto_lyrics)
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True)
    with (
        patch.object(runner.IdentifyLookup, "start", return_value=None),
        patch.object(runner.LyricsLookup, "start") as lookup,
        patch.object(runner, "_run_common"),
        patch.object(runner, "transcribe_lyrics") as transcribe,
        patch.object(runner, "retime_after_separation") as retime,
    ):
        runner._run_with_band_lookup(job, job_dir / "source.wav", job_dir)
    assert lookup.called is looks
    assert transcribe.called is looks
    assert retime.called is looks


# ─── cleanup after a failed or cancelled job stays off the event loop ───


def _slow_rmtree(monkeypatch) -> threading.Event:
    """Make every rmtree take 0.2 s, the way a multi-GB folder on a NAS does.
    The event is set while one is in progress."""
    real_rmtree = shutil.rmtree
    removing = threading.Event()

    def slow(*args, **kwargs):
        removing.set()
        try:
            time.sleep(0.2)
            return real_rmtree(*args, **kwargs)
        finally:
            removing.clear()

    monkeypatch.setattr(shutil, "rmtree", slow)
    return removing


async def _ticks_while(removing: threading.Event, run) -> int:
    """How often another coroutine got to run while an rmtree was in progress."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            if removing.is_set():
                ticks += 1
            await asyncio.sleep(0.005)

    task = asyncio.create_task(ticker())
    try:
        await run
    finally:
        task.cancel()
    return ticks


async def test_failed_job_quarantine_leaves_the_loop_free(tmp_path: Path, monkeypatch):
    """Every SSE stream and request stalled while a failed job's folder was
    stripped and moved. The first "error" a page sees must already carry the
    error_detail and the evidence, since the page records the failure then."""
    job, _job_dir, _source = _failed_separation_job(
        tmp_path, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    removing = _slow_rmtree(monkeypatch)
    seen_error: list[tuple[str | None, bool]] = []

    async def watch():
        while not seen_error:
            if job.status == "error":
                seen_error.append(
                    (job.error_detail, (tmp_path / "failed" / job.id / "error.txt").is_file())
                )
            await asyncio.sleep(0.001)

    watcher = asyncio.create_task(watch())
    with patch("app.pipeline.runner._run_blocking", side_effect=_worker_died):
        ticks = await _ticks_while(removing, run_pipeline(job, job.source_url, tmp_path))
    await asyncio.wait_for(watcher, 1)

    assert ticks > 0, "the quarantine blocked the event loop"
    detail, evidence = seen_error[0]
    assert detail is not None
    assert evidence


@pytest.mark.parametrize("ending", ["cancelled", "stopped", "emptied from the Trash"])
async def test_end_of_run_cleanup_leaves_the_loop_free(tmp_path: Path, monkeypatch, ending):
    """A cancel removes the folder, a stop removes the partial separation, and
    a job emptied from the Trash while it ran is removed when it settles."""
    from app.core import registry as _registry
    from app.pipeline import runner as _runner

    job = Job(
        id="abcdefabc130",
        cancel_requested=ending != "emptied from the Trash",
        stop_requested=ending == "stopped",
    )
    monkeypatch.setitem(_jobs, job.id, job)
    job_dir = tmp_path / job.id
    (job_dir / _runner.DEMUCS_MODEL).mkdir(parents=True)
    if ending == "emptied from the Trash":
        _registry.mark_deleted(job.id)

    def run(_job, _job_dir):
        if job.cancel_requested:
            raise JobCancelled()

    removing = _slow_rmtree(monkeypatch)
    ticks = await _ticks_while(removing, _runner._run_async(job, job_dir, tmp_path, run))

    assert ticks > 0, "the cleanup blocked the event loop"
    assert not (job_dir / _runner.DEMUCS_MODEL).exists()
    assert job_dir.exists() is (ending == "stopped")


async def test_cancel_before_claim_cleanup_leaves_the_loop_free(tmp_path: Path, monkeypatch):
    """The queue worker removes a job cancelled between pop and claim (#520)."""
    from app.pipeline import jobqueue

    job = Job(id="abcdefabc131", status="queued", cancel_requested=True)
    monkeypatch.setitem(_jobs, job.id, job)
    jobqueue.enqueue(job.id)
    (jobqueue.JOBS_DIR / job.id).mkdir(parents=True)
    removing = _slow_rmtree(monkeypatch)

    async def settle():
        task = jobqueue.start_worker()
        try:
            while job.status != "cancelled" or (jobqueue.JOBS_DIR / job.id).exists():
                await asyncio.sleep(0.01)
        finally:
            jobqueue.request_stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    ticks = await _ticks_while(removing, asyncio.wait_for(settle(), 5))

    assert ticks > 0, "the cleanup blocked the event loop"


async def test_failed_job_emptied_from_the_trash_during_its_quarantine_is_removed(
    tmp_path: Path, monkeypatch
):
    """The job reads as running while its folder moves, so emptying the Trash
    then answers 202 and leaves the removal to the runner (#748)."""
    from app.core import registry as _registry
    from app.pipeline import runner as _runner

    job, job_dir, _source = _failed_separation_job(tmp_path, "local:Hollow Veins")
    monkeypatch.setitem(_jobs, job.id, job)
    real_quarantine = _runner._quarantine_failed_job

    def quarantine_then_empty_the_trash(*args):
        _registry.mark_deleted(job.id)
        real_quarantine(*args)

    monkeypatch.setattr(_runner, "_quarantine_failed_job", quarantine_then_empty_the_trash)
    await _runner._run_async(job, job_dir, tmp_path, _worker_died)

    assert job.status == "error"
    assert job.id not in _registry.all_jobs(), "the deleted job's record stayed"
