from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from app.core.config import (
    DEMUCS_MODEL,
    EXTRA_STEM_NAMES,
    JOB_ID_RE,
    LYRICS_FILE,
    STEM_NAMES,
    TAKES_DIRNAME,
)
from app.core.models import (
    FINISHED_STATUSES,
    Job,
    clean_artist,
    clean_audio_tags,
    clean_identity,
    clean_work,
)
from app.core.settings import fsync_dir, quarantine_corrupt
from app.core.stem_files import stem_exists, stem_url
from app.core.takes import reconcile_tracks, recover_takes
from app.core.versions import clean_stems

logger = logging.getLogger("stemdeck.registry")

REGISTRY_VERSION = 2

Operation = Literal["pipeline", "fill", "split", "retime"]

_jobs: dict[str, Job] = {}
# Active subprocesses keyed by job id and operation. Lets POST /cancel
# terminate the running process from the API thread instead of waiting for the
# pipeline thread to notice the cancel flag. By operation too, because one song
# can have several waiting (a fill, a split, a lyrics timing) while one of them
# runs: keyed by id alone, cancelling one that waited terminated the process of
# the one running, the shared Demucs worker included.
_procs: dict[tuple[str, Operation], subprocess.Popen] = {}
# The operation the processes registered from here on belong to. Set by the
# endpoints that work on a finished song (see operation()); asyncio.to_thread
# carries it into the thread that registers them. Anything else is the job's
# own pipeline run.
_operation: ContextVar[Operation] = ContextVar("operation", default="pipeline")
_lock = threading.Lock()
_REGISTRY_FILE = "registry.json"
_TERMINAL = {"done"}
# Jobs that were queued or mid-flight when the process died. They are persisted
# too (so a queue left running survives a restart) but restored as "queued" and
# handed back to the queue -- see restore(). _TERMINAL stays narrow because
# restore() and _recover_done_job() both mean "finished successfully" by it.
_RESUMABLE = {"queued", "processing", "downloading", "analyzing", "separating"}
# Halted from the Trash with its files kept (#748). Persisted so a restart
# keeps it in the Trash, and never resumed: only Extract queues it again.
_STOPPED = {"stopped"}
_PERSISTED = _TERMINAL | _RESUMABLE | _STOPPED

# One resume only. A job that reliably kills the process (a demucs OOM taking
# the backend down with it) would otherwise be re-queued on every start,
# wedging the queue forever.
_MAX_RESUME_ATTEMPTS = 1

# Ids restore() wants re-queued, drained once by the app lifespan.
_pending_resume: list[str] = []

# Job ids the user deleted. Orphan recovery in restore() adopts any job-shaped
# directory it finds, which is what resurrected tracks whose files failed to
# delete (#521). A directory that outlived its delete must not come back, and
# the client-side tombstone cannot be relied on for that -- "Reset app data"
# wipes it. Persisted with the registry; see _prune_deleted for why it stays
# small.
_deleted: set[str] = set()

# What the user chose for a song, or its import recorded, that nothing in its
# folder can be derived from. registry.json alone used to hold these, so a
# registry rebuilt from the folders reset every one of them: a binned song came
# back, versions (and the mixes the page keys by their ids) were gone, and a
# link song could no longer fill its missing stems. metadata.json keeps a copy,
# written when the song finishes and by every request that changes one.
SAVED_FIELDS = (
    "favorite",
    "trashed_at",
    "versions",
    "active_version",
    "vocal_split_wanted",
    "selected_stems",
    "source_url",
    "source_format",
    "separation_model",
    "separation_quality",
    "auto_lyrics",
    "created_at",
)


def saved_fields(job: Job) -> dict[str, Any]:
    return {name: getattr(job, name) for name in SAVED_FIELDS}


def register(job: Job) -> Job:
    with _lock:
        _jobs[job.id] = job
    return job


def is_upload(job: Job) -> bool:
    """Uploads are the jobs that occupy disk while they wait."""
    return (job.source_url or "").startswith("local:")


