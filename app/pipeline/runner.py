from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from app.core.models import Job, JobCancelled
from app.core.registry import persist as persist_registry
from app.pipeline.analyze import analyze
from app.pipeline.collect import cleanup_source, collect, make_original_track, make_selected_mix
from app.pipeline.download import _set, download
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


def _run_blocking(job: Job, url: str, job_dir: Path) -> None:
    _check_cancel(job)
    source = download(job, url, job_dir)
    _check_cancel(job)
    analyze(job, source)
    _check_cancel(job)
    stems_root = separate(job, source, job_dir)
    found = collect(job, stems_root, job_dir)
    stems_dir = job_dir / "stems"
    # Source download (100-300 MB) is no longer used by anything below
    # -- both the original-complement and the selected-mix are built
    # from the demucs-emitted stems. Reclaim disk before the ffmpeg
    # amix steps in case they need scratch space.
    cleanup_source(job_dir)
    job.stems = [{"name": name, "url": f"/api/jobs/{job.id}/stems/{name}.wav"} for name in found]
    # Subset post-processing. When the user kept all 6 stems, both of
    # these are skipped -- the original is the sum of the stems and a
    # mix would equal the original. When a strict subset was chosen:
    #   - original.wav: complement of the selection (sum of unselected
    #     stems) so the studio can play it alongside the isolated
    #     selected stems and reconstruct the full song without doubling.
    #   - mix.wav: ffmpeg amix of the selected stems for download.
    # Update stage so the import-progress UI doesn't keep saying
    # "Separating stems..." for the extra ~5-30 s ffmpeg adds.
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
        # mix_path may point at mix.wav (multi-stem amix) or directly at
        # one of the existing stem WAVs (single-stem short-circuit).
        # Either way, .name gives us the URL segment.
        job.mix_url = f"/api/jobs/{job.id}/stems/{mix_path.name}"
    _check_cancel(job)


async def run_pipeline(job: Job, url: str, jobs_dir: Path) -> None:
    job_dir = jobs_dir / job.id
    # One try/except covers everything from directory creation through pipeline
    # execution. If anything before the lock raises, the job would otherwise
    # stay stuck on `queued` forever -- transition to `error` instead.
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        async with _pipeline_lock:
            await asyncio.to_thread(_run_blocking, job, url, job_dir)
    except JobCancelled:
        logger.info("pipeline cancelled for job %s", job.id)
        _set(job, status="cancelled", stage="Cancelled")
        persist_registry(jobs_dir)
        # Drop partial files so the disk reclaim is immediate.
        _rmtree(job_dir)
        return
    except Exception as e:
        # yt-dlp wraps hook exceptions in DownloadError; if the user cancelled
        # mid-download the underlying cause is JobCancelled but it arrives here
        # as a generic exception. Detect via the flag and route to cancelled.
        if job.cancel_requested:
            logger.info("pipeline cancelled (wrapped) for job %s", job.id)
            _set(job, status="cancelled", stage="Cancelled")
            persist_registry(jobs_dir)
            _rmtree(job_dir)
            return
        logger.exception("pipeline failed for job %s: %s", job.id, e)
        _set(
            job,
            status="error",
            stage="Error: Processing failed",
            error="Audio processing failed. Please try another video.",
        )
        persist_registry(jobs_dir)
        return
    _set(job, status="done", progress=1.0, stage="Done")
    persist_registry(jobs_dir)
