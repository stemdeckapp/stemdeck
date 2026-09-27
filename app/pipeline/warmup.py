"""Eager model pre-download for the desktop first-boot setup wizard (#275).

Run as `python -m app.pipeline.warmup`. Downloads/caches the ML
checkpoint families StemDeck uses: Demucs (htdemucs_6s), beat-this,
All-In-One song sections, the on-demand lead/backing vocal-split karaoke
model, and Whisper for lyrics transcription when that would run. This keeps a user's first real job from paying for them mid-pipeline. Invoked by the Tauri
`warmup_models` command (desktop/src-tauri/src/main.rs) as one of the setup
steps; Docker has no equivalent step and keeps the pre-existing
lazy-download-on-first-use behavior (see docs/models.md).

One line per model is printed to stdout so the caller can report per-model
status: "WARMUP_OK <name>" on success, "WARMUP_FAILED <name> <error>" on
failure. A model failing to download here is never fatal to setup -- exactly
like the lazy path it replaces, a missing model degrades only that one
feature (e.g. no beat grid, or the karaoke split unavailable until it can
download later) rather than blocking the app from starting.

"Until it can download later" depends on the cache being empty rather than
wrong. A checkpoint truncated by a dropped connection is still a file, and
the loader takes a file's existence as proof it is good, so the retry never
fires and the feature stays dead (#502).

The two that reported that failure in the wild, beat_this and the vocal-split
model, therefore load through app/core/model_cache.load_or_heal. Demucs and
the section model do not: they cache through their own libraries rather than
torch.hub, and neither has been seen to poison itself this way. Adding them
would mean naming their cache layouts here and keeping those names correct
from a distance, which is the mistake that made the first version of
vocal_split_artifacts point at a file audio-separator never writes.
"""

from __future__ import annotations

import os
import sys

from app.core.config import (
    BEAT_MODEL_CHECKPOINT,
    DEMUCS_MODEL,
    MODELS_DIR,
    SECTION_MODEL,
    TRANSCRIBE_MODEL_CPU,
    TRANSCRIBE_MODEL_GPU,
    VOCAL_SPLIT_MODEL,
)


def _warm_demucs() -> None:
    from demucs.pretrained import get_model

    get_model(DEMUCS_MODEL)


def _warm_beat_this() -> None:
    from beat_this.inference import Audio2Beats

    from app.core.model_cache import beat_this_artifacts, load_or_heal

    load_or_heal(
        lambda: Audio2Beats(checkpoint_path=BEAT_MODEL_CHECKPOINT, device="cpu", dbn=False),
        lambda: beat_this_artifacts(BEAT_MODEL_CHECKPOINT),
    )


def _warm_vocal_split() -> None:
    from audio_separator.separator import Separator

    from app.core.model_cache import load_or_heal, vocal_split_artifacts

    def load() -> None:
        separator = Separator(
            log_level=40,  # logging.ERROR -- this is a one-shot download, not a job
            model_file_dir=str(MODELS_DIR / "audio-separator"),
        )
        separator.load_model(model_filename=VOCAL_SPLIT_MODEL)

    load_or_heal(load, lambda: vocal_split_artifacts(MODELS_DIR, VOCAL_SPLIT_MODEL))


def _warm_sections() -> None:
    # Matches app/pipeline/section_worker.py: unelevated Windows cannot create
    # the symlinks the Hugging Face cache wants, and the WinError 1314 that
    # results escapes the hub's own PermissionError fallback. Both entry points
    # that download this model must opt out, or setup fails where a real job
    # would have succeeded (and vice versa).
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

    from allin1_infer.models import load_pretrained_model

    load_pretrained_model(model_name=SECTION_MODEL, device="cpu")


def _warm_whisper() -> None:
    """The Whisper model the lyrics stage would use here, and only when that
    stage would run: under "auto" only a CUDA install transcribes, and the
    GPU model is 1.6 GB. Last in the list for the same reason, so a slow
    connection costs the other models nothing. A download cut short by the
    setup timeout heals itself: Whisper checks the file's SHA-256 on every
    load and fetches it again when it does not match."""
    from app.core.settings import get_demucs_device, transcribe_lyrics_enabled

    device = get_demucs_device()
    if not transcribe_lyrics_enabled(device):
        return

    import whisper

    from app.pipeline.transcribe import whisper_models_dir

    name = TRANSCRIBE_MODEL_GPU if device == "cuda" else TRANSCRIBE_MODEL_CPU
    whisper.load_model(name, device="cpu", download_root=str(whisper_models_dir()))


_STEPS = (
    ("demucs", _warm_demucs),
    ("beat_this", _warm_beat_this),
    ("sections", _warm_sections),
    ("vocal_split", _warm_vocal_split),
    ("whisper", _warm_whisper),
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