def pending_count(*, uploads: bool) -> int:
    """How many jobs of one kind are waiting for their turn."""
    with _lock:
        return sum(1 for j in _jobs.values() if j.status == "queued" and is_upload(j) == uploads)


def requeue_if_capacity(job: Job, job_dir: Path, max_pending: int) -> bool:
    """Reset a stopped job and count it as waiting, if its kind has room.

    The same per-kind limit as a new import, checked and applied under one lock
    so two Extract presses cannot both squeeze into the last slot (#748)."""
    uploads = is_upload(job)
    with _lock:
        pending = sum(1 for j in _jobs.values() if j.status == "queued" and is_upload(j) == uploads)
        if pending >= max_pending:
            return False
        # Claims the slot. The file work in reset_for_rerun stays outside the
        # lock; the job is not in the queue until the caller enqueues it.
        job.status = "queued"
    reset_for_rerun(job, job_dir)
    return True


def register_if_capacity(job: Job, max_pending: int) -> bool:
    """Atomically check pending count and register if under capacity.
    Returns True if registered, False if the queue is full.

    Capacity is counted per kind: a waiting upload holds its source file on
    disk, a waiting URL holds nothing, so a backlog of one must not block the
    other."""
    uploads = is_upload(job)
    with _lock:
        pending = sum(1 for j in _jobs.values() if j.status == "queued" and is_upload(j) == uploads)
        if pending >= max_pending:
            return False
        _jobs[job.id] = job
    return True


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def remove(job_id: str) -> None:
    with _lock:
        _jobs.pop(job_id, None)
        for key in [key for key in _procs if key[0] == job_id]:
            del _procs[key]


