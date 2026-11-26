"""The RoFormer engine: Kim's Mel-Band RoFormer vocal model
(kim_mel_roformer, app/core/stem_models.py), and the Mel-Band and BS-RoFormer
models a person added (Settings, My models; app/core/user_models.py).

Runs inside the persistent separation worker (demucs_worker.py), never in the
server: torch and audio-separator are imported inside the functions only.

The architecture is audio-separator's MelBandRoformer class, from an internal
module of that package (uvr_lib_v5.roformer.mel_band_roformer). stem_models
reads Kim as unavailable when that file is not there, and
tests/test_mel_roformer.py fails when the class no longer builds the
checkpoint's exact state dict. The checkpoint is downloaded on first use from
its author's Hugging Face repository, pinned to a revision and verified by
SHA-256 (stem_models.KIM_CHECKPOINT).

The model sees 8 s of audio at a time. Chunks overlap by half and cross-fade
at their ends, and the song is padded so that every real sample is under a
fully open window of at least one chunk: no seams, no faded song edges, and a
model that returned its input would give the input back.

On an NVIDIA or AMD (ROCm) GPU it runs under fp16 autocast, as it was trained
(use_amp in its training config); MPS and the CPU stay in float32.

A model the person added runs through the same chunked engine with its own
chunk size, overlap and batch (RoformerSpec), and gives one output per stem
the plan assigned it, picked by the output index its config names. Its file
was checked when it was added; here it is checked again before it is read:
its size and modification time on every load, its SHA-256 once per worker
lifetime. A torch checkpoint is read with weights_only, a safetensors file
by user_model_format's own reader, and either loads strictly: exactly the
keys the class builds, no prefix stripped, nothing renamed or adapted. Every
failure is a UserModelError with one fixed code; the worker turns it into
its "user model failed" line, and the job stands HT Demucs 6 stems in.
"""

# engine approach after a community implementation; credit pending

from __future__ import annotations

import importlib.metadata
import logging
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.stem_models import KIM_CHECKPOINT, ModelFile, model_path

logger = logging.getLogger("stemdeck.mel_roformer")

# The model's hyper-parameters: the "model" block of its training config,
# configs/KimberleyJensen/config_vocals_mel_band_roformer_kj.yaml in
# ZFTurbo/Music-Source-Separation-Training (1721 bytes, SHA-256 f63f38eb...,
# read on 2026-11-23; the model's own Hugging Face repo holds no config).
# Every value below is that file's. The shape of every weight they produce is
# also checked against the checkpoint's own by tests/test_mel_roformer.py; the
# STFT hop and window, which leave no trace in the weights, are checked there
# by the length a forward pass returns.
MODEL_KWARGS: dict[str, Any] = {
    "dim": 384,
    "depth": 6,
    "stereo": True,
    "num_stems": 1,
    "time_transformer_depth": 1,
    "freq_transformer_depth": 1,
    "num_bands": 60,
    "dim_head": 64,
    "heads": 8,
    "attn_dropout": 0,
    "ff_dropout": 0,
    "flash_attn": True,
    "dim_freqs_in": 1025,
    "sample_rate": 44100,
    "stft_n_fft": 2048,
    "stft_hop_length": 441,
    "stft_win_length": 2048,
    "stft_normalized": False,
    "mask_estimator_depth": 2,
    "multi_stft_resolution_loss_weight": 1.0,
    "multi_stft_resolutions_window_sizes": (4096, 2048, 1024, 512, 256),
    "multi_stft_hop_size": 147,
    "multi_stft_normalized": False,
}
SAMPLE_RATE = 44100
CHANNELS = 2
# The config's audio.chunk_size: 8 s, and stft_hop_length x 800 frames, so
# the inverse STFT returns exactly a chunk.
CHUNK = 352_800
# inference.num_overlap 2: each chunk starts half a chunk after the last.
STEP = CHUNK // 2
# Each chunk fades in and out over this many samples.
FADE = CHUNK // 10
# Chunks per forward pass. The measured 2017 MB of VRAM is at this size.
BATCH = 2

ProgressFn = Callable[[int], None]

