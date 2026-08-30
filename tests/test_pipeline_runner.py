from __future__ import annotations

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
