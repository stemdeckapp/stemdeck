from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from app.core.config import (
    DEMUCS_MODEL,
    FAILED_TTL_SECONDS,
    JOB_ID_RE,
    JOB_TTL_SECONDS,
    STEM_NAMES,
    TAKES_DIRNAME,
    TIMEOUT_FFMPEG,
    ffmpeg_executable,
)
from app.core.models import FINISHED_STATUSES, Job
from app.core.registry import all_jobs as registry_all
from app.core.registry import deleted_ids as registry_deleted_ids
from app.core.registry import persist as registry_persist
from app.core.registry import remove as registry_remove
from app.core.registry import set_proc
from app.core.stem_files import is_stem_audio, stem_names_on_disk, stem_path
from app.pipeline.audio_stats import scan_stem

logger = logging.getLogger("stemdeck.collect")


def _rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("failed to remove %s", path, exc_info=True)


def _run_ffmpeg(job: Job, cmd: list[str]) -> bool:
    """Run an ffmpeg command, registering the subprocess with the job
    registry so POST /api/jobs/{id}/cancel can terminate it. Returns
    True on success, False on failure or external termination.

    Without registering the proc, an in-flight ffmpeg amix would block
    cancellation for up to its 300s timeout -- the cancel flag is set
    but the runner can't see it until subprocess.run returns. With
    set_proc, the cancel API can call proc.terminate() directly and
    communicate() returns within ~1s with a non-zero returncode."""
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    set_proc(job.id, proc)
    try:
        try:
            _, stderr = proc.communicate(timeout=TIMEOUT_FFMPEG)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            logger.warning("ffmpeg timed out for job %s", job.id)
            return False
        if proc.returncode != 0:
            tail = (stderr or b"").decode(errors="replace").splitlines()[-3:]
            logger.warning(
                "ffmpeg exit %s for job %s: %s",
                proc.returncode,
                job.id,
                " | ".join(tail) or "(no stderr)",
            )
            return False
        return True
    finally:
        set_proc(job.id, None)


def remove_job_dir(job_dir: Path) -> bool:
    """Remove a job's directory. False means files are still on disk.

    Retried once: on macOS the common failure is Finder or Spotlight creating
    a .DS_Store between rmtree's scan and its final rmdir, which leaves
    "Directory not empty" on a directory that is about to be empty again. On
    Windows it is a handle a process that just exited has not quite let go of,
    which is what the runner's cancel path meets right after terminating
    ffmpeg or the demucs worker (#749). Shared by delete_job and that path."""
    for attempt in (1, 2):
        if not job_dir.is_dir():
            return True
        try:
            shutil.rmtree(job_dir)
            return True
        except Exception:
            logger.warning(
                "failed to remove job dir %s (attempt %d)", job_dir, attempt, exc_info=True
            )
    return not job_dir.is_dir()


_TERMINAL = FINISHED_STATUSES


def collect(job: Job, stems_root: Path, job_dir: Path) -> list[str]:
    """Move Demucs-emitted stems into the job's stems/ dir and clean up
    the demucs intermediate dir. Does NOT delete the source download --
    cleanup_source() is called by the runner, for the jobs whose source
    can be fetched again, after any post-processing that needs to
    re-encode it (e.g. building original.wav)."""
    target_dir = job_dir / "stems"
    target_dir.mkdir(exist_ok=True)
    found: list[str] = []
    for name in STEM_NAMES:
        src = stems_root / f"{name}.wav"
        if src.exists():
            shutil.move(str(src), target_dir / f"{name}.wav")
            found.append(name)
    _rmtree(job_dir / DEMUCS_MODEL)
    if not found:
        raise RuntimeError("no stems produced by demucs")
    return found


def cleanup_source(job_dir: Path) -> None:
    """Delete the source audio file. Called after collect AND after any
    post-processing that re-encodes the source (make_original_track).
    The source is 100-300 MB, so getting rid of it is the bulk of disk
    reclaim per job; only the stems remain.

    Not called for uploads. The runner decides: a link can be fetched
    again, an upload cannot, and deleting an upload's source destroys
    the only copy StemDeck has of it."""
    for f in job_dir.glob("source.*"):
        f.unlink(missing_ok=True)