# The architectures a model the person added may have, by the class that
# builds it in audio-separator (uvr_lib_v5.roformer).
ARCHES = ("mel_band_roformer", "bs_roformer")
# The overlap-add needs at least two chunks over every sample: with one, the
# song's first and last samples sit under a fade alone.
MIN_OVERLAP = 2
# weights_only cannot be bypassed from a crafted pickle from this version on
# (CVE-2025-32434); a model the person added is never read by an older one.
MIN_TORCH = (2, 6)


@dataclass(frozen=True)
class RoformerSpec:
    """How the engine runs one RoFormer: its class and constructor
    arguments, the audio it sees at a time, and which output is which stem.

    ``outputs`` maps a stem to its index on the model's stems axis (a model
    with no stems axis has the one index 0). ``overlap`` is the config's
    inference.num_overlap: each chunk starts chunk // overlap after the last."""

    arch: str
    kwargs: Mapping[str, Any]
    chunk: int
    overlap: int
    batch: int
    outputs: Mapping[str, int]
    num_stems: int


KIM_SPEC = RoformerSpec(
    arch="mel_band_roformer",
    kwargs=MODEL_KWARGS,
    chunk=CHUNK,
    overlap=CHUNK // STEP,
    batch=BATCH,
    outputs={"vocals": 0},
    num_stems=1,
)

# The closed set of reasons a model the person added fails in a job; each is a
# reason code the API and the page know (app/core/user_model_format.py).
USER_FAILURE_CODES = frozenset(
    {
        "changed",
        "load_failed",
        "key_mismatch",
        "gates_missing",
        "non_finite",
        "forward_failed",
        "unsupported_audio",
        "torch_too_old",
        "unavailable",
    }
)


class UserModelError(Exception):
    """A model the person added could not be loaded or run. ``code`` is one of
    USER_FAILURE_CODES; the message is for the log only."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in USER_FAILURE_CODES:
            raise ValueError("not a user model failure code")
        super().__init__(message or code)
        self.code = code


class GatesMissing(UserModelError):
    """The file fits the class except for the attention layers' to_gates
    weights: it was trained with ungated attention, and the RoFormer classes
    of this install's audio-separator always build gated attention. Its own
    code ("gates_missing", as the dry run says it) and its own words in the
    log; nothing is adapted."""

    def __init__(self) -> None:
        super().__init__(
            "gates_missing",
            "the model file has no to_gates weights: it was trained without gated"
            " attention, which audio-separator's RoFormer classes always have, so it"
            " cannot be loaded as it is",
        )


@dataclass(frozen=True)
class UserFile:
    """A model file the person added, as the worker's request names it, with
    what it must still be."""

    model_id: str
    path: Path
    format: str  # "safetensors" or "torch"
    sha256: str
    size: int
    mtime_ns: int | None


def _int(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise UserModelError("load_failed", "a value of the model's settings is out of range")
    return value


def user_spec(model_id: str, raw: object) -> tuple[RoformerSpec, UserFile, tuple[str, ...]]:
    """The worker's request entry for a model the person added, as the
    engine's spec, its file and its declared stems. UserModelError
    ("load_failed") for an entry that is not one: the parent built it from a
    record its store checked, so this guards against a torn request only."""
    if not isinstance(raw, Mapping):
        raise UserModelError("load_failed", "no description of the model")
    arch, kwargs, audio = raw.get("arch"), raw.get("kwargs"), raw.get("audio")
    outputs, stems = raw.get("outputs"), raw.get("stems")
    fmt, path, sha = raw.get("format"), raw.get("path"), raw.get("sha256")
    if arch not in ARCHES or not isinstance(kwargs, Mapping) or not isinstance(audio, Mapping):
        raise UserModelError("load_failed", "the model's architecture or settings are missing")
    if not isinstance(outputs, Mapping) or not isinstance(stems, list) or not stems:
        raise UserModelError("load_failed", "the model's stems are missing")
    num_stems = _int(raw.get("num_stems", kwargs.get("num_stems", 1)), 1, 64)
    clean_outputs: dict[str, int] = {}
    for stem, index in outputs.items():
        if not isinstance(stem, str) or stem not in stems:
            raise UserModelError("load_failed", "an output is not a declared stem")
        clean_outputs[stem] = _int(index, 0, num_stems - 1)
    if set(clean_outputs) != set(stems):
        raise UserModelError("load_failed", "a declared stem has no output")
    if fmt not in ("safetensors", "torch") or not isinstance(path, str) or not path:
        raise UserModelError("load_failed", "the model file is not named")
    if not (isinstance(sha, str) and len(sha) == 64):
        raise UserModelError("load_failed", "the model file has no SHA-256")
    mtime_ns = raw.get("mtime_ns")
    spec = RoformerSpec(
        arch=arch,
        kwargs=dict(kwargs),
        chunk=_int(audio.get("chunk"), 1024, 10_000_000),
        overlap=_int(audio.get("overlap", MIN_OVERLAP), 1, 64),
        batch=_int(audio.get("batch", 1), 1, 64),
        outputs=clean_outputs,
        num_stems=num_stems,
    )
    file = UserFile(
        model_id=model_id,
        path=Path(path),
        format=fmt,
        sha256=sha,
        size=_int(raw.get("size"), 1, 1 << 62),
        mtime_ns=mtime_ns if isinstance(mtime_ns, int) and not isinstance(mtime_ns, bool) else None,
    )
    return spec, file, tuple(stems)


def checkpoint_path(model_file: ModelFile = KIM_CHECKPOINT) -> Path:
    return model_path(model_file)


def ensure_checkpoint(
    *,
    read_timeout: int,
    attempts: int,
    model_file: Any = KIM_CHECKPOINT,
    path: Path | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> Path:
    """The checkpoint on disk, downloaded when it is missing or not its full
    size. The download says itself on stderr the way torch.hub does
    ('Downloading: "<url>" to <path>', then its progress), so the worker's
    parent shows it as the model download and holds it to that stall limit.
    A file of the right size is used as it is; load() checks its SHA-256 only
    when it does not read. ``on_bytes`` is told the bytes received so far
    (Settings > Models' Download button, app/pipeline/model_get_worker.py)."""
    from app.core.model_download import fetch_verified_file

    path = path or checkpoint_path(model_file)
    try:
        if path.stat().st_size == model_file.size:
            return path
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    sys.stderr.write(f'Downloading: "{model_file.url}" to {path}\n')
    sys.stderr.flush()
    fetch_verified_file(
        model_file.url,
        str(path),
        model_file.sha256,
        model_file.size,
        read_timeout=read_timeout,
        attempts=attempts,
        on_bytes=on_bytes,
    )
    return path


def build_model() -> Any:
    """The MelBandRoformer with this checkpoint's shape and fresh weights."""
    from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import MelBandRoformer

    return MelBandRoformer(**MODEL_KWARGS)


def build_user_model(spec: RoformerSpec) -> Any:
    """A model the person added with its architecture and fresh weights: the
    MelBandRoformer or BSRoformer class audio-separator has, given exactly
    the constructor arguments the store cleaned, in the form its dry run
    gave them (user_model_format.build_model)."""
    from app.core.user_model_format import build_model

    if spec.arch == "mel_band_roformer":
        from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import (
            MelBandRoformer as cls,
        )
    elif spec.arch == "bs_roformer":
        from audio_separator.separator.uvr_lib_v5.roformer.bs_roformer import BSRoformer as cls
    else:
        raise UserModelError("load_failed", "not a RoFormer architecture")
    return build_model(cls, spec.arch, dict(spec.kwargs))


def read_state(path: Path) -> Any:
    """The checkpoint's state dict. weights_only: the checkpoint is a pickle,
    and reading it must not be able to run code."""
    import torch

    return torch.load(path, map_location="cpu", weights_only=True)


def into_model(state: Any, path: Path) -> Any:
    """A model holding ``state``, on the CPU, in eval mode. strict: a state
    dict that only partly fits would load "successfully" and separate
    nothing. A misfit says so in words: the file is fine, the class is not
    the one it was made for (an audio-separator that changed it)."""
    model = build_model()
    try:
        model.load_state_dict(state, strict=True)
    except RuntimeError as e:
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise RuntimeError(
            f"the vocal model file {path} does not fit the MelBandRoformer this install"
            f" has ({first}); the file was kept, update StemDeck or report this"
        ) from e
    return model.eval()


def load_weights(path: Path) -> Any:
    """The model with the checkpoint's weights, on the CPU, in eval mode."""
    return into_model(read_state(path), path)


def load(
    *,
    read_timeout: int,
    attempts: int,
    model_file: Any = KIM_CHECKPOINT,
    path: Path | None = None,
) -> Any:
    """The model, its checkpoint downloaded first when needed.

    Only reading the file can find it damaged. When it does not read, its
    SHA-256 decides: a file that is not the pinned one (cut short at the
    right size, a damaged disk) is removed and fetched again, once; a file
    that is exactly the pinned one is never touched, and the error says it
    is intact, since fetching 913 MB again would read the same way. Building
    the model and fitting the weights into it are outside that: a failure
    there is never the file's."""
    from app.core.model_download import file_matches

    path = ensure_checkpoint(
        read_timeout=read_timeout, attempts=attempts, model_file=model_file, path=path
    )
    try:
        state = read_state(path)
    except Exception as first:
        if file_matches(path, model_file.sha256, model_file.size):
            raise RuntimeError(
                f"the vocal model file {path} is intact (its SHA-256 matches) but could not"
                f" be read ({type(first).__name__}); the file was kept, update StemDeck or"
                " report this"
            ) from first
        logger.warning("vocal model file %s is damaged (%s); fetching it again", path, first)
        path.unlink(missing_ok=True)
        path = ensure_checkpoint(
            read_timeout=read_timeout, attempts=attempts, model_file=model_file, path=path
        )
        state = read_state(path)
    return into_model(state, path)


# The model files whose SHA-256 this worker has checked, by (id, SHA-256). Ids
# are random and a file is never rewritten in place, so an entry never goes
# stale; the size and modification time are still checked on every load.
_verified: set[tuple[str, str]] = set()
# The weights of gated attention, which a model trained without it lacks (the
# dry run's rule, user_model_check_worker.compare_keys).
_GATE_SUFFIXES = (".to_gates.weight", ".to_gates.bias")


def _torch_new_enough() -> bool:
    try:
        version = importlib.metadata.version("torch")
    except importlib.metadata.PackageNotFoundError:
        return False
    parts: list[int] = []
    for piece in version.split("+")[0].split(".")[:2]:
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts) >= MIN_TORCH


def check_user_file(file: UserFile) -> None:
    """UserModelError("changed") unless the file is still the one recorded:
    its size and modification time now, and its SHA-256 the first time this
    worker reads it."""
    from app.core.model_download import file_matches

    try:
        st = os.stat(file.path)
    except OSError:
        raise UserModelError("changed", "the model file is gone") from None
    if st.st_size != file.size or (file.mtime_ns is not None and st.st_mtime_ns != file.mtime_ns):
        raise UserModelError("changed", "the model file is not the size or age recorded")
    key = (file.model_id, file.sha256)
    if key in _verified:
        return
    if not file_matches(file.path, file.sha256, file.size):
        raise UserModelError("changed", "the model file's SHA-256 is not the one recorded")
    _verified.add(key)


def _read_safetensors(path: Path) -> dict[str, Any]:
    """Every tensor of a safetensors file, its header checked first, by
    StemDeck's own reader (no dependency, user_model_format)."""
    from app.core.user_model_format import load_safetensors

    return load_safetensors(path)


def read_user_state(file: UserFile) -> dict[str, Any]:
    """The state dict of a model file the person added. A torch checkpoint is
    read with weights_only, and one level of wrapping ("state_dict" or
    "model" holding the dict) is taken off; every value must be a tensor.
    UserModelError("load_failed") when it does not read as one."""
    import torch

    try:
        if file.format == "safetensors":
            state = _read_safetensors(file.path)
        else:
            state = torch.load(file.path, map_location="cpu", weights_only=True)
    except UserModelError:
        raise
    except Exception as e:  # noqa: BLE001 -- any unreadable file is the same refusal
        raise UserModelError(
            "load_failed", f"the model file does not read ({type(e).__name__})"
        ) from e
    if isinstance(state, Mapping) and not all(torch.is_tensor(v) for v in state.values()):
        for key in ("state_dict", "model"):
            if isinstance(state.get(key), Mapping):
                state = state[key]
                break
    if not isinstance(state, Mapping) or not state:
        raise UserModelError("load_failed", "the model file holds no state dict")
    if not all(isinstance(k, str) and torch.is_tensor(v) for k, v in state.items()):
        raise UserModelError("load_failed", "the model file holds more than tensors")
    return dict(state)


def into_user_model(model: Any, state: Mapping[str, Any]) -> Any:
    """``model`` holding ``state``, strictly: exactly the keys the class
    builds, each the shape it builds, every value finite. Nothing is renamed,
    stripped or adapted. On the CPU, in eval mode."""
    import torch

    expected = set(model.state_dict())
    got = set(state)
    missing, unexpected = expected - got, got - expected
    if missing or unexpected:
        if not unexpected and all(k.endswith(_GATE_SUFFIXES) for k in missing):
            raise GatesMissing()
        raise UserModelError(
            "key_mismatch",
            f"the model file does not fit its class ({len(missing)} weights missing,"
            f" {len(unexpected)} not expected)",
        )
    for value in state.values():
        if value.is_floating_point() and not bool(torch.isfinite(value).all()):
            raise UserModelError("non_finite", "the model file holds weights that are not numbers")
    try:
        model.load_state_dict(state, strict=True)
    except RuntimeError as e:
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise UserModelError(
            "key_mismatch", f"the model file does not fit its class ({first})"
        ) from e
    return model.eval()


def load_user(spec: RoformerSpec, file: UserFile) -> Any:
    """A model the person added, its file checked, read and fitted. Raises
    UserModelError with the reason's code."""
    if not _torch_new_enough():
        raise UserModelError("torch_too_old", "torch is older than 2.6")
    check_user_file(file)
    try:
        model = build_user_model(spec)
    except UserModelError:
        raise
    except Exception as e:  # noqa: BLE001 -- the class refused the settings
        raise UserModelError(
            "load_failed", f"the model could not be built ({type(e).__name__})"
        ) from e
    return into_user_model(model, read_user_state(file))


def is_rocm(device: str) -> bool:
    """Whether ``device`` is an AMD GPU under a ROCm build of torch."""
    if device != "cuda":
        return False
    import torch

    return bool(getattr(torch.version, "hip", None))


@contextmanager
def contiguous_sdpa(enabled: bool) -> Iterator[None]:
    """Hand scaled_dot_product_attention contiguous tensors while it is on.

    torch 2.8.0+rocm6.4 on RDNA4 (gfx1201) returns wrong attention, on every
    backend, when v's memory layout differs from k's, and this model passes
    exactly that: a contiguous k beside a transposed view of v. Measured
    against the CPU the vocals came out at -0.1 dB, no separation at all;
    made contiguous first they match it. Fixed in torch 2.14.1+rocm7.2. Only
    on ROCm and only around this model's forward pass, so nothing else in the
    worker changes."""
    if not enabled:
        yield
        return
    import torch.nn.functional as F

    real = F.scaled_dot_product_attention

    def sdpa(q, k, v, *args, **kwargs):
        return real(q.contiguous(), k.contiguous(), v.contiguous(), *args, **kwargs)

    F.scaled_dot_product_attention = sdpa
    try:
        yield
    finally:
        F.scaled_dot_product_attention = real


def autocast(device: str) -> Any:
    """fp16 autocast on an NVIDIA or ROCm GPU, as the model was trained;
    nothing anywhere else."""
    if device != "cuda":
        return nullcontext()
    import torch

    return torch.autocast("cuda", dtype=torch.float16)


_length_noted: list[int] = []


def _note_length(returned: int, chunk: int = CHUNK, kind: str = "vocal") -> None:
    if returned != chunk and not _length_noted:
        _length_noted.append(returned)
        logger.warning("the %s model returned %d samples for a chunk of %d", kind, returned, chunk)


def chunk_starts(length: int, chunk: int = CHUNK, step: int = STEP) -> tuple[int, int, list[int]]:
    """(left pad, padded length, chunk starts) for ``length`` samples. The
    left pad is a whole step, so the first real sample sits in the open part
    of the first chunk's window; the right pad is at least a step, and makes
    the chunks tile the padded length exactly."""
    pad = chunk - step
    needed = pad + length + pad
    extra = max(0, needed - chunk)
    padded = chunk + -(-extra // step) * step
    return pad, padded, list(range(0, padded - chunk + 1, step))


def separate(
    model: Any,
    mix: Any,
    device: str,
    spec: RoformerSpec,
    progress: ProgressFn | None = None,
    stems: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The stems of ``mix`` (channels x samples, float, 44.1 kHz) that
    ``spec`` maps to outputs, each the shape of ``mix``, on the CPU, in
    float32. ``stems`` limits them to the ones asked for (the stems a plan
    assigned this model), so no other output is kept. ``progress`` gets
    0..100 as chunks finish."""
    import torch

    wanted = [s for s in (stems if stems is not None else spec.outputs) if s in spec.outputs]
    if not wanted:
        raise ValueError("no stem of this model was asked for")
    chunk = spec.chunk
    step = chunk // max(MIN_OVERLAP, spec.overlap)
    fade = chunk // 10
    kind = "vocal" if spec is KIM_SPEC else "RoFormer"
    n = mix.shape[-1]
    pad, padded, starts = chunk_starts(n, chunk, step)
    x = torch.nn.functional.pad(mix.float(), (pad, padded - pad - n))
    window = torch.ones(chunk)
    window[:fade] = torch.linspace(0.0, 1.0, fade)
    window[-fade:] = torch.linspace(1.0, 0.0, fade)
    outs = {stem: torch.zeros_like(x) for stem in wanted}
    weight = torch.zeros(padded)
    last = -1
    with torch.no_grad(), contiguous_sdpa(is_rocm(device)), autocast(device):
        for i in range(0, len(starts), spec.batch):
            group = starts[i : i + spec.batch]
            batch = torch.stack([x[:, s : s + chunk] for s in group]).to(device)
            y = model(batch)
            # (batch, stems, channels, samples), or (batch, channels,
            # samples) from a model with one output.
            if y.ndim == 4:
                parts = {stem: y[:, spec.outputs[stem]].float().cpu() for stem in wanted}
            elif y.ndim == 3 and all(spec.outputs[stem] == 0 for stem in wanted):
                parts = {stem: y.float().cpu() for stem in wanted}
            else:
                raise ValueError("the model returned audio of an unexpected shape")
            del y
            # The inverse STFT can come back a few samples short of a chunk;
            # the neighbouring chunk covers what is missing. The real model
            # returns exactly a chunk, so anything else is said once: a wrong
            # hop or window would otherwise pass unnoticed.
            returned = next(iter(parts.values())).shape[-1]
            length = min(chunk, returned)
            _note_length(returned, chunk, kind)
            for j, s in enumerate(group):
                for stem, part in parts.items():
                    outs[stem][:, s : s + length] += part[j, :, :length] * window[:length]
                weight[s : s + length] += window[:length]
            if progress is not None:
                pct = min(100, (i + len(group)) * 100 // len(starts))
                if pct != last:
                    progress(pct)
                    last = pct
    scale = weight.clamp(min=1e-8)
    return {stem: (out / scale)[:, pad : pad + n] for stem, out in outs.items()}


def separate_vocals(model: Any, mix: Any, device: str, progress: ProgressFn | None = None) -> Any:
    """The vocals of ``mix`` (channels x samples, float, 44.1 kHz) from Kim's
    model, the same shape, on the CPU, in float32. ``progress`` gets 0..100
    as chunks finish."""
    return separate(model, mix, device, KIM_SPEC, progress)["vocals"]
