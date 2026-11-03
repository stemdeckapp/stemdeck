"""Eager model pre-download for the desktop first-boot setup wizard (#275).

Run as `python -m app.pipeline.warmup`. Downloads/caches the ML
checkpoint families StemDeck uses: Demucs (htdemucs_6s), beat-this,
All-In-One song sections, the on-demand lead/backing vocal-split karaoke
model, and Whisper for lyrics transcription when that would run. This keeps a user's first real job from paying for them mid-pipeline. Invoked by the Tauri
`warmup_models` command (desktop/src-tauri/src/main.rs) as one of the setup
steps; Docker has no equivalent step and keeps the pre-existing
lazy-download-on-first-use behavior (see docs/models.md).

Lines on stdout tell the caller where it is (#405): "WARMUP_START <name> <n>
<count>" before each model, then "WARMUP_OK <name>" on success or
"WARMUP_FAILED <name> <error>" on failure, followed by "WARMUP_OFFLINE <name>"
when the download host could not be reached or stopped answering. When the
disk cannot hold the models, the only line is "WARMUP_NO_SPACE <needed bytes>
<free bytes>" and nothing is downloaded. Every download inside gets the same
read timeout and tries as the separation model's (app/core/model_download.py),
so a dead connection ends a model in about a minute and a half instead of
holding the whole step.

A model failing to download here is never fatal to setup -- exactly like the
lazy path it replaces, a missing model degrades only that one
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
import shutil
import socket
import sys
from pathlib import Path

from app.core.config import (
    BEAT_MODEL_CHECKPOINT,
    DEMUCS_MODEL,
    MODEL_DOWNLOAD_ATTEMPTS,
    MODELS_DIR,
    SECTION_MODEL,
    TIMEOUT_MODEL_DOWNLOAD_READ,
    TRANSCRIBE_MODEL_CPU,
    TRANSCRIBE_MODEL_GPU,
    VOCAL_SPLIT_MODEL,
)
from app.core.model_download import bounded_downloads

# What each model adds to the data folder, in bytes: measured on a finished
# install (models/ and cache/huggingface/, 2026-11-03), except Whisper's
# "small", which is its published size. Only the space check before anything
# downloads uses these, so a model renamed through the environment is just not
# counted.
_DOWNLOAD_BYTES = {
    "demucs": 54_996_327,
    "beat_this": 81_058_141,
    "sections": 11_204_803,
    "vocal_split": 52_834_731,
    "turbo": 1_617_941_637,
    "small": 483_617_219,
}


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


def _whisper_model() -> str | None:
    """The Whisper model the lyrics stage would use here, or None when that
    stage would not run: under "auto" only a CUDA install transcribes."""
    from app.core.settings import get_demucs_device, transcribe_lyrics_enabled

    device = get_demucs_device()
    if not transcribe_lyrics_enabled(device):
        return None
    return TRANSCRIBE_MODEL_GPU if device == "cuda" else TRANSCRIBE_MODEL_CPU


def _warm_whisper() -> None:
    """The GPU model is 1.6 GB, so this is last in the list: a slow
    connection costs the other models nothing. A download cut short by the
    setup timeout heals itself: Whisper checks the file's SHA-256 on every
    load and fetches it again when it does not match."""
    name = _whisper_model()
    if name is None:
        return

    import whisper

    from app.pipeline.transcribe import whisper_models_dir

    whisper.load_model(name, device="cpu", download_root=str(whisper_models_dir()))


_STEPS = (
    ("demucs", _warm_demucs),
    ("beat_this", _warm_beat_this),
    ("sections", _warm_sections),
    ("vocal_split", _warm_vocal_split),
    ("whisper", _warm_whisper),
)


def _remember_vocal_split(error: Exception | None) -> None:
    """Keep the vocal split's result where the backend reads it (#832).

    setup.log alone told nobody: the Vocals choice offered Lead + Backing as
    usual and the first sign was one Vocals stem. Best-effort like the rest."""
    try:
        from app.pipeline import vocal_split_status

        if error is None:
            vocal_split_status.record(True, source="warmup")
        else:
            code = vocal_split_status.failure_code("load", error)
            vocal_split_status.record(False, code, source="warmup")
    except Exception as e:
        print(f"WARMUP_NOTE vocal_split status not recorded: {e}", flush=True)


def _model_folders() -> tuple[Path, ...]:
    """Where the models land: MODELS_DIR (torch.hub, Whisper and the vocal
    split) and the Hugging Face cache (the sections model)."""
    from huggingface_hub import constants

    return (MODELS_DIR, Path(constants.HF_HUB_CACHE))


def _free_bytes() -> int:
    """Free space on the disk MODELS_DIR is on, which may not exist yet."""
    folder = MODELS_DIR
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return shutil.disk_usage(folder).free


def _bytes_needed() -> int:
    """About what the downloads still need: every model's size, less what the
    model folders already hold, so an install that has them all needs
    nothing. Symlinks are skipped: the Hugging Face cache links each file to
    a blob that is counted already."""
    total = 0
    for name, _ in _STEPS:
        key = _whisper_model() if name == "whisper" else name
        total += _DOWNLOAD_BYTES.get(key or "", 0)
    present = sum(
        path.stat().st_size
        for folder in _model_folders()
        if folder.is_dir()
        for path in folder.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    return max(0, total - present)


def _unreachable(error: BaseException) -> bool:
    """Whether a download failed because its host could not be reached or
    stopped answering: no network, no DNS, a refused or dropped connection, a
    read that timed out. The whole chain is read, since every library wraps
    these in errors of its own (requests, httpx, the hub, ModelDownloadError)."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, (socket.gaierror, TimeoutError, ConnectionError)):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def main() -> int:
    needed, free = _bytes_needed(), _free_bytes()
    if needed > free:
        print(f"WARMUP_NO_SPACE {needed} {free}", flush=True)
        return 0
    with bounded_downloads(TIMEOUT_MODEL_DOWNLOAD_READ, MODEL_DOWNLOAD_ATTEMPTS):
        for number, (name, fn) in enumerate(_STEPS, start=1):
            print(f"WARMUP_START {name} {number} {len(_STEPS)}", flush=True)
            try:
                fn()
                print(f"WARMUP_OK {name}", flush=True)
                if name == "vocal_split":
                    _remember_vocal_split(None)
            except Exception as e:
                print(f"WARMUP_FAILED {name} {e}", flush=True)
                if _unreachable(e):
                    print(f"WARMUP_OFFLINE {name}", flush=True)
                if name == "vocal_split":
                    _remember_vocal_split(e)
    return 0  # best-effort throughout -- see module docstring


if __name__ == "__main__":
    sys.exit(main())
