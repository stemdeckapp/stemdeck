"""The take render's video encoders, for the Settings "Video encoder" control.

GET  /api/video-encoders       the policy, this OS's candidates, what the
                               probe cache already knows (never probes)
POST /api/video-encoders/test  probe every candidate afresh at 1080p30 and
                               4K30, plus libx264 for comparison

Nothing from the request reaches a command: the test takes no input, and the
encoder names in every command come from config.VIDEO_GPU_ENCODERS. One test
at a time (409 while one runs), bounded by VIDEO_ENCODER_TEST_BUDGET_SEC.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from fastapi import APIRouter, HTTPException

from app.core.config import VIDEO_CPU_ENCODER, VIDEO_ENCODER_TEST_BUDGET_SEC
from app.core.settings import get_video_encoder
from app.pipeline import video_encoder

logger = logging.getLogger("stemdeck.api")

router = APIRouter()

_test_lock = asyncio.Lock()


def _summary(version: str) -> dict:
    value, source = video_encoder.policy()
    cands = list(video_encoder.candidates())
    return {
        # The stored setting (what the select shows) and what renders use:
        # STEMDECK_VIDEO_ENCODER wins over the setting when it is set.
        "setting": get_video_encoder(),
        "effective": value,
        "source": source,
        "candidates": cands,
        "families": {name: video_encoder.FAMILY[name] for name in [*cands, VIDEO_CPU_ENCODER]},
        "disabled": video_encoder.disabled(),
        "ffmpeg": version,
        "os": video_encoder.os_key(),
        "platform": sys.platform,
    }


def _cached_rows() -> list[dict]:
    rows: dict[str, dict] = {}
    sizes = {(w, h, f): key for key, (w, h, f) in video_encoder.TEST_SIZES.items()}
    for result in video_encoder.cached_results():
        size = sizes.get((result.width, result.height, result.fps))
        if size is None:
            continue
        row = rows.setdefault(
            result.encoder,
            {
                "encoder": result.encoder,
                "family": video_encoder.FAMILY[result.encoder],
                "disabled": result.encoder in video_encoder.disabled(),
                "results": {},
            },
        )
        row["results"][size] = result.view()
    order = list(video_encoder.candidates())
    return sorted(
        rows.values(), key=lambda r: order.index(r["encoder"]) if r["encoder"] in order else 99
    )


@router.get("/video-encoders")
async def get_video_encoders() -> dict:
    try:
        _ident, version = await asyncio.to_thread(video_encoder.ffmpeg_identity)
        rows = await asyncio.to_thread(_cached_rows)
    except Exception:
        logger.exception("video encoder listing failed")
        raise HTTPException(status_code=500, detail="video encoders could not be listed") from None
    return {**_summary(version), "results": rows, "tested": False}


@router.post("/video-encoders/test")
async def test_video_encoders() -> dict:
    if _test_lock.locked():
        raise HTTPException(status_code=409, detail="an encoder test is already running")
    async with _test_lock:
        try:
            _ident, version = await asyncio.to_thread(video_encoder.ffmpeg_identity)
            rows = await asyncio.wait_for(
                asyncio.to_thread(video_encoder.test_all, VIDEO_ENCODER_TEST_BUDGET_SEC),
                # The budget stops new probes; this bounds the request even if
                # the last probe hangs past it (each probe has its own timeout).
                timeout=VIDEO_ENCODER_TEST_BUDGET_SEC + 60,
            )
        except asyncio.TimeoutError:  # not the builtin before Python 3.11
            raise HTTPException(status_code=504, detail="the encoder test took too long") from None
        except Exception:
            logger.exception("video encoder test failed")
            raise HTTPException(status_code=500, detail="the encoder test failed") from None
    return {**_summary(version), "results": rows, "tested": True}
