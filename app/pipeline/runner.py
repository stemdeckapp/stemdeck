from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import DEMUCS_MODEL, TIMEOUT_FFMPEG
from app.core.models import Job, JobCancelled, _set
from app.core.redact import redact
from app.core.registry import persist as persist_registry
from app.core.settings import get_auto_sections
from app.pipeline.analyze import analyze
from app.pipeline.beatgrid import compute_beat_grid
from app.pipeline.collect import (
    cleanup_source,
    collect,
    compute_stem_peaks,
    make_original_track,
    make_selected_mix,
)
from app.pipeline.download import download
from app.pipeline.errors import classify_failure
from app.pipeline.sections import detect_sections
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
    into an MP4 (issue #219). Stream-copies the video (no
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
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_FFMPEG)
    except (OSError, subprocess.SubprocessError) as e:
        # ffmpeg missing or timed out. Distinct from an .mp4 that simply has no
        # video stream, and the only one of the two worth surfacing (#436).
        dest.unlink(missing_ok=True)
        job.video_status = "failed"
        logger.warning("video extract failed for job %s: %s", job.id, e)
        return
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        job.video_status = "unavailable"
        logger.info("no video track preserved for job %s (source has no video stream?)", job.id)
        return
    job.has_video = True
    job.video_status = "ok"


def _prepare_local_source(job: Job, source: Path, job_dir: Path) -> Path:
    """Transcode any local upload to 16-bit 44.1 kHz stereo WAV before
    handing it to Demucs. Normalises MP3 and non-standard WAV formats
    (24-bit, 32-bit float, high sample rate, multi-channel) that Demucs
    would otherwise process silently and output as silence.

    For .mp4 uploads, first preserves a silent video.mp4 for later
    MP4 export. Deletes the original source file after a
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


def _lap(job: Job, name: str, start: float) -> float:
    """Record a stage duration in job.stage_timings; returns a new start mark.
    Timings feed the one-line completion summary, metadata.json, and the
    failure quarantine's error.txt (#293)."""
    now = time.monotonic()
    if job.stage_timings is None:
        job.stage_timings = {}
    job.stage_timings[name] = round(now - start, 1)
    return now


def _presence_from_rms(rms_values: dict[str, float]) -> dict[str, int]:
    """Normalize per-stem RMS to 0-100 relative to the loudest stem -- exact
    logic the old analyze.compute_stem_presence used, now fed by the single
    streamed pass in compute_stem_peaks (#287) instead of a second full
    decode of every stem."""
    if not rms_values:
        return {}
    max_rms = max(rms_values.values())
    if max_rms < 1e-9:
        return {name: 0 for name in rms_values}
    return {name: max(0, min(100, round(rms / max_rms * 100))) for name, rms in rms_values.items()}


def _run_common(job: Job, source: Path, job_dir: Path) -> None:
    """Analyze → separate → collect → mix. Shared by both YouTube and local
    upload pipelines after their respective source acquisition steps."""
    _check_cancel(job)
    mark = time.monotonic()
    analyze(job, source)
    mark = _lap(job, "analyze", mark)
    _check_cancel(job)
    stems_root = separate(job, source, job_dir)
    mark = _lap(job, "separate", mark)
    found = collect(job, stems_root, job_dir)
    stems_dir = job_dir / "stems"
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
    # "original"/"mix" get peaks (all_stem_names) but are excluded from
    # presence (found -- the demucs-produced stems only), matching the old
    # two-pass behavior.
    rms_values = compute_stem_peaks(stems_dir, all_stem_names)
    job.stem_presence = _presence_from_rms(
        {name: rms for name, rms in rms_values.items() if name in found}
    )
    mark = _lap(job, "post", mark)

    # Beat grid for the click track. Runs last and swallows its own failures:
    # by this point the job is fully usable, and a missing grid only costs the
    # metronome. compute_beat_grid never raises, but the guard stays so a
    # future change there can't take the whole pipeline down with it.
    try:
        compute_beat_grid(stems_dir)
    except Exception:
        logger.exception("beat grid stage failed for job %s", job.id)
    mark = _lap(job, "beatgrid", mark)

    # Automatic sections are suggestions and never make an otherwise usable
    # separation fail. Cancellation remains authoritative so a user can still
    # stop a long CPU inference pass immediately. The setting is read here, per
    # job, rather than captured at import, so turning the toggle off applies to
    # the next job without a restart.
    _check_cancel(job)
    if get_auto_sections() and job.sections is None and job.duration_sec and job.duration_sec > 0:
        _set(job, stage="Analyzing song structure...")
        try:
            sections = detect_sections(job, stems_dir, job.duration_sec)
            if sections:
                _set(job, sections=sections, sections_source="automatic")
        except JobCancelled:
            raise
        except Exception:
            logger.exception("section analysis stage failed for job %s", job.id)
    _lap(job, "sections", mark)