def make_original_track(job: Job, job_dir: Path, stems_dir: Path) -> Path | None:
    """Build the "Original" backing track at stems/original.wav as the
    sum of the stems the user did NOT select. This way the studio can
    play (original + each selected stem) and reconstruct the full song
    without doubling the selected stems -- which is what would happen
    if "original" were the raw source download (drum hits in original
    + isolated drums.wav = drums at 2x amplitude).

    Skipped when the user kept all 6 stems (no complement to mix) or
    when none of the unselected stem WAVs are on disk."""
    unselected = [s for s in STEM_NAMES if s not in job.selected_stems]
    inputs = [stems_dir / f"{name}.wav" for name in unselected]
    inputs = [p for p in inputs if p.exists()]
    if not inputs:
        return None
    out = stems_dir / "original.wav"
    cmd: list[str] = [
        ffmpeg_executable(),
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
    ]
    for p in inputs:
        cmd += ["-i", str(p)]
    if len(inputs) == 1:
        # Single complement stem -- copy as-is so we still produce a
        # canonical mix.wav-shaped output without invoking amix on a
        # 1-input graph (which is a no-op anyway).
        cmd += ["-c:a", "pcm_s16le", str(out)]
    else:
        filter_inputs = "".join(f"[{i}:a]" for i in range(len(inputs)))
        cmd += [
            "-filter_complex",
            f"{filter_inputs}amix=inputs={len(inputs)}:normalize=0",
            "-c:a",
            "pcm_s16le",
            str(out),
        ]
    return out if _run_ffmpeg(job, cmd) else None


def make_selected_mix(job: Job, stems_dir: Path, found: list[str]) -> Path | None:
    """If the user picked a strict subset of stems at submit time,
    sum those stems with ffmpeg amix into mix.wav. Returns the output
    path on success, or None when there's nothing to mix.

    Returns the existing single stem path (no ffmpeg) if exactly one
    stem was selected -- copying it to mix.wav would be 30 MB of
    duplicate data. The caller uses the returned path's name for the
    download URL, so a single-stem selection points the Download Mix
    button directly at the existing stem file.

    amix normalize=0 keeps stem amplitudes as-is. Demucs separations
    sum back to (close to) the original signal, so a 2-stem subset
    fits comfortably below 0 dBFS without normalization headroom."""
    selected = [s for s in job.selected_stems if s in found]
    if not selected:
        return None
    if len(selected) == 1:
        return stems_dir / f"{selected[0]}.wav"
    inputs = [stems_dir / f"{name}.wav" for name in selected]
    out = stems_dir / "mix.wav"
    cmd: list[str] = [
        ffmpeg_executable(),
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
    ]
    for p in inputs:
        cmd += ["-i", str(p)]
    filter_inputs = "".join(f"[{i}:a]" for i in range(len(inputs)))
    cmd += [
        "-filter_complex",
        f"{filter_inputs}amix=inputs={len(inputs)}:normalize=0",
        "-c:a",
        "pcm_s16le",
        str(out),
    ]
    return out if _run_ffmpeg(job, cmd) else None


# Enough points to back the deepest zoom, not the 1x bar count: a full-width
# panel draws about 2400 bars at WAVE_ZOOM_MAX (10x, transport.js), and past
# the point count the bars repeat their neighbours instead of revealing
# anything. Raise this and WAVE_ZOOM_MAX together.
_PEAK_POINTS = 3000