def set_trashed(job_id: str, trashed: bool) -> Job | None:
    """Move a job to the Trash, or take it back out.

    Not a delete: the stems stay on disk and the job stays in the registry, so
    restoring is free and the user's audio is never destroyed by a tap. Only
    emptying the Trash calls DELETE, which is what actually removes files.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        job.trashed_at = time.time() if trashed else None
        return job


def set_favorite(job_id: str, favorite: bool) -> Job | None:
    """Mark a job as a favourite, or take it back out (#734)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        job.favorite = favorite
        return job


def all_jobs() -> dict[str, Job]:
    """Return a snapshot of the registry for sweep / cleanup."""
    with _lock:
        return dict(_jobs)


def _migrate(data: dict) -> dict:
    """Upgrade registry JSON to REGISTRY_VERSION incrementally.
    Each block transforms v(n) → v(n+1) so older snapshots always catch up."""
    version = data.get("version", 0)
    if version < 1:
        # v0 → v1: version field was absent; no structural change needed.
        data["version"] = 1
        version = 1
    if version < 2:
        # v1 → v2: nothing changes in this file. restore() copies each
        # song's SAVED_FIELDS into its metadata.json once.
        data["version"] = 2
        version = 2
    # Future migrations go here as `if version < N:` blocks.
    return data


def registry_path(jobs_dir: Path) -> Path:
    return jobs_dir / _REGISTRY_FILE


def mark_deleted(job_id: str) -> None:
    """Record that the user deleted this job, so restore() will not re-adopt
    its directory if the files outlived the delete."""
    with _lock:
        _deleted.add(job_id)


def deleted_ids() -> set[str]:
    """A snapshot of the ids the user deleted (see mark_deleted)."""
    with _lock:
        return set(_deleted)


def _prune_deleted(jobs_dir: Path) -> None:
    """Forget deletion records whose directory is finally gone.

    A record only has to outlive the directory it refers to, so this keeps the
    set naturally bounded instead of growing for the life of the install.
    Caller must not hold _lock."""
    with _lock:
        stale = {job_id for job_id in _deleted if not (jobs_dir / job_id).exists()}
        _deleted.difference_update(stale)


def persist(jobs_dir: Path) -> None:
    """Persist terminal jobs so completed library entries survive restarts.

    Callers run on the pipeline thread, API threads, and the sweep loop
    concurrently, so the write+replace happens under the lock with a unique
    temp name per call (#281) -- a shared temp path let two writers collide,
    and on Windows os.replace over a file another writer holds open raises
    PermissionError. Best-effort like the settings store: a failed persist
    logs and returns rather than killing the caller."""
    try:
        jobs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("cannot create jobs dir %s; skipping persist", jobs_dir, exc_info=True)
        return
    path = registry_path(jobs_dir)
    _prune_deleted(jobs_dir)
    with _lock:
        records = [
            job.to_record()
            for job in sorted(_jobs.values(), key=lambda item: item.created_at)
            if job.status in _PERSISTED
        ]
        payload = (
            json.dumps(
                {
                    "version": REGISTRY_VERSION,
                    "jobs": records,
                    "deleted": sorted(_deleted),
                },
                indent=2,
            )
            + "\n"
        )
        try:
            _write_atomic(path, payload)
        except OSError:
            logger.warning("could not persist registry to %s", path, exc_info=True)


def _write_atomic(path: Path, payload: str) -> None:
    """Replace `path` with `payload` so that a crash at any moment leaves
    either the old file or the new one, never a torn or empty one.

    The bytes are flushed and fsynced before the rename: without that, a power
    loss right after os.replace can leave the new name pointing at a file whose
    data never reached the disk, which is how a registry.json turns up empty.
    The temp name is unique per call so concurrent writers cannot collide on it
    (#281). Raises OSError; the temp file is gone either way."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(path.parent)


def _read_registry(path: Path) -> dict | None:
    """The registry file as a JSON object, or None when it is unusable.

    An unusable file (empty, truncated, not JSON, not an object, unreadable) is
    moved aside to registry.json.corrupt-<time> before anything else happens:
    restore() goes on to re-adopt the jobs on disk and persist, and that persist
    used to write over the only copy of the favourites, the Trash, the queue
    and the delete tombstones. Same quarantine as a corrupt settings.json."""
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError):
        logger.warning("registry at %s is unreadable", path, exc_info=True)
        quarantine_corrupt(path, "registry")
        return None
    if not isinstance(data, dict):
        logger.warning("registry at %s is not a JSON object", path)
        quarantine_corrupt(path, "registry")
        return None
    return data


def _keep_bad_records(path: Path, records: list, why: str) -> None:
    """Save the raw JSON of records the load skipped or repaired, beside the
    registry as registry.json.bad-<time>, so dropping them is never silent.
    The next persist rewrites registry.json without them."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bad-{stamp}-{uuid.uuid4().hex[:6]}")
    try:
        payload = json.dumps({"why": why, "records": records}, indent=2) + "\n"
        _write_atomic(target, payload)
        logger.warning("kept %d registry record(s) aside in %s: %s", len(records), target, why)
    except (OSError, TypeError, ValueError):
        logger.warning("could not keep bad registry records aside", exc_info=True)


def _annotation_types(annotation: object) -> tuple[type, ...] | None:
    """The JSON types a Job field annotation allows, or None when it is not one
    of the simple shapes the record uses (the field is then not checked).

    The annotations are strings (from __future__ import annotations), such as
    "float | None", "list[dict[str, Any]]" or "Literal['none', 'done']"."""
    if not isinstance(annotation, str):
        return None
    allowed: list[type] = []
    for part in annotation.split("|"):
        base = part.strip().split("[", 1)[0]
        if base == "None":
            allowed.append(type(None))
        elif base in ("str", "Literal", "JobStatus"):
            allowed.append(str)
        elif base in ("int", "float"):
            allowed.extend((int, float))
        elif base == "bool":
            allowed.append(bool)
        elif base == "list":
            allowed.append(list)
        elif base == "dict":
            allowed.append(dict)
        else:
            return None
    return tuple(allowed)


def _repair_field_types(job: Job) -> list[str]:
    """Reset every field whose value is the wrong JSON type to its default.

    Job.from_record copies values as they are, so a record with a string where
    a number belongs loaded fine and failed later, somewhere unrelated: sorting
    by created_at in persist() raised, and from then on no save succeeded.
    Returns the names of the fields it reset."""
    repaired: list[str] = []
    for f in dataclasses.fields(Job):
        if f.name == "id":
            continue
        allowed = _annotation_types(f.type)
        if allowed is None:
            continue
        value = getattr(job, f.name)
        ok = isinstance(value, allowed)
        if ok and isinstance(value, bool) and bool not in allowed:
            ok = False  # bool is an int subclass; True is not a number here
        if ok:
            continue
        if f.default is not dataclasses.MISSING:
            setattr(job, f.name, f.default)
        elif f.default_factory is not dataclasses.MISSING:
            setattr(job, f.name, f.default_factory())
        else:
            continue
        repaired.append(f.name)
    return repaired


def restore(jobs_dir: Path) -> None:
    """Load persisted jobs and recover completed orphan jobs from disk.

    Nothing the user had is lost silently: a file that cannot be read as a
    whole is moved aside (registry.json.corrupt-<time>), and a record that
    cannot be loaded as it was is skipped or repaired with its raw JSON kept in
    registry.json.bad-<time>. Every other record loads as before."""
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = registry_path(jobs_dir)
    changed = False
    data = _read_registry(path) if path.is_file() else None
    if data is not None:
        try:
            copy_saved = data.get("version", 0) < 2
            data = _migrate(data)
            recorded = data.get("deleted")
            if isinstance(recorded, list):
                with _lock:
                    _deleted.update(
                        job_id
                        for job_id in recorded
                        if isinstance(job_id, str) and JOB_ID_RE.match(job_id)
                    )
            to_add = {}
            resume: list[Job] = []
            deleted = deleted_ids()
            records = data.get("jobs", [])
            if not isinstance(records, list):
                _keep_bad_records(path, [records], "the jobs list was not a list")
                records = []
            bad: list = []
            # The fields each record really held, not a default filled in.
            held: dict[str, set[str]] = {}
            for record in records:
                # One record at a time. A single bad one used to abort the whole
                # load, and the persist after orphan recovery then wrote the
                # survivors over everything else: favourites, the Trash, the
                # queue and the delete tombstones.
                try:
                    job = Job.from_record(record)
                    repaired = _repair_field_types(job)
                except Exception:
                    rid = record.get("id") if isinstance(record, dict) else None
                    logger.warning("skipping unreadable registry record %r", rid, exc_info=True)
                    bad.append(record)
                    continue
                if repaired:
                    logger.warning(
                        "registry record %s had wrong types in %s; reset to defaults",
                        job.id,
                        ", ".join(repaired),
                    )
                    bad.append(record)
                    changed = True
                if not JOB_ID_RE.match(job.id):
                    continue
                if job.id in deleted:
                    # Deleted while it was still stopping, and the process
                    # ended before the stop settled (#748): finish the delete.
                    shutil.rmtree(jobs_dir / job.id, ignore_errors=True)
                    changed = True
                    continue
                held[job.id] = set(record) - set(repaired)
                try:
                    # A stopped job may have no title yet: one trashed before
                    # its download finished still has files and a Trash place.
                    if (job.status in _TERMINAL and job.title) or job.status in _STOPPED:
                        to_add[job.id] = job
                    elif job.status in _RESUMABLE:
                        recovered = _resume_or_recover(job, jobs_dir / job.id)
                        if recovered is not None:
                            to_add[recovered.id] = recovered
                            # The bumped resume_attempts has to reach disk here.
                            # The next persist is whenever the job finishes, so
                            # without this a job that kills the process every
                            # time is read back with the same count on every
                            # start and retried forever -- the exact loop the
                            # cap exists to stop.
                            changed = True
                            if recovered.status in _RESUMABLE:
                                resume.append(recovered)
                except Exception:
                    logger.warning("could not restore registry record %s", job.id, exc_info=True)
                    bad.append(record)
            if bad:
                _keep_bad_records(path, bad, "records that could not be loaded as they were")
            for job in to_add.values():
                if _sync_tracks(job, jobs_dir / job.id):
                    changed = True
            if copy_saved:
                # Songs finished before metadata.json kept a copy get one now,
                # or a registry lost later would still reset all of them.
                copied = sum(
                    _copy_saved_fields(job, jobs_dir, held.get(job.id, set()))
                    for job in to_add.values()
                    if job.status in _TERMINAL
                )
                if copied:
                    logger.info("copied saved fields into %d song folder(s)", copied)
                    changed = True
            with _lock:
                _jobs.update(to_add)
            # The user's order first, then oldest first. queue_position is
            # rewritten whenever the queue changes, so a queue the user
            # reordered comes back in that order rather than submission order.
            # Records written before that field existed all default to 0, where
            # created_at decides exactly as it used to.
            _pending_resume.extend(
                j.id for j in sorted(resume, key=lambda j: (j.queue_position, j.created_at))
            )
        except Exception:
            # Broad on purpose. restore() runs at import time, so anything that
            # escapes here stops the backend booting with no way for a user to
            # recover short of deleting the file by hand (#520). Records are
            # guarded one by one above, so reaching this is about the file as a
            # whole: keep its bytes before the persist below can replace them.
            logger.warning("failed to load registry from %s", path, exc_info=True)
            if path.is_file():
                quarantine_corrupt(path, "registry")

    # Orphan recovery, and the persist that follows it, were outside the guard
    # above -- an unreadable jobs_dir or a failed write was fatal at startup for
    # the same reason.
    try:
        with _lock:
            known = set(_jobs)
            deleted = set(_deleted)
        rebuilt: list[str] = []
        unsaved: list[str] = []
        for job_dir in jobs_dir.iterdir():
            if not job_dir.is_dir() or not JOB_ID_RE.match(job_dir.name) or job_dir.name in known:
                continue
            if job_dir.name in deleted:
                # The user deleted this and its files outlived the delete.
                # Adopting it here is what brought songs back (#521).
                continue
            found = _rebuild_from_folder(job_dir)
            if found is not None:
                recovered, had_saved = found
                _sync_tracks(recovered, job_dir)
                with _lock:
                    _jobs[recovered.id] = recovered
                changed = True
                rebuilt.append(recovered.id)
                if not had_saved:
                    unsaved.append(recovered.id)
        if rebuilt:
            listed = ", ".join(unsaved[:20]) or "none"
            if len(unsaved) > 20:
                listed += f" and {len(unsaved) - 20} more"
            logger.warning(
                "registry.json had no entry for %d song(s); rebuilt them from their folders: "
                "%d with their favourite, Trash, versions and source, %d without "
                "(metadata.json older than those, or unreadable): %s",
                len(rebuilt),
                len(rebuilt) - len(unsaved),
                len(unsaved),
                listed,
            )
        if changed:
            persist(jobs_dir)
    except Exception:
        logger.warning("failed to recover jobs from %s", jobs_dir, exc_info=True)


