from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path

from app.core.config import JOB_ID_RE, STEM_NAMES
from app.core.models import Job

logger = logging.getLogger("stemdeck.registry")

_jobs: dict[str, Job] = {}
# Active subprocesses keyed by job_id (currently only Demucs). Lets
# POST /cancel terminate the running process from the API thread instead
# of waiting for the pipeline thread to notice the cancel flag.
_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()
_REGISTRY_FILE = "registry.json"
_TERMINAL = {"done"}


def register(job: Job) -> Job:
    with _lock:
        _jobs[job.id] = job
    return job


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def remove(job_id: str) -> None:
    with _lock:
        _jobs.pop(job_id, None)
        _procs.pop(job_id, None)


def all_jobs() -> dict[str, Job]:
    """Return a snapshot of the registry for sweep / cleanup."""
    with _lock:
        return dict(_jobs)


def persist(jobs_dir: Path) -> None:
    """Persist terminal jobs so completed library entries survive restarts."""
    try:
        jobs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("cannot create jobs dir %s; skipping persist", jobs_dir, exc_info=True)
        return
    with _lock:
        records = [
            job.to_record()
            for job in sorted(_jobs.values(), key=lambda item: item.created_at)
            if job.status in _TERMINAL
        ]
    path = jobs_dir / _REGISTRY_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": 1, "jobs": records}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def restore(jobs_dir: Path) -> None:
    """Load persisted jobs and recover completed orphan jobs from disk."""
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / _REGISTRY_FILE
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            to_add = {}
            for record in data.get("jobs", []):
                job = Job.from_record(record)
                if JOB_ID_RE.match(job.id) and job.status in _TERMINAL:
                    to_add[job.id] = job
            with _lock:
                _jobs.update(to_add)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("failed to load registry from %s", path, exc_info=True)

    with _lock:
        known = set(_jobs)
    changed = False
    for job_dir in jobs_dir.iterdir():
        if not job_dir.is_dir() or not JOB_ID_RE.match(job_dir.name) or job_dir.name in known:
            continue
        recovered = _recover_done_job(job_dir)
        if recovered is not None:
            with _lock:
                _jobs[recovered.id] = recovered
            changed = True
    if changed:
        persist(jobs_dir)


def _recover_done_job(job_dir: Path) -> Job | None:
    stems_dir = job_dir / "stems"
    if not stems_dir.is_dir():
        return None
    stems = [
        {"name": name, "url": f"/api/jobs/{job_dir.name}/stems/{name}.wav"}
        for name in ("original", *STEM_NAMES)
        if (stems_dir / f"{name}.wav").is_file()
    ]
    if not stems:
        return None
    mix_url = None
    if (stems_dir / "mix.wav").is_file():
        mix_url = f"/api/jobs/{job_dir.name}/stems/mix.wav"
    selected = [stem["name"] for stem in stems if stem["name"] in STEM_NAMES] or list(STEM_NAMES)
    return Job(
        id=job_dir.name,
        status="done",
        progress=1.0,
        stage_message="Done",
        stems=stems,
        selected_stems=selected,
        mix_url=mix_url,
        created_at=job_dir.stat().st_mtime,
    )


def set_proc(job_id: str, proc: subprocess.Popen | None) -> None:
    with _lock:
        if proc is None:
            _procs.pop(job_id, None)
        else:
            _procs[job_id] = proc


def get_proc(job_id: str) -> subprocess.Popen | None:
    with _lock:
        return _procs.get(job_id)