def compute_stem_peaks(stems_dir: Path, stem_names: list[str]) -> dict[str, float]:
    """Compute and cache [min, max] waveform peaks for each stem via a single
    streamed pass per file (#286), and return each stem's RMS from that same
    pass so the caller can derive stem presence without a second decode
    (#287). Peaks failure is non-fatal — missing peaks.json degrades to
    client-side decode; a stem missing from the returned dict is simply
    excluded from presence."""
    peaks: dict[str, list[list[float]]] = {}
    rms_values: dict[str, float] = {}
    for name in stem_names:
        path = stems_dir / f"{name}.wav"
        if not path.is_file():
            continue
        try:
            result, rms = scan_stem(path, _PEAK_POINTS)
            if not result:
                continue
            peaks[name] = result
            rms_values[name] = rms
        except Exception:
            logger.warning("could not compute peaks for %s/%s", stems_dir.name, name, exc_info=True)

    if peaks:
        try:
            tmp = stems_dir / "peaks.json.tmp"
            tmp.write_text(json.dumps(peaks), encoding="utf-8")
            tmp.replace(stems_dir / "peaks.json")
        except Exception:
            logger.warning("could not write peaks.json for %s", stems_dir.name, exc_info=True)

    return rms_values


# Serialises the read-modify-write of peaks.json between a take being saved
# (merge_stem_peaks) and the first request of a job that had none
# (fill_missing_peaks), which run on different threads.
_PEAKS_WRITE_LOCK = threading.Lock()


