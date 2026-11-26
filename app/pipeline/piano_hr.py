"""The second piano engine: ByteDance's high-resolution piano transcription.

Kong et al. 2021, "High-resolution Piano Transcription with Pedals by
Regressing Onset and Offset Times": piano_transcription_inference 0.0.6 and
torchlibrosa 0.1.0 (MIT), vendored in app/_vendor/piano_hr (its README says
what was changed, and why), run on the torch StemDeck already ships. Its
weights (172 MB, CC BY 4.0) are not in the package: they are fetched on first
use, by the parent, never by the worker, which has no network
(app/pipeline/midi.py, app/core/model_fetch.py).

Two halves, like midi_file.py:

- In the server: where the vendored code and the checkpoint live, what they
  are (manifest.json), and whether the checkpoint on disk is the pinned file
  (checkpoint_ok). Nothing here imports torch or the vendored package.
- In the worker (midi_worker.py, once _install_engine has put the vendored
  folder on sys.path): load_model, model_output, output_notes. Each imports
  what it needs when it is called.

The package's PianoTranscription class (inference.py) is left out of the
vendored copy: when its checkpoint is missing its __init__ downloads it with
os.system("wget ..."), it eval()s its model name and it torch.loads without
weights_only. What StemDeck needs of it, cutting the audio into segments and
joining their outputs (enframe, deframe), is here, the same steps and
arithmetic (MIT, Qiuqiang Kong); tests/test_midi_worker.py checks them.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import (
    PIANO_HR_CHECKPOINT_RELPATH,
    PIANO_HR_CHECKPOINT_SHA256,
    PIANO_HR_CHECKPOINT_SIZE,
    PIANO_HR_CHECKPOINT_URL,
)

VENDOR_DIR = Path(__file__).resolve().parent.parent / "_vendor" / "piano_hr"
MANIFEST_PATH = VENDOR_DIR / "manifest.json"

ENGINE_ID = "piano_hr"
ENGINE_LABEL = "ByteDance Piano Transcription"
ENGINE_LICENCE = "MIT"
MODEL_LICENCE = "CC BY 4.0"

# The model's own constants (piano_transcription_inference/config.py): it
# hears 16 kHz audio, 100 frames a second, the 88 keys from A0 (MIDI 21), and
# was trained on 10 s segments, which PianoTranscription takes 5 s apart.
SAMPLE_RATE = 16_000
SEGMENT_SAMPLES = SAMPLE_RATE * 10
FRAMES_PER_SECOND = 100
CLASSES = 88
VELOCITY_SCALE = 128


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    """manifest.json as written when the engine was vendored, or {}."""
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def engine_version() -> str | None:
    for package in manifest().get("packages") or []:
        if isinstance(package, dict) and package.get("name") == "piano-transcription-inference":
            version = package.get("version")
            return version if isinstance(version, str) else None
    return None


def vendor_id() -> str | None:
    """A short id of the exact vendored sdists (a hash of their sha256s)."""
    value = manifest().get("id")
    return value if isinstance(value, str) else None


def vendored() -> bool:
    """Whether the vendored code is all there (cheap file lookups)."""
    return (
        engine_version() is not None
        and (VENDOR_DIR / "piano_transcription_inference" / "models.py").is_file()
        and (VENDOR_DIR / "torchlibrosa" / "stft.py").is_file()
    )


# -- the checkpoint


def checkpoint_file():
    """The checkpoint as a pinned stem_models.ModelFile, under MODELS_DIR."""
    from app.core.stem_models import ModelFile

    return ModelFile(
        url=PIANO_HR_CHECKPOINT_URL,
        sha256=PIANO_HR_CHECKPOINT_SHA256,
        size=PIANO_HR_CHECKPOINT_SIZE,
        relpath=PIANO_HR_CHECKPOINT_RELPATH,
    )


def checkpoint_path() -> Path:
    from app.core.stem_models import model_path

    return model_path(checkpoint_file())


def checkpoint_state() -> str:
    """ "downloaded" (the pinned size), "partial" (another size) or
    "not_downloaded". Size only: checkpoint_ok reads the whole file."""
    try:
        size = checkpoint_path().stat().st_size
    except OSError:
        return "not_downloaded"
    return "downloaded" if size == PIANO_HR_CHECKPOINT_SIZE else "partial"


# (path, size, mtime_ns, inode) of the last file whose SHA-256 matched, so a
# run hashes 172 MB again only when the file changed since.
_verified: tuple[str, int, int, int] | None = None
_verified_lock = threading.Lock()


def _identity(path: Path) -> tuple[str, int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_size, st.st_mtime_ns, st.st_ino)


def checkpoint_ok(path: Path | None = None) -> bool:
    """Whether the checkpoint on disk is exactly the pinned file: its size,
    then its SHA-256 (about half a second, once per change of the file)."""
    global _verified
    path = path or checkpoint_path()
    ident = _identity(path)
    if ident is None or ident[1] != PIANO_HR_CHECKPOINT_SIZE:
        return False
    with _verified_lock:
        if _verified == ident:
            return True
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
    except OSError:
        return False
    if digest.hexdigest() != PIANO_HR_CHECKPOINT_SHA256:
        return False
    with _verified_lock:
        # Kept only if the file did not change while it was read.
        _verified = ident if _identity(path) == ident else None
    return True


def remove_bad_checkpoint(path: Path | None = None) -> bool:
    """Remove a checkpoint that is not the pinned file, so the next run
    downloads it again. True when one was removed."""
    path = path or checkpoint_path()
    if not path.exists() or checkpoint_ok(path):
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    return True


# -- in the worker


def load_model(checkpoint: Path, device: str):
    """The Note_pedal model with the checkpoint's weights, on ``device``, in
    eval mode. torch.load with weights_only: the file is a pickle, and only
    tensors and plain containers may come out of it. Every weight must fit
    the model (strict): a file that loads but is not this model is refused."""
    import torch
    from piano_transcription_inference.models import Note_pedal

    model = Note_pedal(frames_per_second=FRAMES_PER_SECOND, classes_num=CLASSES)
    state = torch.load(str(checkpoint), map_location="cpu", weights_only=True)
    weights = state["model"]
    model.note_model.load_state_dict(weights["note_model"], strict=True)
    model.pedal_model.load_state_dict(weights["pedal_model"], strict=True)
    model.to(torch.device(device))
    model.eval()
    return model


def read_audio(path: Path):
    """The file as mono float32 at the model's 16 kHz. librosa.load, as Basic
    Pitch reads a stem: soundfile first (wav, flac, mp3), then audioread."""
    import librosa
    import numpy as np

    audio, _sr = librosa.load(str(path), sr=SAMPLE_RATE, mono=True)
    return np.asarray(audio, dtype=np.float32)


def enframe(x, segment_samples: int):
    """(1, n) audio, n a whole number of segments, as (segments,
    segment_samples): each segment starts half a segment after the one
    before. PianoTranscription.enframe."""
    import numpy as np

    assert x.shape[1] % segment_samples == 0
    batch = []
    pointer = 0
    while pointer + segment_samples <= x.shape[1]:
        batch.append(x[:, pointer : pointer + segment_samples])
        pointer += segment_samples // 2
    return np.concatenate(batch, axis=0)


def deframe(x):
    """(segments, frames, classes) back to (frames, classes): the last frame
    of each segment dropped (the spectrogram's centre padding adds it), then
    the first three quarters of the first segment, the middle halves of the
    others and the last three quarters of the last. PianoTranscription.deframe."""
    import numpy as np

    if x.shape[0] == 1:
        return x[0]
    x = x[:, 0:-1, :]
    _n, frames, _classes = x.shape
    assert frames % 4 == 0
    y = [x[0, 0 : int(frames * 0.75)]]
    for i in range(1, x.shape[0] - 1):
        y.append(x[i, int(frames * 0.25) : int(frames * 0.75)])
    y.append(x[-1, int(frames * 0.25) :])
    return np.concatenate(y, axis=0)


def model_output(model, audio, device: str, batch: int, on_pct=None) -> dict:
    """The model's activations over the whole of ``audio`` (16 kHz mono).

    PianoTranscription.transcribe's steps: pad to whole segments, enframe 10 s
    segments 5 s apart, run them, deframe. The segments run ``batch`` at a
    time rather than one by one, each batch a progress line (``on_pct``, 5
    to 90). transcribe then slices the output at the audio's length in
    samples, more than there are frames, so the padding's frames stay; so do
    they here (silence, no notes), to keep its output exactly."""
    import numpy as np
    import torch

    if audio.size == 0:
        raise RuntimeError("the audio has no samples")
    x = audio[None, :]
    audio_len = x.shape[1]
    pad_len = int(np.ceil(audio_len / SEGMENT_SAMPLES)) * SEGMENT_SAMPLES - audio_len
    x = np.concatenate((x, np.zeros((1, pad_len))), axis=1)
    segments = enframe(x, SEGMENT_SAMPLES)
    total = len(segments)
    outputs: dict[str, list] = {}
    target = torch.device(device)
    if on_pct is not None:
        on_pct(5)
    with torch.inference_mode():
        for start in range(0, total, max(1, batch)):
            chunk = torch.from_numpy(
                np.ascontiguousarray(segments[start : start + batch], dtype=np.float32)
            ).to(target)
            for key, value in model(chunk).items():
                outputs.setdefault(key, []).append(value.float().cpu().numpy())
            if on_pct is not None:
                on_pct(5 + min(85, 85 * min(total, start + batch) // total))
    return {
        key: deframe(np.concatenate(parts, axis=0))[0:audio_len] for key, parts in outputs.items()
    }


def output_notes(output: dict, params: dict) -> list:
    """Note events (start s, end s, MIDI pitch, amplitude 0..1, None) from
    the activations, under ``params`` (midi.run_params): the package's own
    post-processing at the preset's thresholds, then each note held by the
    sustain pedal it detected (midi_notes.pedal_extend), the way the piano
    sounded, since the file carries no pedal of its own."""
    from piano_transcription_inference.utilities import RegressionPostProcessor

    from app.pipeline import midi_notes

    post = RegressionPostProcessor(
        FRAMES_PER_SECOND,
        classes_num=CLASSES,
        onset_threshold=params["onset_threshold"],
        offset_threshold=params["offset_threshold"],
        frame_threshold=params["frame_threshold"],
        pedal_offset_threshold=params["pedal_offset_threshold"],
    )
    # It adds keys to the dict it is given: a copy, so one output decodes
    # again under other thresholds.
    notes, pedals = post.output_dict_to_midi_events(dict(output))
    events = [
        (float(n["onset_time"]), float(n["offset_time"]), int(n["midi_note"]), int(n["velocity"]))
        for n in notes
    ]
    if params.get("pedal", True):
        spans = [(float(p["onset_time"]), float(p["offset_time"])) for p in pedals or []]
        events = midi_notes.pedal_extend(events, spans)
    return [(a, b, pitch, vel / (VELOCITY_SCALE - 1), None) for a, b, pitch, vel in events]
