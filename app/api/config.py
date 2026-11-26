from __future__ import annotations

from fastapi import APIRouter

from app.core import model_inventory
from app.core.config import EXTRA_STEM_NAMES, STEM_NAMES

router = APIRouter()


@router.get("/config")
def get_config() -> dict:
    # extra_stem_names (#275) are produced only when a job's on-demand
    # lead/backing vocal split has run -- kept separate from stem_names so
    # existing clients that assume "every job produces exactly these stems"
    # are unaffected; new clients merge it into their lane vocab (see
    # syncStemNamesFromAPI in static/js/constants.js).
    return {"stem_names": list(STEM_NAMES), "extra_stem_names": list(EXTRA_STEM_NAMES)}


@router.get("/models")
def list_models() -> dict:
    """Settings, Models: every built-in model, its purpose, and its state and
    size on disk (app/core/model_inventory.py). Read-only, and no path in the
    answer. A plain def: FastAPI runs it in the thread pool, since it looks at
    files and may import torch to find torch.hub's folder."""
    return model_inventory.inventory()