def merge_stem_peaks(
    stems_dir: Path,
    new_names: list[str],
    sources: dict[str, Path] | None = None,
    remove: list[str] | None = None,
) -> dict[str, float]:
    """Add peaks/RMS for newly-produced stems (e.g. the on-demand lead/backing
    vocal split, #275) into the existing peaks.json instead of recomputing
    every stem. Best-effort, same as compute_stem_peaks: a failure here only
    costs client-side waveform decode for the new stems, never the job.

    Returns each scanned stem's RMS, from the same pass, so the caller can
    extend stem presence without decoding anything twice (see
    presence_for_split).

    `sources` maps a name to a WAV outside stems_dir: a recorded take lives in
    takes/<take_id>/audio.wav but its waveform is served from the same
    peaks.json as the stems, so the chunked engine needs no fallback. `remove`
    drops entries, for a deleted take."""
    path = stems_dir / "peaks.json"
    rms_values: dict[str, float] = {}
    scanned: dict[str, list[list[float]]] = {}
    # A job that never had peaks.json (separated before it existed, or made
    # by hand) gets one here for the first time. Written with the take alone,
    # the studio would see peaks, stream the song, and draw no stem at all.
    # Those extra stems are drawn only: their RMS is not returned.
    backfill = (
        []
        if path.is_file() or remove
        else [n for n in _stem_wav_names(stems_dir) if n not in new_names]
    )

    for name in [*backfill, *new_names]:
        wav = (sources or {}).get(name) or stem_path(stems_dir, name)
        if not wav.is_file():
            continue
        try:
            result, rms = scan_stem(wav, _PEAK_POINTS)
            if name in new_names:
                rms_values[name] = rms
            if result:
                scanned[name] = result
        except Exception:
            logger.warning("could not compute peaks for %s/%s", stems_dir.name, name, exc_info=True)

    # Read, change and write under one lock: a take saved while the first
    # request of an old job builds its peaks must not lose either change.
    with _PEAKS_WRITE_LOCK:
        peaks: dict[str, list[list[float]]] = {}
        if path.is_file():
            try:
                peaks = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("could not read existing peaks.json in %s", stems_dir, exc_info=True)
        for name in remove or []:
            peaks.pop(name, None)
        peaks.update(scanned)
        if not peaks and not path.is_file():
            # Nothing to draw and no file yet: leave it to fill_missing_peaks.
            return rms_values
        try:
            tmp = stems_dir / "peaks.json.tmp"
            tmp.write_text(json.dumps(peaks), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.warning("could not write peaks.json for %s", stems_dir.name, exc_info=True)

    return rms_values


def _stem_wav_names(stems_dir: Path) -> list[str]:
    # WAV or MP3 (#783): an MP3-stored song has no WAVs left.
    return stem_names_on_disk(stems_dir)


def fill_missing_peaks(stems_dir: Path, sources: dict[str, Path] | None = None) -> bool:
    """Write peaks.json for a job that has none: every WAV in stems_dir, plus
    `sources` (name -> WAV elsewhere: the recorded takes). Returns True when
    peaks.json exists afterwards.

    Jobs separated before peaks.json existed, or put together by hand, have
    no peaks. The studio then had to decode every stem in the browser for its
    waveforms, which meant swapping the streaming engine for the full-decode
    one a moment after the song opened, and a take started in that moment was
    recording into the engine being thrown away. Built once, on the first
    request: one streamed pass per file, about 1 s for a three minute song of
    seven stems. Entries that appear meanwhile (a take saved during the scan)
    are kept, not overwritten.
    """
    path = stems_dir / "peaks.json"
    if path.is_file():
        return True
    wanted: dict[str, Path] = {
        name: stem_path(stems_dir, name) for name in _stem_wav_names(stems_dir)
    }
    for name, wav in (sources or {}).items():
        if wav.is_file():
            wanted[name] = wav
    computed: dict[str, list[list[float]]] = {}
    for name, wav in wanted.items():
        try:
            result, _rms = scan_stem(wav, _PEAK_POINTS)
        except Exception:
            logger.warning("could not compute peaks for %s/%s", stems_dir.name, name, exc_info=True)
            continue
        if result:
            computed[name] = result
    if not computed:
        return False
    with _PEAKS_WRITE_LOCK:
        peaks: dict[str, list[list[float]]] = {}
        if path.is_file():
            try:
                peaks = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("could not read peaks.json in %s", stems_dir, exc_info=True)
        for name, result in computed.items():
            peaks.setdefault(name, result)
        tmp = stems_dir / f"peaks.json.{uuid.uuid4().hex}.tmp"
        try:
            tmp.write_text(json.dumps(peaks), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            logger.warning("could not write peaks.json for %s", stems_dir.name, exc_info=True)
            return False
        finally:
            tmp.unlink(missing_ok=True)
    logger.info(
        "built missing peaks.json for %s (%d entries)", stems_dir.parent.name, len(computed)
    )
    return True


def presence_for_split(
    rms_values: dict[str, float],
    stem_presence: dict[str, int] | None,
    *,
    reference: str = "vocals",
) -> dict[str, int]:
    """Put the lead/backing stems on the same 0-100 scale the pipeline already
    recorded for the base six.

    Presence is a stem's RMS as a percentage of the loudest stem in the job,
    and that loudest value is not stored anywhere -- only the percentages are.
    It can be recovered exactly from any stem whose presence is known:

        loudest = rms[reference] / (presence[reference] / 100)

    So scanning the reference stem alongside the new ones is enough, and the
    alternative (re-decoding all eight stems to renormalise) is avoided.

    Returns an empty dict when the reference is missing or silent, which leaves
    the new cards reading "--" rather than showing a number derived from
    nothing."""
    reference_rms = rms_values.get(reference)
    reference_pct = (stem_presence or {}).get(reference)
    if not reference_rms or not reference_pct:
        return {}
    loudest = reference_rms / (reference_pct / 100)
    if loudest < 1e-9:
        return {}
    return {
        name: max(0, min(100, round(rms / loudest * 100)))
        for name, rms in rms_values.items()
        if name != reference
    }


def _has_takes_on_disk(job_dir: Path) -> bool:
    """True when the job folder holds a recording, finished or not. A take in
    progress is on disk before it is in job.takes, so the record alone is not
    enough to know."""
    takes = job_dir / TAKES_DIRNAME
    try:
        return takes.is_dir() and any(takes.iterdir())
    except OSError:
        return True  # cannot tell, so assume there is something to keep


def _has_stems_on_disk(job_dir: Path) -> bool:
    stems = job_dir / "stems"
    try:
        # An MP3-stored song (#783) holds stems too: missing it here would let
        # the sweep delete an unregistered folder that still has a song in it.
        return stems.is_dir() and any(is_stem_audio(p) for p in stems.iterdir())
    except OSError:
        return True


def _keep_reason(job: Job | None, job_dir: Path, cutoff: float, deleted: set[str]) -> str | None:
    """Why the TTL sweep must leave `job_dir` alone, or None to remove it.

    The sweep is the user's own "delete finished jobs after N days" setting,
    and it only ever means plain finished separations. It used to judge by age
    alone, so it also deleted the user's favourites, their recorded takes, and
    any folder at all in the library directory that was old enough, job or not.
    Every rule below keeps something the user would not count as "a finished
    job I no longer need"; when in doubt, keep."""
    if not JOB_ID_RE.match(job_dir.name):
        # Not a job folder: something of the user's, or of another program,
        # that happens to live in the library directory.
        return "not a job folder"
    if job is not None:
        if job.status not in _TERMINAL:
            return "job is still active"
        if job.created_at >= cutoff:
            return "job is newer than the cutoff"
        if job.favorite:
            return "favourite"
        if job.takes or job.tracks or _has_takes_on_disk(job_dir):
            return "holds recorded takes"
        # A job in the Trash follows the same rule as any other finished job:
        # the Trash has no purge of its own, and this sweep never had a
        # separate rule for it.
        return None
    # Unregistered. restore() adopts every job folder that has stems, so one
    # that is still unregistered is either a delete whose files outlived it
    # (tombstoned, the user asked for it gone) or an import that never got as
    # far as stems. Anything else is kept.
    try:
        if job_dir.stat().st_mtime >= cutoff:
            return "unregistered folder is newer than the cutoff"
    except OSError:
        return "unregistered folder cannot be read"
    if job_dir.name in deleted:
        return None
    if _has_takes_on_disk(job_dir):
        return "unregistered folder holds recorded takes"
    if _has_stems_on_disk(job_dir):
        return "unregistered folder holds stems"
    return None


def sweep_old_jobs(jobs_dir: Path, ttl_seconds: int | None = None) -> None:
    """Delete finished jobs older than `ttl_seconds` (JOB_TTL_SECONDS when not
    given) and remove them from the in-memory registry. Called hourly from the
    background sweep loop started at app startup, and only when the user has
    asked for automatic deletion -- deciding *whether* to sweep is the caller's
    job, this one only decides what is old and what may go.

    Judged by Job.created_at, not directory mtime (which unrelated filesystem
    events touch). Never removes an active job, a favourite, a job with
    recorded takes or tracks, or a folder whose name is not a job id; an
    unregistered job folder goes only when it is old and abandoned. See
    _keep_reason for each rule. Every removal is logged with its reason."""
    cutoff = time.time() - (JOB_TTL_SECONDS if ttl_seconds is None else ttl_seconds)
    if not jobs_dir.is_dir():
        return
    jobs = registry_all()
    deleted = registry_deleted_ids()
    removed = False
    for d in jobs_dir.iterdir():
        if not d.is_dir():
            continue
        if d.name == "failed":
            continue  # the failure quarantine has its own TTL (sweep_failed_jobs)
        job = jobs.get(d.name)
        reason = _keep_reason(job, d, cutoff, deleted)
        if reason is not None:
            logger.debug("auto-delete kept %s: %s", d.name, reason)
            continue
        if job is not None:
            logger.info("auto-delete removed job %s: finished and older than the cutoff", d.name)
        else:
            logger.info("auto-delete removed unregistered job folder %s: old and abandoned", d.name)
        _rmtree(d)
        registry_remove(d.name)
        removed = True
    if removed:
        registry_persist(jobs_dir)


def sweep_failed_jobs(jobs_dir: Path) -> None:
    """Expire quarantined failure-evidence dirs (jobs/failed/<id>) older than
    FAILED_TTL_SECONDS. Runs unconditionally from the hourly sweep loop --
    unlike the job TTL sweep, which persistent-library deployments disable,
    failure evidence must never accumulate forever (#277)."""
    failed_root = jobs_dir / "failed"
    if not failed_root.is_dir():
        return
    cutoff = time.time() - FAILED_TTL_SECONDS
    for d in failed_root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                _rmtree(d)
        except OSError:
            logger.warning("failed-quarantine sweep could not stat %s", d, exc_info=True)