def _copy_saved_fields(job: Job, jobs_dir: Path, held: set[str]) -> bool:
    """Put the job's SAVED_FIELDS into its metadata.json, changing nothing else
    in it. A field the registry record did not hold only fills a gap: 0.20.0
    drops versions and the newer fields from the registry, so after a
    downgrade and back, metadata.json is their only copy. False when the song
    has no usable metadata.json."""
    meta_path = (jobs_dir / job.id / "metadata.json").resolve()
    if not meta_path.is_relative_to(jobs_dir.resolve()):
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return False
        for name, value in saved_fields(job).items():
            if name in held or name not in meta:
                meta[name] = value
        _write_atomic(meta_path, json.dumps(meta, indent=2) + "\n")
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        logger.warning("could not copy saved fields into %s", meta_path, exc_info=True)
        return False
    return True


def _resume_or_recover(job: Job, job_dir: Path) -> Job | None:
    """Decide what a job that was in flight when the process died becomes.

    Three states of the same directory need telling apart. If the stems are all
    there, the crash landed between the last stem being written and the "done"
    persist, so the work is finished and re-running it would duplicate the
    library entry. Otherwise the job goes back in the queue from the top, after
    the partial demucs output is cleared so collect() cannot mistake it for
    results. A job that has already burned its resume is failed loudly rather
    than retried forever.

    A job the user trashed before the restart is not resumed (#748). It came
    back as a Paused row in the queue, offered to Start a song that was in the
    Trash. It is stopped instead, the same end a trash before the restart now
    has, and its files stay until the Trash is emptied."""
    recovered = _recover_done_job(job_dir)
    if recovered is not None:
        # Finished after all: keep what the registry says the user chose (its
        # Trash, its Lead + Backing still to be split), the newer source.
        for name in SAVED_FIELDS:
            setattr(recovered, name, getattr(job, name))
        return recovered

    if job.trashed_at is not None:
        reset_for_rerun(job, job_dir)
        job.status = "stopped"
        job.stage_message = "Stopped"
        return job

    job.resume_attempts += 1
    if job.resume_attempts > _MAX_RESUME_ATTEMPTS:
        job.status = "error"
        job.stage_message = "Error: Interrupted"
        job.error = "Interrupted by a restart twice. Import it again to retry."
        return job

    reset_for_rerun(job, job_dir)
    return job


