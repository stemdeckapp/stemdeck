from __future__ import annotations

import io

import segno
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

router = APIRouter()

_MAX_LEN = 500


@router.get("/qr")
def get_qr(url: str) -> Response:
    if not url or len(url) > _MAX_LEN:
        raise HTTPException(status_code=422, detail="url required (max 500 chars)")
    qr = segno.make_qr(url, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=5, border=2, dark="#1a1206", light="#ffffff")
    return Response(
        content=buf.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )
