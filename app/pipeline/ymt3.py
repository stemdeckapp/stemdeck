"""The third MIDI engine: YourMT3+, every instrument of a stem at once.

Chang et al. 2024, "YourMT3+: Multi-instrument Music Transcription with
Enhanced Transformer Architectures and Cross-dataset Stem Augmentation", the
YPTF.MoE+Multi variant trained without pitch-shift augmentation ("noPS"). Its
inference code is vendored in app/_vendor/ymt3 (its README says what was
taken and changed, and why) and runs on the torch StemDeck already ships.
Its weights are fetched on first use by the parent, never by the worker,
which has no network (app/pipeline/midi.py, app/core/model_fetch.py), then
converted once on this computer into a weights-only file
(app/pipeline/ymt3_convert.py).

Three parts, like piano_hr.py:

- In the server: where the vendored code, the checkpoint and the weights
  live, what they are (manifest.json), and whether each file on disk is the
  pinned one. Nothing here imports torch or the vendored code.
- The conversion (convert_checkpoint), run in its own process: the
  checkpoint's tensors read with torch's weights-only loader into the
  deterministic safetensors file.
- In the worker (midi_worker.py, once _install_engine has put the vendored
  folders on sys.path): build_model, load_weights, read_audio, model_output,
  decode, stem_notes. Each imports what it needs when it is called.

The model hears a whole mix and names each note's instrument (a General MIDI
program, or drums). A stem separated from a song still holds some of the
others, so a stem's file keeps only the notes of its own instrument class
(config.MIDI_YMT3_CLASSES): on separated stems that was worth 0.108
(guitar) to 0.218 (other) onset F1 (data/midi-bench/results/
engines-per-stem.md).
"""

from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import (
    MIDI_YMT3_CLASSES,
    YMT3_CHECKPOINT_RELPATH,
    YMT3_CHECKPOINT_SHA256,
    YMT3_CHECKPOINT_SIZE,
    YMT3_CHECKPOINT_URL,
    YMT3_NOTE_AMPLITUDE,
    YMT3_WEIGHTS_RELPATH,
    YMT3_WEIGHTS_SHA256,
    YMT3_WEIGHTS_SIZE,
)
from app.core.pinned_file import PinnedFile

VENDOR_DIR = Path(__file__).resolve().parent.parent / "_vendor" / "ymt3"
SRC_DIR = VENDOR_DIR / "src"
SHIM_DIR = VENDOR_DIR / "shim"
MANIFEST_PATH = VENDOR_DIR / "manifest.json"

ENGINE_ID = "ymt3"
ENGINE_LABEL = "YourMT3+"
ENGINE_LICENCE = "Apache-2.0"
MODEL_LICENCE = "Apache-2.0"

# The checkpoint as published, and the weights-only file made from it.
CHECKPOINT = PinnedFile(
    url=YMT3_CHECKPOINT_URL,
    sha256=YMT3_CHECKPOINT_SHA256,
    size=YMT3_CHECKPOINT_SIZE,
    relpath=YMT3_CHECKPOINT_RELPATH,
)
WEIGHTS = PinnedFile(
    url=None,
    sha256=YMT3_WEIGHTS_SHA256,
    size=YMT3_WEIGHTS_SIZE,
    relpath=YMT3_WEIGHTS_RELPATH,
)

# The pickled objects of the checkpoint besides its tensors: the training
# run's task manager and tokenizer, the event codec they hold, and numpy
# values inside them. torch's weights-only loader refuses any global it does
# not know; each of these is given _Inert, which takes its arguments and
# keeps nothing, so no code of the file runs and nothing it names has to be
# importable. The list is exactly what the pinned file holds: another file
# with another global is refused by the loader, not run.
CHECKPOINT_GLOBALS = (
    "_codecs.encode",
    "numpy.core.multiarray._reconstruct",
    "numpy.core.multiarray.scalar",
    "numpy.dtype",
    "numpy.ndarray",
    "utils.event_codec.FastCodec",
    "utils.note_event_dataclasses.Event",
    "utils.note_event_dataclasses.EventRange",
    "utils.task_manager.TaskManager",
    "utils.tokenizer.NoteEventTokenizer",
)