def reset_for_rerun(job: Job, job_dir: Path) -> None:
    """Put a job back to the start of the pipeline, its source kept.

    The partial demucs output is cleared so collect() cannot mistake it for
    results. Shared by a resume after a restart and by extracting a stopped
    job again (#748). Bumps version, so a stream watching the job sees it."""
    shutil.rmtree(job_dir / DEMUCS_MODEL, ignore_errors=True)
    job.status = "queued"
    job.stage_message = "Queued"
    job.progress = 0.0
    job.cancel_requested = False
    job.stop_requested = False
    job.error = None
    job.stems = []
    job.mix_url = None
    job.version += 1


def take_pending_resume() -> list[str]:
    """Ids to re-queue after a restart. Pops, so a resume can only fire once."""
    with _lock:
        ids = list(_pending_resume)
        _pending_resume.clear()
    return ids


def _sync_tracks(job: Job, job_dir: Path) -> bool:
    """Reconcile a loaded job's recording tracks with its takes on disk, so
    takes recorded before tracks existed come back as a track each. Never
    fatal: restore() runs at import time."""
    if not job.takes and not job.tracks and not (job_dir / "takes").is_dir():
        return False
    try:
        job.tracks, job.takes, changed = reconcile_tracks(job_dir, job.tracks, job.takes)
    except Exception:
        logger.warning("could not reconcile tracks for %s", job.id, exc_info=True)
        return False
    return changed


def _recover_done_job(job_dir: Path) -> Job | None:
    found = _rebuild_from_folder(job_dir)
    return found[0] if found is not None else None


