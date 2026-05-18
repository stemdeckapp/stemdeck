from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import JOB_ID_RE, JOBS_DIR, STEM_NAMES, ffmpeg_executable
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


@router.get("/jobs/{job_id}/stems/{name}.mp3")
async def get_stem_mp3(job_id: str, name: str) -> StreamingResponse:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if name not in _ALLOWED_NAMES:
        raise HTTPException(status_code=404, detail="unknown stem")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    wav_path = (JOBS_DIR / job_id / "stems" / f"{name}.wav").resolve()
    if not wav_path.is_file() or not wav_path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="stem not found")

    async def _stream():
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(wav_path),
            "-q:a",
            "2",  # VBR ~190 kbps
            "-f",
            "mp3",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.stdout.close()
            await proc.wait()

    return StreamingResponse(
        _stream(),
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'attachment; filename="{name}.mp3"'},
    )
