"""Settings, Models: every built-in model, what it is for, and what it holds on disk.

Read-only. GET /api/models (app/api/config.py) serves inventory(), and the page
groups it by purpose and adds what each model is used for from the per-stem
choices it already has (/api/settings). Nothing here downloads, loads or
removes a model.

Each entry names no path, only states and sizes: the folders are this
machine's (a user name, a drive), and the page has no use for them.

    {"id", "group", "use", "name", "stems", "license", "state",
     "size_bytes", "download_bytes", "available"}

group is "separation", "transcription" or "analysis". use says what the
model does inside its group: "stems" (it separates the stems listed),
"lead_backing" (the Karaoke split of the vocals), "midi" (Basic Pitch),
"midi_piano" (the second piano engine, piano_hr), "lyrics", "lyrics_gpu",
"lyrics_cpu" (Whisper) or "beats" (beat_this).

state is one of:
    "downloaded"      every file is there (a pinned file at its pinned size)
    "partial"         some of its files are there, not all
    "not_downloaded"  none of them; the first job that needs it fetches it
    "built_in"        it ships inside StemDeck (Basic Pitch): nothing to fetch
    "unknown"         where it would be could not be worked out (torch does
                      not load, #730), so nothing is claimed

size_bytes is what its files hold on disk now (None when nothing is there or
the state is unknown); download_bytes is what a full download is, when the
code knows it.

Where each model lives is asked of the code that downloads it, never worked
out again here: stem_models.model_path() for the separation models (torch.hub's
checkpoints folder, and MODELS_DIR for Kim), model_cache.vocal_split_artifacts()
for the Karaoke split, piano_hr.checkpoint_path() for the second piano engine
(MODELS_DIR), model_cache.beat_this_artifacts() for the beat model,
transcribe.whisper_models_dir() for Whisper. Two models the code does not place
are left out: the song-sections model, which the Hugging Face hub caches under
a layout only allin1_infer names, and the models a person added, which
GET /api/my-models lists with their sizes.

Removing a built-in model is not offered: a separation or a split that is
running may be reading the file, and torch.hub or audio-separator would fetch
it again on the next job anyway. That needs its own design (holding the
pipeline lock, and a state for "removed, fetch on next use").
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from app.core import stem_models
from app.core.config import (
    BEAT_MODEL_CHECKPOINT,
    MODELS_DIR,
    TRANSCRIBE_MODEL_CPU,
    TRANSCRIBE_MODEL_GPU,
    VOCAL_SPLIT_MODEL,
)

logger = logging.getLogger("stemdeck.models")

# The default Karaoke split model and its licence (docs/models.md). A model a
# deployment swapped in with STEMDECK_KARAOKE_MODEL is listed by its file name,
# with no licence: the code does not know it.
_KARAOKE_DEFAULT = "UVR_MDXNET_KARA_2.onnx"
_KARAOKE_NAME = "UVR-MDX-NET Karaoke 2"
_KARAOKE_LICENSE = "MIT"
# Whisper's file for a model name, where it is not "<name>.pt": the basename of
# its address in whisper._MODELS (openai-whisper 20250625). Read from here, not
# from the package, because importing whisper loads numba, seconds of work the
# server never needs. A name missing from this table and not "<name>.pt" reads
# as not downloaded, which is wrong only in what it shows.
_WHISPER_FILES = {"turbo": "large-v3-turbo.pt", "large": "large-v3.pt"}
# What a full download is, from app/pipeline/warmup.py (its published sizes).
_WHISPER_BYTES = {"turbo": 1_617_941_637, "small": 483_617_219}
_BEAT_BYTES = 81_058_141
_KARAOKE_BYTES = 52_834_731
_WHISPER_LICENSE = "MIT"

STATES = ("downloaded", "partial", "not_downloaded", "built_in", "unknown")


def _file_size(path: Path) -> int | None:
    """The size of a regular file, or None when there is none."""
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _disk(
    files: Iterable[tuple[Path, int | None]],
) -> tuple[str, int | None]:
    """(state, size on disk) for a model made of ``files``: each a path and
    the size it must be, or None when any non-empty file counts."""
    total = 0
    present = 0
    complete = 0
    count = 0
    for path, expected in files:
        count += 1
        size = _file_size(path)
        if size is None or size <= 0:
            continue
        present += 1
        total += size
        if expected is None or size == expected:
            complete += 1
    if count and complete == count:
        return "downloaded", total
    if present:
        return "partial", total
    return "not_downloaded", None


def _located(where: Callable[[], Iterable[tuple[Path, int | None]]]) -> tuple[str, int | None]:
    """_disk() of the files ``where`` names, or "unknown" when naming them
    failed (torch.hub's folder needs torch, which may not load)."""
    try:
        files = list(where())
    except Exception:  # noqa: BLE001 -- the list must answer; "unknown" claims nothing
        logger.info("models: a model's folder could not be worked out", exc_info=True)
        return "unknown", None
    return _disk(files)


def _has_package(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _entry(
    model_id: str,
    group: str,
    use: str,
    name: str,
    *,
    stems: Iterable[str] = (),
    license: str | None,
    state: str,
    size: int | None,
    download_bytes: int | None,
    available: bool,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "group": group,
        "use": use,
        "name": name,
        "stems": list(stems),
        "license": license,
        "state": state,
        "size_bytes": size,
        "download_bytes": download_bytes,
        "available": bool(available),
    }


def _separation() -> list[dict[str, Any]]:
    out = []
    for model in stem_models.MODELS.values():
        state, size = _located(
            lambda m=model: [(stem_models.model_path(f), f.size) for f in m.files]
        )
        try:
            available = stem_models.model_available(model.id)
        except Exception:  # noqa: BLE001 -- a lookup that fails is "cannot run"
            available = False
        out.append(
            _entry(
                model.id,
                "separation",
                "stems",
                model.name,
                stems=[s for s in (*stem_models.SELECTABLE_STEMS, "other") if s in model.stems],
                license=model.license,
                state=state,
                size=size,
                download_bytes=model.download_bytes,
                available=available,
            )
        )
    return out


def _karaoke() -> dict[str, Any]:
    from app.core.model_cache import vocal_split_artifacts

    default = VOCAL_SPLIT_MODEL == _KARAOKE_DEFAULT
    # The model file only: the indexes beside it are audio-separator's own.
    state, size = _located(
        lambda: [(vocal_split_artifacts(MODELS_DIR, VOCAL_SPLIT_MODEL)[0], None)]
    )
    try:
        from app.pipeline.vocal_split_status import capability

        available = capability().get("available") is True
    except Exception:  # noqa: BLE001
        available = False
    return _entry(
        "uvr_karaoke_2" if default else "karaoke",
        "separation",
        "lead_backing",
        _KARAOKE_NAME if default else VOCAL_SPLIT_MODEL,
        stems=["lead_backing"],
        license=_KARAOKE_LICENSE if default else None,
        state=state,
        size=size,
        download_bytes=_KARAOKE_BYTES if default else None,
        available=available,
    )


def _basic_pitch() -> dict[str, Any]:
    from app.pipeline import midi, midi_file

    try:
        available = midi.availability()[0]
    except Exception:  # noqa: BLE001
        available = False
    return _entry(
        midi_file.ENGINE_ID,
        "transcription",
        "midi",
        midi_file.ENGINE_LABEL,
        stems=["piano", "bass"],
        license=midi_file.ENGINE_LICENCE,
        state="built_in",
        size=_file_size(midi_file.MODEL_PATH),
        download_bytes=None,
        available=available,
    )


def _piano_hr() -> dict[str, Any]:
    from app.core.config import PIANO_HR_CHECKPOINT_SIZE
    from app.pipeline import midi, piano_hr

    try:
        available = midi.engine_availability(piano_hr.ENGINE_ID)[0]
    except Exception:  # noqa: BLE001
        available = False
    state, size = _located(lambda: [(piano_hr.checkpoint_path(), PIANO_HR_CHECKPOINT_SIZE)])
    return _entry(
        piano_hr.ENGINE_ID,
        "transcription",
        "midi_piano",
        piano_hr.ENGINE_LABEL,
        stems=["piano"],
        # The weights' licence: the code is MIT (GET /api/transcribe/status).
        license=piano_hr.MODEL_LICENCE,
        state=state,
        size=size,
        download_bytes=PIANO_HR_CHECKPOINT_SIZE,
        available=available,
    )


def whisper_file(name: str) -> str:
    """The file Whisper keeps model ``name`` in."""
    return _WHISPER_FILES.get(name, f"{name}.pt")


def _whisper() -> list[dict[str, Any]]:
    from app.pipeline.transcribe import whisper_models_dir

    available = _has_package("whisper")
    if TRANSCRIBE_MODEL_GPU == TRANSCRIBE_MODEL_CPU:
        roles = [(TRANSCRIBE_MODEL_GPU, "lyrics")]
    else:
        roles = [(TRANSCRIBE_MODEL_GPU, "lyrics_gpu"), (TRANSCRIBE_MODEL_CPU, "lyrics_cpu")]
    out = []
    for name, use in roles:
        state, size = _located(lambda n=name: [(whisper_models_dir() / whisper_file(n), None)])
        out.append(
            _entry(
                f"whisper_{name}",
                "analysis",
                use,
                f"Whisper {name}",
                license=_WHISPER_LICENSE,
                state=state,
                size=size,
                download_bytes=_WHISPER_BYTES.get(name),
                available=available,
            )
        )
    return out


def _beat_this() -> dict[str, Any]:
    from app.core.model_cache import beat_this_artifacts

    state, size = _located(lambda: [(p, None) for p in beat_this_artifacts(BEAT_MODEL_CHECKPOINT)])
    default = BEAT_MODEL_CHECKPOINT == "final0"
    return _entry(
        "beat_this",
        "analysis",
        "beats",
        f"Beat This! ({BEAT_MODEL_CHECKPOINT})",
        license=None,
        state=state,
        size=size,
        download_bytes=_BEAT_BYTES if default else None,
        available=_has_package("beat_this"),
    )


def inventory() -> dict[str, Any]:
    """GET /api/models. Blocking (file lookups, and torch's folder may import
    torch): the route runs it in the thread pool."""
    return {
        "models": [
            *_separation(),
            _karaoke(),
            _basic_pitch(),
            _piano_hr(),
            *_whisper(),
            _beat_this(),
        ],
        # Built-in models are listed, never removed here (the module docstring).
        "remove": False,
    }