def _run_blocking(job: Job, url: str, job_dir: Path) -> None:
    _check_cancel(job)
    mark = time.monotonic()
    source = download(job, url, job_dir)
    _lap(job, "download", mark)
    _run_common(job, source, job_dir)


def _run_local_blocking(job: Job, source_path: Path, job_dir: Path) -> None:
    _check_cancel(job)
    mark = time.monotonic()
    source = _prepare_local_source(job, source_path, job_dir)
    _lap(job, "prepare", mark)
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
        "sections": job.sections,
        "sections_source": job.sections_source,
        "tags": job.tags,
        "has_video": job.has_video,
        "video_status": job.video_status,
        "compute_device": job.compute_device,
        "gpu_fallback": job.gpu_fallback,
        "stage_timings": job.stage_timings,
    }
    try:
        (job_dir / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("could not write metadata.json for job %s", job.id, exc_info=True)


# Files worth keeping in a quarantined failure dir. Everything else (source
# download, stem WAVs, video) is deleted first -- evidence must stay KB-scale,
# not GB-scale.
_QUARANTINE_KEEP = frozenset(("error.txt", "metadata.json"))


def _quarantine_failed_job(job: Job, job_dir: Path, jobs_dir: Path, exc: Exception) -> None:
    """Preserve failure evidence instead of destroying it (#277).

    Writes error.txt (stage, device, model, timings, classified cause, stderr
    tail, full traceback), strips the heavy audio payloads, and moves the dir
    to jobs/failed/<id> where sweep_failed_jobs expires it after FAILED_TTL.
    Best-effort throughout: any step failing falls back to plain removal so a
    pathological error can never leak disk."""
    tail: list[str] = getattr(exc, "tail", None) or []
    cause = classify_failure("\n".join([*tail, repr(exc)]))
    detail = cause
    # error_detail reaches the client directly (job state, notification card,
    # and the report URL's "what" field) -- redact before the [:200] truncation,
    # not after, so a redaction placeholder never gets cut in half.
    #
    # Prefer the stderr tail, which only SeparationError carries. Without the
    # fallback, every yt-dlp failure arrived as the bare word "unknown" with no
    # message at all, and the only way to find out what happened was to read
    # data/logs/ (#434).
    message = redact(tail[-1]) if tail else redact(str(exc))
    if message.strip():
        detail += f" — {message[:200]}"
    job.error_detail = detail

    try:
        # title/source stay unredacted: they never leave this file (the
        # /failure API's allowlist excludes both, see app/api/jobs.py), so
        # this is purely local diagnostic value for the person looking at
        # their own disk. Everything below IS served to the client and is
        # redacted accordingly -- exc!r can embed a source URL (yt-dlp errors
        # often do), and the stderr tail/traceback can carry either a source
        # URL or the reporter's home directory.
        redacted_tail = [redact(line) for line in tail]
        lines = [
            f"time: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"job: {job.id}",
            f"title: {job.title or '(unknown)'}",
            f"source: {job.source_url or '(unknown)'}",
            f"stage: {job.stage_message}",
            f"device: {job.compute_device or getattr(exc, 'device', None) or '(not reached)'}",
            f"model: {DEMUCS_MODEL}",
            f"cause: {cause}",
            f"timings: {json.dumps(job.stage_timings) if job.stage_timings else '(none)'}",
            f"exception: {redact(repr(exc))}",
        ]
        if redacted_tail:
            lines += ["", "--- stderr tail ---", *redacted_tail]
        tb = redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))).rstrip()
        if tb:
            lines += ["", "--- traceback ---", tb]
        (job_dir / "error.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Strip heavy payloads: the quarantine keeps diagnostics, not audio.
        for p in list(job_dir.iterdir()):
            if p.name in _QUARANTINE_KEEP:
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)

        failed_root = jobs_dir / "failed"
        failed_root.mkdir(parents=True, exist_ok=True)
        dest = failed_root / job.id
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(job_dir), str(dest))
        logger.info("[%s] failure evidence kept at %s", job.id, dest)
    except Exception:
        logger.warning("[%s] quarantine failed; removing job dir", job.id, exc_info=True)
        _rmtree(job_dir)


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
            _quarantine_failed_job(job, job_dir, jobs_dir, e)
            persist_registry(jobs_dir)
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
    # One-line per-job summary: the timing telemetry that makes performance
    # regressions (and the CPU-vs-GPU question) answerable from logs (#293).
    t = job.stage_timings or {}
    logger.info(
        "[%s] done device=%s model=%s %s total=%.1fs",
        job.id,
        job.compute_device or "n/a",
        DEMUCS_MODEL,
        " ".join(f"{k}={v}s" for k, v in t.items()),
        sum(t.values()),
    )


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