# The model's input (app/_vendor/ymt3/src/config/config.py, audio_cfg): 16 kHz
# mono in segments of 32767 samples (2.05 s), one after another, no overlap,
# as the authors' own transcribe() cuts a file.
SAMPLE_RATE = 16_000
SEGMENT_SAMPLES = 32_767
# The variant's training arguments (the experiment name of the checkpoint,
# mc13_256_g4_all_v7_mt3f_sqr_rms_moe_wf4_n8k2_silu_rope_rp_b36_nops, as the
# Space's app.py passes them): the 13-class multi-channel task with 256 tokens
# a segment, a Perceiver-TF encoder with 26 latents, attention to channel,
# a mixture of 8 experts (top 2) with SiLU, rotary positions on part of each
# head, a multi-channel T5 decoder, and the linear spectrogram, hop 300.
TASK_NAME = "mc13_full_plus_256"


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    """manifest.json as written when the code was vendored, or {}."""
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def engine_version() -> str | None:
    """The Space revision the code came from, its first 7 characters."""
    source = manifest().get("source")
    revision = source.get("revision") if isinstance(source, dict) else None
    return revision[:7] if isinstance(revision, str) and len(revision) >= 7 else None


def vendor_id() -> str | None:
    """A short id of the exact vendored files (manifest.json's id)."""
    value = manifest().get("id")
    return value if isinstance(value, str) else None


def vendored() -> bool:
    """Whether the vendored code is all there (cheap file lookups)."""
    return (
        engine_version() is not None
        and (SRC_DIR / "model" / "ymt3.py").is_file()
        and (SRC_DIR / "utils" / "task_manager.py").is_file()
        and (SHIM_DIR / "transformers" / "models" / "t5" / "modeling_t5.py").is_file()
        and (SHIM_DIR / "pytorch_lightning" / "__init__.py").is_file()
    )


# -- the weights


def weights_path() -> Path:
    return WEIGHTS.path()


def checkpoint_path() -> Path:
    return CHECKPOINT.path()


def model_state() -> str:
    """What the status and Settings say about the model: "downloaded" when
    the weights file is there at its pinned size, "partial" when a download
    or a conversion was left part way (a checkpoint, or a file of another
    size), else "not_downloaded". Sizes only."""
    state = WEIGHTS.state()
    if state != "not_downloaded":
        return state
    return "not_downloaded" if CHECKPOINT.state() == "not_downloaded" else "partial"


def weights_ok(path: Path | None = None) -> bool:
    return WEIGHTS.ok(path or weights_path())


def checkpoint_ok(path: Path | None = None) -> bool:
    return CHECKPOINT.ok(path or checkpoint_path())


# -- the conversion (its own process: app/pipeline/ymt3_convert.py)


class _Inert:
    """What every pickled object of the checkpoint becomes: an instance
    that takes any arguments and state and keeps none of it."""

    def __new__(cls, *args: Any, **kwargs: Any) -> _Inert:
        return object.__new__(cls)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __setstate__(self, state: Any) -> None:
        pass

    def __call__(self, *args: Any, **kwargs: Any) -> _Inert:
        return _Inert()


def checkpoint_tensors(path: Path) -> dict[str, Any]:
    """The model's tensors in the checkpoint at ``path``, read with torch's
    weights-only loader (no pickle module here), the names in
    CHECKPOINT_GLOBALS given _Inert. Only the "state_dict" is kept: the
    optimizer's state and the training metadata are left behind."""
    import torch

    stubs = [(_Inert, name) for name in CHECKPOINT_GLOBALS]
    with torch.serialization.safe_globals(stubs):
        checkpoint = torch.load(str(path), map_location="cpu", weights_only=True, mmap=True)
    state = checkpoint.get("state_dict") if isinstance(checkpoint, dict) else None
    if not isinstance(state, dict) or not state:
        raise ValueError("the checkpoint has no state_dict")
    out = {}
    for name, tensor in state.items():
        if not isinstance(name, str) or not isinstance(tensor, torch.Tensor):
            raise ValueError("the state_dict holds something other than named tensors")
        # The authors' loader drops the training-only pitch-shift layer
        # (model_helper.load_model_checkpoint); this checkpoint has none.
        if "pitchshift" in name:
            continue
        out[name] = tensor.detach().clone().contiguous()
    return out


def convert_checkpoint(src: Path, dst: Path) -> int:
    """Write the weights-only file of the checkpoint at ``src`` to ``dst``
    (tensor_file.write: sorted, synced). Returns the number of tensors.
    Raises OSError, ValueError."""
    from app.core import tensor_file

    tensors = checkpoint_tensors(src)
    tensor_file.write(dst, tensors, {"format": "pt", "source": "mimbres/YourMT3 last.ckpt"})
    return len(tensors)


# -- in the worker


def build_config() -> tuple[dict, dict, dict]:
    """(audio_cfg, model_cfg, shared_cfg) of this variant: the vendored
    config.py's defaults, copied, with what the authors' update_config sets
    for the variant's arguments (model/init_train.py, not vendored, at the
    Space revision in manifest.json)."""
    from copy import deepcopy

    from config.config import audio_cfg, model_cfg, shared_cfg

    audio, model, shared = deepcopy(audio_cfg), deepcopy(model_cfg), deepcopy(shared_cfg)
    audio["codec"] = "spec"
    audio["hop_length"] = 300
    tokenizer = shared["TOKENIZER"]
    if tokenizer["max_shift_steps"] == "auto":
        step = tokenizer["shift_step_ms"] / 1000
        tokenizer["max_shift_steps"] = int(
            (audio["input_frames"] / audio["sample_rate"]) // step + 2
        )
    model["encoder_type"] = "perceiver-tf"
    model["decoder_type"] = "multi-t5"
    model["use_task_conditional_encoder"] = True
    model["use_task_conditional_decoder"] = True
    perceiver = model["encoder"]["perceiver-tf"]
    perceiver["position_encoding_type"] = "rope"
    perceiver["num_latents"] = 26
    perceiver["attention_to_channel"] = True
    perceiver["sca_use_query_residual"] = True
    perceiver["ff_layer_type"] = "moe"
    perceiver["ff_widening_factor"] = 4
    perceiver["moe_num_experts"] = 8
    perceiver["moe_topk"] = 2
    perceiver["hidden_act"] = "silu"
    perceiver["rope_partial_pe"] = True
    return audio, model, shared


def build_model():
    """The model with fresh weights, in eval mode, on the CPU. Matrix
    products at torch's "high" precision, as the authors' loader sets it
    (TF32 on a GPU that has it): the scores in engines-per-stem.md ran so."""
    import torch
    from model.ymt3 import YourMT3
    from utils.task_manager import TaskManager

    torch.set_float32_matmul_precision("high")
    audio_cfg, model_cfg, shared_cfg = build_config()
    manager = TaskManager(
        task_name=TASK_NAME,
        max_shift_steps=int(shared_cfg["TOKENIZER"]["max_shift_steps"]),
        debug_mode=False,
    )
    model = YourMT3(
        audio_cfg=audio_cfg,
        model_cfg=model_cfg,
        shared_cfg=shared_cfg,
        optimizer=None,
        task_manager=manager,
        eval_subtask_key="default",
        write_output_dir=None,
    )
    return model.eval()


def load_weights(model, path: Path, device: str):
    """``model`` with the weights file's tensors, every one, and nothing
    else (strict): a file that loads but is not this model is refused. Then
    moved to ``device``."""
    import torch

    from app.core import tensor_file

    tensors, _metadata = tensor_file.read(path)
    model.load_state_dict(tensors, strict=True)
    model.to(torch.device(device))
    model.eval()
    return model


def read_audio(path: Path):
    """The file as mono float32 at 16 kHz, (1, samples): the channels
    averaged, then torchaudio's resampler, as the authors' transcribe() does
    it (soundfile reads it here, torchaudio there: the same samples)."""
    import numpy as np
    import soundfile as sf
    import torch
    import torchaudio

    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = torch.from_numpy(np.ascontiguousarray(data.mean(axis=1))).unsqueeze(0)
    if rate != SAMPLE_RATE:
        mono = torchaudio.functional.resample(mono, rate, SAMPLE_RATE)
    return mono.numpy()


def segments(audio):
    """(1, n) audio as (segments, 1, SEGMENT_SAMPLES) float32, the last one
    padded with silence: utils/audio.py slice_padded_array with hop equal to
    the length (not vendored; this is its arithmetic)."""
    import numpy as np

    n = audio.shape[1]
    if n == 0:
        raise RuntimeError("the audio has no samples")
    count = -(-n // SEGMENT_SAMPLES)
    padded = np.zeros((1, count * SEGMENT_SAMPLES), dtype=np.float64)
    padded[:, :n] = audio
    return padded.reshape(count, 1, SEGMENT_SAMPLES).astype(np.float32)


def model_output(model, segs, device: str, batch: int, on_pct=None) -> list:
    """The model's tokens for every segment, ``batch`` segments at a time,
    each batch a progress line (``on_pct``, 5 to 90): the loop of the
    authors' inference_file, without its validation loss."""
    import torch

    total = len(segs)
    out = []
    target = torch.device(device)
    if on_pct is not None:
        on_pct(5)
    with torch.inference_mode():
        for start in range(0, total, max(1, batch)):
            x = torch.from_numpy(segs[start : start + batch]).to(target)
            preds = model.inference(x, None).detach().cpu().numpy()
            if len(preds) != len(x):
                raise RuntimeError("the model returned another number of segments")
            out.append(preds)
            if on_pct is not None:
                on_pct(5 + min(85, 85 * min(total, start + batch) // total))
    return out


def decode(model, preds: list, count: int) -> list[tuple[float, float, int, int, int, bool]]:
    """Every note the tokens say, (onset s, offset s, pitch, velocity,
    program, is_drum), sorted: each decoding channel detokenized with its
    segments' start times, ties merged, then the channels mixed, as the
    authors' transcribe() does it."""
    from utils.event2note import merge_zipped_note_events_and_ties_to_notes
    from utils.note2event import mix_notes

    starts = [SEGMENT_SAMPLES * i / SAMPLE_RATE for i in range(count)]
    manager = model.task_manager
    channels = []
    for ch in range(manager.num_decoding_channels):
        tokens = [batch[:, ch, :] for batch in preds]
        zipped, _events, _errors = manager.detokenize_list_batches(
            tokens, starts, return_events=True
        )
        notes, _counts = merge_zipped_note_events_and_ties_to_notes(zipped)
        channels.append(notes)
    return [
        (
            float(n.onset),
            float(n.offset),
            int(n.pitch),
            int(n.velocity),
            int(n.program),
            bool(n.is_drum),
        )
        for n in mix_notes(channels)
    ]


def keeps(stem: str, program: int, is_drum: bool) -> bool:
    """Whether a note of ``program`` (or a drum hit) belongs in ``stem``'s
    file: the stem's class (config.MIDI_YMT3_CLASSES)."""
    if stem == "drums":
        return is_drum
    if is_drum:
        return False
    rule = MIDI_YMT3_CLASSES[stem]
    if "programs" in rule:
        return program in rule["programs"]
    return program not in rule["not_programs"]


def stem_notes(notes: list, stem: str) -> tuple[list, int | None]:
    """The notes of ``stem``'s class as the file writer takes them (start s,
    end s, pitch, amplitude, None), and the General MIDI program most of them
    have (the lowest on a tie), or None for drums or no notes. The model
    has one loudness for every note, so each gets YMT3_NOTE_AMPLITUDE."""
    kept = [n for n in notes if keeps(stem, n[4], n[5])]
    events = [(n[0], max(n[1], n[0]), n[2], YMT3_NOTE_AMPLITUDE, None) for n in kept]
    if stem == "drums" or not kept:
        return events, None
    counts = Counter(n[4] for n in kept if 0 <= n[4] <= 127)
    if not counts:
        return events, None
    top = max(counts.values())
    return events, min(p for p, c in counts.items() if c == top)
