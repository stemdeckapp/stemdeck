from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import JOB_ID_RE, JOBS_DIR, STEM_NAMES
from app.core.registry import get as registry_get

router = APIRouter(tags=["stems"])

# Stem files served by this endpoint: the 6 demucs stems + two
# pipeline-produced extras. "original" is the re-encoded source song
# (added when the user picked a strict subset), "mix" is the ffmpeg
# amix of the user's selected stems.
_ALLOWED_NAMES = frozenset(STEM_NAMES) | {"original", "mix"}


@router.api_route("/jobs/{job_id}/stems/{name}.wav", methods=["GET", "HEAD"])
def get_stem(job_id: str, name: str) -> FileResponse:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if name not in _ALLOWED_NAMES:
        raise HTTPException(status_code=404, detail="unknown stem")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    # Resolve and confirm the path stays under JOBS_DIR -- belt and suspenders
    # on top of the regex above. Mirrors the check in app/pipeline/analyze.py.
    path = (JOBS_DIR / job_id / "stems" / f"{name}.wav").resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="stem not found")
    return FileResponse(path, media_type="audio/wav", filename=f"{name}.wav")
