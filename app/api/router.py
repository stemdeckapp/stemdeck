from __future__ import annotations

from fastapi import APIRouter

from app.api.config import router as config_router
from app.api.events import router as events_router
from app.api.jobs import router as jobs_router
from app.api.stems import router as stems_router

router = APIRouter()
router.include_router(config_router, tags=["config"])
router.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
router.include_router(events_router, tags=["events"])
router.include_router(stems_router, tags=["stems"])