def _rebuild_from_folder(job_dir: Path) -> tuple[Job, bool] | None:
    """A finished song rebuilt from its folder, and whether its metadata.json
    held the SAVED_FIELDS (a folder written before they were kept has none)."""
    stems_dir = job_dir / "stems"
    if not stems_dir.is_dir():
        return None
    # WAV or MP3, whichever the song was stored as (#783).
    stems = [
        {"name": name, "url": stem_url(job_dir.name, stems_dir, name)}
        for name in ("original", *STEM_NAMES, *EXTRA_STEM_NAMES)
        if stem_exists(stems_dir, name)
    ]
    if not stems:
        return None
    mix_url = None
    if stem_exists(stems_dir, "mix"):
        mix_url = stem_url(job_dir.name, stems_dir, "mix")
    selected = [stem["name"] for stem in stems if stem["name"] in STEM_NAMES] or list(STEM_NAMES)
    # A restart between the split finishing and its next registry persist
    # would otherwise report the job as never split -- derive from disk the
    # same way `stems` above does, so the recovered library entry doesn't
    # regress (#275).
    has_split = all(stem_exists(stems_dir, name) for name in EXTRA_STEM_NAMES)
    vocal_split = "done" if has_split else "none"
    meta_path = job_dir / "metadata.json"
    meta: dict = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        if not isinstance(meta, dict):
            meta = {}  # read as none, so the folders after this one still rebuild
    else:
        # Crash window (#284): the process died between status=done and the
        # metadata write, leaving a complete stems dir that used to be
        # unrecoverable. Recover with a placeholder title and write a minimal
        # metadata.json immediately, so the NEXT restart takes the normal
        # path -- self-healing, not a permanent special case.
        meta = {"title": f"Recovered track {job_dir.name[:6]}"}
        try:
            meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        except OSError:
            logger.warning("could not write recovery metadata for %s", job_dir.name, exc_info=True)
    job = Job(
        id=job_dir.name,
        status="done",
        progress=1.0,
        stage_message="Done",
        stems=stems,
        selected_stems=selected,
        mix_url=mix_url,
        created_at=job_dir.stat().st_mtime,
        title=meta.get("title"),
        thumbnail=meta.get("thumbnail"),
        duration_sec=meta.get("duration_sec"),
        bpm=meta.get("bpm"),
        key=meta.get("key"),
        scale=meta.get("scale"),
        key_confidence=meta.get("key_confidence"),
        lufs=meta.get("lufs"),
        peak_db=meta.get("peak_db"),
        dynamic_range=meta.get("dynamic_range"),
        loudness_version=meta.get("loudness_version"),
        tempo_stability=meta.get("tempo_stability"),
        stem_presence=meta.get("stem_presence"),
        sections=meta.get("sections"),
        sections_source=meta.get("sections_source"),
        tags=meta.get("tags"),
        audio_tags=clean_audio_tags(meta.get("audio_tags")),
        identity=clean_identity(meta.get("identity")),
        artist=clean_artist(meta.get("artist")),
        work=clean_work(meta.get("work")),
        # The file is the record: metadata.json predates it for most jobs.
        has_lyrics=(job_dir / LYRICS_FILE).is_file(),
        vocal_split=vocal_split,
        stem_format=meta.get("stem_format") if meta.get("stem_format") in ("wav", "mp3") else None,
        stem_mp3_kbps=meta.get("stem_mp3_kbps")
        if isinstance(meta.get("stem_mp3_kbps"), int)
        else None,
        # Takes are files first: their folders survive whatever the registry
        # lost, so the lanes come back with the job.
        takes=recover_takes(job_dir),
    )
    # A file on disk, so read through the same checks as a registry record: a
    # value of the wrong type keeps what the folder gave instead of loading.
    # NaN passes as a float but fails every JSON response the library sends.
    saved = {
        name: meta[name] for name in (*SAVED_FIELDS, "has_video", "video_status") if name in meta
    }
    if saved:
        kept = Job.from_record({**saved, "id": job.id})
        for name in saved.keys() - set(_repair_field_types(kept)):
            value = getattr(kept, name)
            if not isinstance(value, float) or math.isfinite(value):
                setattr(job, name, value)
        job.selected_stems = clean_stems(job.selected_stems) or selected
    return job, any(name in meta for name in SAVED_FIELDS)


@contextmanager
def operation(name: Operation) -> Iterator[None]:
    """Register the processes started inside as `name`'s (see _operation)."""
    token = _operation.set(name)
    try:
        yield
    finally:
        _operation.reset(token)


def set_proc(job_id: str, proc: subprocess.Popen | None) -> None:
    """Register `proc` as the running process of `job_id`'s current operation,
    or clear it with None.

    A pipeline process registered after its job's cancel is terminated here
    (#748). The cancel found nothing to terminate when it landed before this,
    during the device probe ahead of the Demucs worker for one, so the trashed
    song kept separating to the end. Checked under the lock the cancel's
    get_proc takes, so one of the two always sees the other."""
    key = (job_id, _operation.get())
    with _lock:
        if proc is None:
            _procs.pop(key, None)
            return
        _procs[key] = proc
        job = _jobs.get(job_id)
        cancelled = (
            key[1] == "pipeline"
            and job is not None
            and job.cancel_requested
            and job.status not in FINISHED_STATUSES
        )
    if cancelled and proc.poll() is None:
        proc.terminate()


def is_real_entry(entry: Path) -> bool:
    """Whether `entry` is itself in its folder, not a link or a Windows
    junction to somewhere else. Removing what is found by walking through one
    deletes whatever it points at."""
    return entry.resolve() == entry.parent.resolve() / entry.name


def job_dirs(folder: Path) -> list[Path]:
    """The job folders directly inside `folder` (the jobs folder, or its
    failed/ quarantine): a job id for a name, and a real directory. The jobs
    folder can be shared with other things, so nothing else in it is a job."""
    if not folder.is_dir():
        return []
    return [
        d for d in folder.iterdir() if JOB_ID_RE.match(d.name) and d.is_dir() and is_real_entry(d)
    ]


def holds_job_files(job_dir: Path) -> bool:
    """Whether a job-id folder holds something StemDeck writes into a job. The
    name alone does not make it ours: twelve hex characters is also a Docker
    container's short id. A folder that cannot be read is not ours to remove."""
    try:
        return any(
            (job_dir / name).exists() for name in ("stems", TAKES_DIRNAME, "metadata.json")
        ) or any(job_dir.glob("source.*"))
    except OSError:
        return False


def quarantine_dirs(jobs_dir: Path) -> list[Path]:
    """The job folders in failed/ that are StemDeck's quarantine: the runner
    writes error.txt into each before moving it there. Empty when failed/ is a
    link, or holds only someone else's folders."""
    failed_root = jobs_dir / "failed"
    if not is_real_entry(failed_root):
        return []
    return [d for d in job_dirs(failed_root) if (d / "error.txt").is_file()]


# The files reset removes beside the job folders: the registry, its temp and
# set-aside copies (_write_atomic, quarantine_corrupt, _keep_bad_records), and
# the desktop shell's store (#403).
_OWN_FILE_RE = re.compile(
    r"(registry\.json(\.(corrupt|bad)-[0-9a-f-]+)?|\.registry\.json\.[\w.-]+\.tmp|user-data\.json)\Z"
)


def reset_all(jobs_dir: Path) -> tuple[list[str], list[str]]:
    """Delete StemDeck's entries in the jobs folder, clearing the in-memory
    registry too. Factory reset (Settings -> General -> "Reset app data") --
    the caller is responsible for checking no job is actively running first
    (deleting a running job's directory out from under it would corrupt that
    job, not just reset the library).

    The jobs folder can be shared with other things (a volume mapped onto a
    share, a folder the user already had), so only what StemDeck made goes:
    job folders it has a record of or that hold job files, its quarantined
    failures in failed/, and its own files. Nothing behind a link is touched. Returns the
    names it could not remove and the names it left alone."""
    with _lock:
        known = set(_jobs) | _deleted
        _jobs.clear()
        _procs.clear()
    if not jobs_dir.is_dir():
        return [], []
    failed_root = jobs_dir / "failed"
    quarantined = quarantine_dirs(jobs_dir)
    targets = [d for d in job_dirs(jobs_dir) if d.name in known or holds_job_files(d)]
    targets += quarantined
    targets += [
        f
        for f in jobs_dir.iterdir()
        if _OWN_FILE_RE.match(f.name) and f.is_file() and is_real_entry(f)
    ]
    failed: list[str] = []
    for entry in targets:
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            logger.warning("reset: could not remove %s", entry, exc_info=True)
            failed.append(entry.name)
    # failed/ itself goes only if it is empty now: rmdir refuses otherwise, and
    # anything still in it is not ours.
    if quarantined and not any(failed_root.iterdir()):
        try:
            failed_root.rmdir()
        except OSError:
            logger.warning("reset: could not remove %s", failed_root, exc_info=True)
            failed.append(failed_root.name)
    skipped = sorted(e.name for e in jobs_dir.iterdir() if e.name not in failed)
    # Anything that survived is still a job-shaped directory on disk, so the
    # next start would adopt it and the "reset" library would refill itself.
    # Record it as deleted and persist that, which also recreates the registry
    # file this loop just removed. A known job left alone because it is a link
    # is one of these too.
    surviving = [name for name in failed if JOB_ID_RE.match(name)]
    surviving += [name for name in skipped if name in known]
    if surviving:
        with _lock:
            _deleted.update(surviving)
        persist(jobs_dir)
    return failed, skipped


def get_proc(job_id: str, op: Operation = "pipeline") -> subprocess.Popen | None:
    with _lock:
        return _procs.get((job_id, op))
