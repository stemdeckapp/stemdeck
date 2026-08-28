"""Eager model pre-download for the desktop first-boot setup wizard (#275).

Run as `python -m app.pipeline.warmup`. Downloads/caches the four ML
checkpoint families StemDeck uses: Demucs (htdemucs_6s), beat-this,
All-In-One song sections, and the on-demand lead/backing vocal-split karaoke
model. This keeps a user's first real job from paying for them mid-pipeline. Invoked by the Tauri
`warmup_models` command (desktop/src-tauri/src/main.rs) as one of the setup
steps; Docker has no equivalent step and keeps the pre-existing
lazy-download-on-first-use behavior (see docs/models.md).

One line per model is printed to stdout so the caller can report per-model
status: "WARMUP_OK <name>" on success, "WARMUP_FAILED <name> <error>" on
failure. A model failing to download here is never fatal to setup -- exactly
like the lazy path it replaces, a missing model degrades only that one
feature (e.g. no beat grid, or the karaoke split unavailable until it can
download later) rather than blocking the app from starting.
"""

from __future__ import annotations

import os
import sys

from app.core.config import (
    BEAT_MODEL_CHECKPOINT,
    DEMUCS_MODEL,
    MODELS_DIR,
    SECTION_MODEL,
    VOCAL_SPLIT_MODEL,
)


def _warm_demucs() -> None:
    from demucs.pretrained import get_model

    get_model(DEMUCS_MODEL)


def _warm_beat_this() -> None:
    from beat_this.inference import Audio2Beats

    Audio2Beats(checkpoint_path=BEAT_MODEL_CHECKPOINT, device="cpu", dbn=False)


def _warm_vocal_split() -> None:
    from audio_separator.separator import Separator

    separator = Separator(
        log_level=40,  # logging.ERROR -- this is a one-shot download, not a job
        model_file_dir=str(MODELS_DIR / "audio-separator"),
    )
    separator.load_model(model_filename=VOCAL_SPLIT_MODEL)


def _warm_sections() -> None:
    # Matches app/pipeline/section_worker.py: unelevated Windows cannot create
    # the symlinks the Hugging Face cache wants, and the WinError 1314 that
    # results escapes the hub's own PermissionError fallback. Both entry points
    # that download this model must opt out, or setup fails where a real job
    # would have succeeded (and vice versa).
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

    from allin1_infer.models import load_pretrained_model

    load_pretrained_model(model_name=SECTION_MODEL, device="cpu")


_STEPS = (
    ("demucs", _warm_demucs),
    ("beat_this", _warm_beat_this),
    ("sections", _warm_sections),
    ("vocal_split", _warm_vocal_split),
)


def main() -> int:
    for name, fn in _STEPS:
        try:
            fn()
            print(f"WARMUP_OK {name}", flush=True)
        except Exception as e:
            print(f"WARMUP_FAILED {name} {e}", flush=True)
    return 0  # best-effort throughout -- see module docstring


if __name__ == "__main__":
    sys.exit(main())
