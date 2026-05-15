from __future__ import annotations

from fastapi import APIRouter

from app.core.config import STEM_NAMES

router = APIRouter()


@router.get("/config")
def get_config() -> dict:
    return {"stem_names": list(STEM_NAMES)}
