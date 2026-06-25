from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

from app.core.config import TIMEOUT_FFMPEG
from app.core.models import Job, JobCancelled, _set
from app.core.registry import persist as persist_registry
from app.pipeline.analyze import analyze, compute_stem_presence
from app.pipeline.collect import (
    cleanup_source,
    collect,
    compute_stem_peaks,
    make_original_track,
    make_selected_mix,
)
from app.pipeline.download import download
from app.pipeline.separate import separate

logger = logging.getLogger("stemdeck.pipeline")


def _rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("failed to remove %s", path, exc_info=True)


# Only one heavy job runs at a time -- Demucs is GPU/CPU-hungry.
_pipeline_lock = asyncio.Semaphore(1)


def _check_cancel(job: Job) -> None:
    if job.cancel_requested:
        raise JobCancelled()


def _extract_video_track(job: Job, source: Path, job_dir: Path) -> None:
    """For an .mp4 upload, preserve a silent video-only track at
    video.mp4 so the studio can later mux it with a custom stem mix
    into a karaoke video (issue #219). Stream-copies the video (no
    re-encode) -- fast and lossless.

    Best-effort: an .mp4 with no video stream (audio-only container)
    fails harmlessly and leaves has_video false."""
    from app.core.config import ffmpeg_executable

    dest = job_dir / "video.mp4"
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-an",  # drop audio -- the mix is added at export time
        "-c:v",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_FFMPEG)
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        logger.info("no video track preserved for job %s (source has no video stream?)", job.id)
        return
    job.has_video = True


def _prepare_local_source(job: Job, source: Path, job_dir: Path) -> Path:
    """Transcode any local upload to 16-bit 44.1 kHz stereo WAV before
    handing it to Demucs. Normalises MP3 and non-standard WAV formats
    (24-bit, 32-bit float, high sample rate, multi-channel) that Demucs
    would otherwise process silently and output as silence.

    For .mp4 uploads, first preserves a silent video.mp4 for later
    karaoke-video export. Deletes the original source file after a
    successful transcode."""
    from app.core.config import ffmpeg_executable

    dest = job_dir / "source.wav"
    if source.resolve() == dest.resolve():
        return source

    _set(job, stage="Preparing audio...")
    if source.suffix.lower() == ".mp4":
        _extract_video_track(job, source, job_dir)
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ar",
        "44100",
        "-ac",
        "2",
        "-sample_fmt",
        "s16",
        "-y",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_FFMPEG)
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg transcode failed: " + result.stderr.decode("utf-8", errors="replace").strip()
        )
    source.unlink(missing_ok=True)
    return dest


def _run_common(job: Job, source: Path, job_dir: Path) -> None:
    """Analyze → separate → collect → mix. Shared by both YouTube and local
    upload pipelines after their respective source acquisition steps."""
    _check_cancel(job)
    analyze(job, source)
    _check_cancel(job)
    stems_root = separate(job, source, job_dir)
    found = collect(job, stems_root, job_dir)
    stems_dir = job_dir / "stems"
    job.stem_presence = compute_stem_presence(stems_dir, found)
    # Source (100-300 MB or the local upload) is no longer needed after
    # collect; delete it before the ffmpeg amix steps in case scratch space
    # is tight.
    cleanup_source(job_dir)
    job.stems = [{"name": name, "url": f"/api/jobs/{job.id}/stems/{name}.wav"} for name in found]
    _check_cancel(job)
    _set(job, stage="Mixing tracks...")
    original_path = make_original_track(job, job_dir, stems_dir)
    if original_path is not None:
        job.stems.insert(
            0,
            {
                "name": "original",
                "url": f"/api/jobs/{job.id}/stems/original.wav",
            },
        )
    _check_cancel(job)
    mix_path = make_selected_mix(job, stems_dir, found)
    if mix_path is not None:
        job.mix_url = f"/api/jobs/{job.id}/stems/{mix_path.name}"
    _check_cancel(job)

    all_stem_names = [s["name"] for s in job.stems]
    if mix_path is not None and mix_path.stem not in all_stem_names:
        all_stem_names.append(mix_path.stem)
    compute_stem_peaks(stems_dir, all_stem_names)


def _run_blocking(job: Job, url: str, job_dir: Path) -> None:
    _check_cancel(job)
    source = download(job, url, job_dir)
    _run_common(job, source, job_dir)


def _run_local_blocking(job: Job, source_path: Path, job_dir: Path) -> None:
    _check_cancel(job)
    source = _prepare_local_source(job, source_path, job_dir)
    _run_common(job, source, job_dir)


def _write_metadata(job: Job, job_dir: Path) -> None:
    meta = {
        "title": job.title,
        "thumbnail": job.thumbnail,
        "duration_sec": job.duration_sec,
        "bpm": job.bpm,
        "key": job.key,
        "scale": job.scale,
        "key_confidence": job.key_confidence,
        "lufs": job.lufs,
        "peak_db": job.peak_db,
        "dynamic_range": job.dynamic_range,
        "tempo_stability": job.tempo_stability,
        "stem_presence": job.stem_presence,
        "tags": job.tags,
        "has_video": job.has_video,
    }
    try:
        (job_dir / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("could not write metadata.json for job %s", job.id, exc_info=True)


async def _run_async(
    job: Job,
    job_dir: Path,
    jobs_dir: Path,
    blocking_fn,
    *fn_args: object,
    error_msg: str = "Audio processing failed. Please try again.",
) -> None:
    """Common async wrapper: acquires the pipeline lock, runs blocking_fn in a
    thread, then handles success / cancel / error outcomes uniformly."""
    try:
        async with _pipeline_lock:
            await asyncio.to_thread(blocking_fn, job, *fn_args, job_dir)
    except Exception as e:
        if not isinstance(e, JobCancelled) and not job.cancel_requested:
            logger.exception("pipeline failed for job %s: %s", job.id, e)
            _set(job, status="error", stage="Error: Processing failed", error=error_msg)
            persist_registry(jobs_dir)
            _rmtree(job_dir)
            return
        logger.info(
            "pipeline cancelled%s for job %s",
            " (wrapped)" if not isinstance(e, JobCancelled) else "",
            job.id,
        )
        _set(job, status="cancelled", stage="Cancelled")
        persist_registry(jobs_dir)
        _rmtree(job_dir)
        return
    _set(job, status="done", progress=1.0, stage="Done")
    _write_metadata(job, job_dir)
    persist_registry(jobs_dir)


async def run_pipeline(job: Job, url: str, jobs_dir: Path) -> None:
    job_dir = jobs_dir / job.id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.exception("pipeline failed for job %s: %s", job.id, e)
        _set(
            job,
            status="error",
            stage="Error: Processing failed",
            error="Audio processing failed. Please try another video.",
        )
        persist_registry(jobs_dir)
        return
    await _run_async(
        job,
        job_dir,
        jobs_dir,
        _run_blocking,
        url,
        error_msg="Audio processing failed. Please try another video.",
    )


async def run_local_pipeline(job: Job, source_path: Path, jobs_dir: Path) -> None:
    """Run the stem-separation pipeline for a locally uploaded file.
    The job directory and source file are already present on disk (created
    by the API handler before this task is scheduled)."""
    job_dir = jobs_dir / job.id
    await _run_async(job, job_dir, jobs_dir, _run_local_blocking, source_path)
