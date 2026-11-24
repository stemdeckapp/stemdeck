"""What a user model's two files must be before anything runs them (My models).

Pure checks, run in the server before a model is kept and again in the test
run's subprocess (app/pipeline/user_model_check_worker.py) on the same bytes.
Nothing here imports torch at module level, opens a network connection or
unpickles anything; PyYAML is imported when a config is read, so a missing
package refuses the model ("unavailable") instead of breaking the server.

The model file. Its format is read from its first bytes, never from its name:
- a zip (b"PK\\x03\\x04") is a torch checkpoint. Only its list of names is read
  here (no pickle is opened): no absolute name or "..", at most MAX_ZIP_ENTRIES
  names, at most MAX_ZIP_TOTAL uncompressed bytes, and a "<folder>/data.pkl".
  The test run loads it with torch.load(weights_only=True) and nothing else.
- safetensors: an 8-byte little-endian header length, a JSON header and the
  tensors' bytes. read_safetensors_header() checks every tensor's dtype
  (F32, F16, BF16), shape and byte span (contiguous, inside the file, each
  span exactly its shape's size); load_safetensors() reads them into torch.
  StemDeck reads the format itself: the safetensors package is not one of its
  dependencies, and adding one changes the desktop update's lock.
- anything else (a legacy pickle, ONNX, GGUF, a web page) is refused.

The config. Strict UTF-8 YAML, at most MAX_USER_MODEL_CONFIG_BYTES. Its events
are scanned first: no alias, no anchor, at most MAX_YAML_EVENTS events, at
most MAX_YAML_DEPTH levels deep, and no tag outside the plain YAML ones and
!!python/tuple. It is then loaded with _TupleSafeLoader, yaml.SafeLoader with
one constructor more: !!python/tuple, read as a plain list. Every other tag
fails to construct. clean_config() then walks the result against a whitelist
of the keys audio-separator's MelBandRoformer and BSRoformer take, each with
a range; an unknown key in "model" refuses the model, so a typo never builds
another network. A closed-form parameter count (exact for audio-separator
0.44.5's classes; tests/test_user_model_format.py checks it against a real
build) must be at most MAX_USER_MODEL_PARAMS before any model is built.

Every refusal is Refused(code), a code from REASONS. No message carries a
path or the file's own text.
"""

from __future__ import annotations

import json
import math
import re
import struct
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import MAX_USER_MODEL_CONFIG_BYTES, MAX_USER_MODEL_PARAMS

# The closed set of reasons a model is refused (each has an i18n sentence,
# settings.myModels.reason.<code>). gates_missing: the checkpoint matches the
# class except for the attention gates (to_gates), which audio-separator's
# RoFormer classes always have and models trained without them lack.
REASONS = frozenset(
    {
        "too_large",
        "empty",
        "bad_format",
        "bad_config",
        "config_missing",
        "unsupported_arch",
        "unsupported_audio",
        "stem_not_in_model",
        "too_big_model",
        "key_mismatch",
        "gates_missing",
        "non_finite",
        "load_failed",
        "forward_failed",
        "timeout",
        "torch_missing",
        "torch_too_old",
        "unavailable",
        "duplicate",
        "limit",
        "disk_full",
        "network",
        "not_found",
        "gated",
        "ambiguous",
        "bad_download",
        "bad_link",
        "interrupted",
        "changed",
        "busy",
    }
)


class Refused(Exception):
    """A model refused for ``code`` (one of REASONS)."""

    def __init__(self, code: str) -> None:
        if code not in REASONS:
            code = "bad_format"
        super().__init__(code)
        self.code = code


# ── the model file ──

ZIP_MAGIC = b"PK\x03\x04"
MAX_ZIP_ENTRIES = 20_000
MAX_ZIP_TOTAL = 3 * 1024**3
MAX_SAFETENSORS_HEADER = 8 * 1024 * 1024
MAX_TENSORS = 20_000
MAX_TENSOR_DIMS = 8
_TENSOR_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,200}\Z")
# dtype -> bytes per element. Little-endian, as the format defines.
DTYPE_SIZES: dict[str, int] = {"F32": 4, "F16": 2, "BF16": 2}


@dataclass(frozen=True)
class TensorInfo:
    dtype: str
    shape: tuple[int, ...]
    start: int  # from the start of the data area
    end: int


def sniff_format(path: Path) -> str:
    """ "torch" or "safetensors", from the file's bytes. Raises Refused
    ("empty", "bad_format")."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        raise Refused("load_failed") from exc
    if size == 0:
        raise Refused("empty")
    if head[:4] == ZIP_MAGIC:
        check_zip(path)
        return "torch"
    read_safetensors_header(path)
    return "safetensors"


def check_zip(path: Path) -> list[str]:
    """The names in a torch checkpoint's zip, read without opening any of
    them. Raises Refused("bad_format") for a zip that is not a plain torch
    checkpoint, or too big unpacked."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise Refused("bad_format") from exc
    if not infos or len(infos) > MAX_ZIP_ENTRIES:
        raise Refused("bad_format")
    total = 0
    names: list[str] = []
    for info in infos:
        name = info.filename
        parts = name.replace("\\", "/").split("/")
        if name.startswith(("/", "\\")) or ".." in parts or ":" in name or "\x00" in name:
            raise Refused("bad_format")
        total += info.file_size
        if total > MAX_ZIP_TOTAL:
            raise Refused("bad_format")
        names.append(name)
    if not any(re.match(r"^[^/]+/data\.pkl\Z", n) for n in names):
        raise Refused("bad_format")
    return names


def read_safetensors_header(path: Path) -> tuple[dict[str, TensorInfo], int]:
    """The tensors a safetensors file declares, and where its data starts.
    Every tensor's span lies inside the file, the spans follow one another
    with no gap or overlap and end at the end of the file, and each is
    exactly its shape's size. Raises Refused("bad_format")."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            raw_len = fh.read(8)
            if len(raw_len) != 8:
                raise Refused("bad_format")
            (n,) = struct.unpack("<Q", raw_len)
            if n < 2 or n > MAX_SAFETENSORS_HEADER or 8 + n > size:
                raise Refused("bad_format")
            raw = fh.read(n)
    except OSError as exc:
        raise Refused("load_failed") from exc
    if len(raw) != n:
        raise Refused("bad_format")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Refused("bad_format") from exc
    if not isinstance(header, dict):
        raise Refused("bad_format")
    meta = header.pop("__metadata__", None)
    if meta is not None and not (
        isinstance(meta, dict) and all(isinstance(v, str) for v in meta.values())
    ):
        raise Refused("bad_format")
    if not header or len(header) > MAX_TENSORS:
        raise Refused("bad_format")
    tensors: dict[str, TensorInfo] = {}
    for name, entry in header.items():
        if not _TENSOR_NAME_RE.match(name) or not isinstance(entry, dict):
            raise Refused("bad_format")
        dtype = entry.get("dtype")
        shape = entry.get("shape")
        offsets = entry.get("data_offsets")
        if dtype not in DTYPE_SIZES or not isinstance(shape, list) or not isinstance(offsets, list):
            raise Refused("bad_format")
        if len(shape) > MAX_TENSOR_DIMS or len(offsets) != 2:
            raise Refused("bad_format")
        if not all(_plain_int(d) and d >= 0 for d in shape):
            raise Refused("bad_format")
        if not all(_plain_int(o) and o >= 0 for o in offsets):
            raise Refused("bad_format")
        start, end = offsets
        if end - start != math.prod(shape) * DTYPE_SIZES[dtype]:
            raise Refused("bad_format")
        tensors[name] = TensorInfo(dtype, tuple(shape), start, end)
    data_size = size - 8 - n
    cursor = 0
    for info in sorted(tensors.values(), key=lambda t: (t.start, t.end)):
        if info.start != cursor:
            raise Refused("bad_format")
        cursor = info.end
    if cursor != data_size:
        raise Refused("bad_format")
    return tensors, 8 + n


def load_safetensors(path: Path) -> dict[str, Any]:
    """Every tensor of a safetensors file as a float32 torch tensor on the
    CPU, after read_safetensors_header() accepted it. Each tensor's bytes are
    read on their own (no mapping of the whole file), so the memory it takes
    is the tensors' float32 size."""
    import torch

    tensors, data_start = read_safetensors_header(path)
    dtypes = {"F32": torch.float32, "F16": torch.float16, "BF16": torch.bfloat16}
    out: dict[str, Any] = {}
    with open(path, "rb") as fh:
        for name, info in tensors.items():
            span = info.end - info.start
            if span == 0:
                out[name] = torch.zeros(info.shape, dtype=torch.float32)
                continue
            fh.seek(data_start + info.start)
            buf = bytearray(fh.read(span))
            if len(buf) != span:
                raise Refused("bad_format")
            t = torch.frombuffer(buf, dtype=dtypes[info.dtype]).reshape(info.shape)
            out[name] = t.to(torch.float32) if info.dtype != "F32" else t
    return out


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ── the config file ──

MAX_YAML_EVENTS = 10_000
MAX_YAML_DEPTH = 12
_YAML_TAGS = frozenset(
    "tag:yaml.org,2002:" + t
    for t in ("str", "int", "float", "bool", "null", "seq", "map", "python/tuple")
)


def _loader():
    """yaml.SafeLoader with !!python/tuple read as a list, and nothing else
    added. Built on first use: PyYAML is imported only here."""
    global _TUPLE_LOADER
    if _TUPLE_LOADER is None:
        import yaml

        class _TupleSafeLoader(yaml.SafeLoader):
            pass

        def tuple_as_list(loader, node):
            return loader.construct_sequence(node, deep=True)

        _TupleSafeLoader.add_constructor("tag:yaml.org,2002:python/tuple", tuple_as_list)
        _TUPLE_LOADER = _TupleSafeLoader
    return _TUPLE_LOADER


_TUPLE_LOADER: Any = None


def parse_config(data: bytes) -> dict:
    """The config's YAML as a dict. Raises Refused ("bad_config",
    "too_large", "empty", "unavailable" when PyYAML is missing)."""
    if len(data) > MAX_USER_MODEL_CONFIG_BYTES:
        raise Refused("too_large")
    if not data.strip():
        raise Refused("empty")
    if b"\x00" in data:
        raise Refused("bad_config")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise Refused("bad_config") from exc
    try:
        import yaml
    except ImportError as exc:
        raise Refused("unavailable") from exc
    loader = _loader()
    try:
        _scan_events(yaml, text)
        # What yaml.load does, with the loader built above: SafeLoader plus
        # the one inert tag. Never yaml.Loader, never FullLoader.
        reader = loader(text)
        try:
            doc = reader.get_single_data()
        finally:
            reader.dispose()
    except Refused:
        raise
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise Refused("bad_config") from exc
    if not isinstance(doc, dict):
        raise Refused("bad_config")
    return doc


def _scan_events(yaml: Any, text: str) -> None:
    """Refuse before anything is built: aliases and anchors (the alias bomb),
    too many events, too deep, and any tag outside _YAML_TAGS."""
    depth = 0
    for count, event in enumerate(yaml.parse(text, Loader=yaml.SafeLoader), start=1):
        if count > MAX_YAML_EVENTS:
            raise Refused("bad_config")
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise Refused("bad_config")
        tag = getattr(event, "tag", None)
        if tag is not None and tag not in _YAML_TAGS:
            raise Refused("bad_config")
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            if depth > MAX_YAML_DEPTH:
                raise Refused("bad_config")
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1


# ── the whitelist ──

SAMPLE_RATE = 44100
ARCHES = ("mel_band_roformer", "bs_roformer")


def _int_in(low: int, high: int):
    def check(value: object) -> int:
        if not _plain_int(value) or not low <= value <= high:  # type: ignore[operator]
            raise Refused("bad_config")
        return value  # type: ignore[return-value]

    return check


def _even_in(low: int, high: int):
    base = _int_in(low, high)

    def check(value: object) -> int:
        v = base(value)
        if v % 2:
            raise Refused("bad_config")
        return v

    return check


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise Refused("bad_config")
    return value


def _false(value: object) -> bool:
    if value is not False:
        raise Refused("bad_config")
    return False


def _true_stereo(value: object) -> bool:
    if value is not True:
        raise Refused("unsupported_audio")
    return True


def _power_of_two(value: object) -> int:
    v = _int_in(256, 8192)(value)
    if v & (v - 1):
        raise Refused("bad_config")
    return v


def _rate(value: object) -> int:
    if not _plain_int(value) or value != SAMPLE_RATE:
        raise Refused("unsupported_audio")
    return SAMPLE_RATE


def _freqs_per_bands(value: object) -> list[int]:
    if not isinstance(value, list) or not 2 <= len(value) <= 256:
        raise Refused("bad_config")
    check = _int_in(1, 2048)
    return [check(v) for v in value]


def _inert_number(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Refused("bad_config")
    if not math.isfinite(value) or not 0 <= value <= 1000:
        raise Refused("bad_config")


def _inert_dropout(value: object) -> None:
    _inert_number(value)
    if value > 0.5:  # type: ignore[operator]
        raise Refused("bad_config")


def _inert_bool(value: object) -> None:
    _bool(value)


def _inert_int_list(value: object) -> None:
    if not isinstance(value, list) or len(value) > 16:
        raise Refused("bad_config")
    for v in value:
        _int_in(1, 1 << 16)(v)


# Keys passed on to the class's constructor, with their check.
_COMMON: dict[str, Any] = {
    "dim": _int_in(16, 1024),
    "depth": _int_in(1, 24),
    "stereo": _true_stereo,
    "num_stems": _int_in(1, 6),
    "time_transformer_depth": _int_in(1, 8),
    "freq_transformer_depth": _int_in(1, 8),
    "dim_head": _even_in(8, 128),
    "heads": _int_in(1, 32),
    "flash_attn": _bool,
    "dim_freqs_in": _int_in(1, 4097),
    "stft_n_fft": _power_of_two,
    "stft_hop_length": _int_in(64, 2048),
    "stft_win_length": _int_in(64, 8192),
    "stft_normalized": _bool,
    "mask_estimator_depth": _int_in(1, 4),
    "mlp_expansion_factor": _int_in(1, 8),
    "sage_attention": _false,
    "zero_dc": _bool,
    "use_torch_checkpoint": _false,
    "skip_connection": _bool,
}
_PASSED: dict[str, dict[str, Any]] = {
    "mel_band_roformer": {**_COMMON, "num_bands": _int_in(8, 256), "sample_rate": _rate},
    "bs_roformer": {
        **_COMMON,
        "freqs_per_bands": _freqs_per_bands,
        # Only 0: audio-separator 0.44.5's linear attention does not build
        # (its Attend takes no "scale").
        "linear_transformer_depth": _int_in(0, 0),
    },
}
# Keys that only matter in training (dropout is off in eval mode, the
# multi-resolution STFT is the training loss): checked, then dropped.
_INERT: dict[str, Any] = {
    "attn_dropout": _inert_dropout,
    "ff_dropout": _inert_dropout,
    "multi_stft_resolution_loss_weight": _inert_number,
    "multi_stft_resolutions_window_sizes": _inert_int_list,
    "multi_stft_hop_size": _inert_number,
    "multi_stft_normalized": _inert_bool,
    "match_input_audio_length": _inert_bool,
}
# BSRoformer takes no sample rate; the config may still say 44100.
_INERT_BS = {"sample_rate": _rate}

# The constructors' own defaults in audio-separator 0.44.5, for the keys the
# parameter count needs (tests/test_user_model_format.py reads them back from
# the classes' signatures).
DEFAULTS: dict[str, dict[str, Any]] = {
    "mel_band_roformer": {
        "num_stems": 1,
        "time_transformer_depth": 2,
        "freq_transformer_depth": 2,
        "num_bands": 60,
        "dim_head": 64,
        "heads": 8,
        "mlp_expansion_factor": 4,
        "stft_n_fft": 2048,
        "stft_hop_length": 512,
        "stft_win_length": 2048,
        "mask_estimator_depth": 1,
        "sample_rate": 44100,
    },
    "bs_roformer": {
        "num_stems": 1,
        "time_transformer_depth": 2,
        "freq_transformer_depth": 2,
        "linear_transformer_depth": 0,
        "dim_head": 64,
        "heads": 8,
        "mlp_expansion_factor": 4,
        "stft_n_fft": 2048,
        "stft_hop_length": 512,
        "stft_win_length": 2048,
        "mask_estimator_depth": 2,
    },
}


def _arch_of(model: dict) -> str:
    mel = "num_bands" in model
    bs = "freqs_per_bands" in model
    if mel == bs:
        raise Refused("unsupported_arch")
    return "mel_band_roformer" if mel else "bs_roformer"


def clean_model(model: object) -> tuple[str, dict]:
    """(arch, constructor kwargs) from a config's "model" block. Only
    whitelisted keys survive; an unknown key refuses the block. The kwargs
    always carry "stereo" and "num_stems", and lists stay lists (JSON);
    constructor_kwargs() makes the tuples the classes want."""
    if not isinstance(model, dict) or not model:
        raise Refused("bad_config")
    if not all(isinstance(k, str) for k in model):
        raise Refused("bad_config")
    arch = _arch_of(model)
    passed = _PASSED[arch]
    inert = {**_INERT, **(_INERT_BS if arch == "bs_roformer" else {})}
    kwargs: dict[str, Any] = {}
    for key, value in model.items():
        if key in passed:
            kwargs[key] = passed[key](value)
        elif key in inert:
            inert[key](value)
        else:
            raise Refused("bad_config")
    if "dim" not in kwargs or "depth" not in kwargs:
        raise Refused("bad_config")
    if kwargs.get("stereo") is not True:
        raise Refused("unsupported_audio")
    kwargs.setdefault("num_stems", 1)
    eff = {**DEFAULTS[arch], **kwargs}
    if eff["stft_win_length"] > eff["stft_n_fft"]:
        raise Refused("bad_config")
    freqs = eff["stft_n_fft"] // 2 + 1
    if arch == "bs_roformer" and sum(kwargs["freqs_per_bands"]) != freqs:
        raise Refused("bad_config")
    if arch == "mel_band_roformer" and min(mel_band_sizes(eff)) == 0:
        raise Refused("bad_config")
    return arch, kwargs


def constructor_kwargs(arch: str, kwargs: dict) -> dict:
    """``kwargs`` as the class's constructor takes them: the band list as a
    tuple (its signature is checked by beartype at build time)."""
    out = dict(kwargs)
    if arch == "bs_roformer" and "freqs_per_bands" in out:
        out["freqs_per_bands"] = tuple(out["freqs_per_bands"])
    return out


def build_model(cls: type, arch: str, kwargs: dict) -> object:
    """The model these kwargs describe, with fresh weights.

    audio-separator's MelBandRoformer takes ``mlp_expansion_factor`` but builds
    its mask estimators with the default of 4 and drops the argument, which
    the reference implementation does not. A checkpoint trained with another
    factor (for example 1) then fails the strict key and shape match. The mask
    estimators are rebuilt with the factor the config asks for, so the model
    is the one the config describes."""
    model = cls(**constructor_kwargs(arch, kwargs))
    if arch == "mel_band_roformer":
        eff = {**DEFAULTS[arch], **kwargs}
        factor = eff["mlp_expansion_factor"]
        if factor != 4:
            import sys

            estimator = sys.modules[cls.__module__].MaskEstimator
            first = model.mask_estimators[0]
            model.mask_estimators = type(model.mask_estimators)(
                estimator(
                    dim=eff["dim"],
                    dim_inputs=first.dim_inputs,
                    depth=eff["mask_estimator_depth"],
                    mlp_expansion_factor=factor,
                )
                for _ in range(eff["num_stems"])
            )
    return model


# ── the audio and output blocks ──

AUDIO_CHUNK_RANGE = (44_100, 1_500_000)
DEFAULT_OVERLAP = 2
DEFAULT_BATCH = 1
_INSTRUMENT_RE = re.compile(r"^[A-Za-z0-9_ -]{1,32}\Z")


def clean_audio(audio: object, inference: object) -> dict:
    """{"chunk", "overlap", "batch"} from the "audio" and "inference" blocks.
    The model's own chunk size is required; overlap and batch default."""
    if not isinstance(audio, dict):
        raise Refused("bad_config")
    if "sample_rate" in audio:
        _rate(audio["sample_rate"])
    if "num_channels" in audio and audio["num_channels"] != 2:
        raise Refused("unsupported_audio")
    if "chunk_size" not in audio:
        raise Refused("bad_config")
    chunk = _int_in(*AUDIO_CHUNK_RANGE)(audio["chunk_size"])
    overlap, batch = DEFAULT_OVERLAP, DEFAULT_BATCH
    if inference is not None:
        if not isinstance(inference, dict):
            raise Refused("bad_config")
        if inference.get("num_overlap") is not None:
            overlap = _int_in(1, 8)(inference["num_overlap"])
        if inference.get("batch_size") is not None:
            batch = _int_in(1, 4)(inference["batch_size"])
    return {"chunk": chunk, "overlap": overlap, "batch": batch}


def map_outputs(num_stems: int, training: object, stems: list[str]) -> dict[str, int]:
    """Declared stem -> the model's output index, from training.instruments
    (output i is instruments[i]) or training.target_instrument (a one-output
    model of that instrument). With neither, a one-output model gives its
    output to a single declared stem: the declaration is then the user's.
    Raises Refused("stem_not_in_model") when a declared stem has no output."""
    instruments: list[str] | None = None
    target: str | None = None
    if training is not None:
        if not isinstance(training, dict):
            raise Refused("bad_config")
        raw = training.get("instruments")
        if raw is not None:
            if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
                raise Refused("bad_config")
            if not all(isinstance(n, str) and _INSTRUMENT_RE.match(n) for n in raw):
                raise Refused("bad_config")
            instruments = [n.strip().lower() for n in raw]
        raw_target = training.get("target_instrument")
        if raw_target is not None:
            if not isinstance(raw_target, str) or not _INSTRUMENT_RE.match(raw_target):
                raise Refused("bad_config")
            target = raw_target.strip().lower()
    if target is not None:
        if num_stems != 1:
            raise Refused("bad_config")
        names = [target]
    elif instruments is not None:
        if len(instruments) != num_stems:
            raise Refused("bad_config")
        names = instruments
    else:
        if num_stems != 1 or len(stems) != 1:
            raise Refused("stem_not_in_model")
        return {stems[0]: 0}
    outputs: dict[str, int] = {}
    for stem in stems:
        if stem not in names:
            raise Refused("stem_not_in_model")
        outputs[stem] = names.index(stem)
    return outputs


@dataclass(frozen=True)
class Cleaned:
    arch: str
    kwargs: dict
    audio: dict
    outputs: dict
    params: int


def clean_config(doc: object, stems: list[str]) -> Cleaned:
    """Everything a model's config decides, checked. ``stems`` are the ones
    the user declared, each of which must be one of the model's outputs."""
    if not isinstance(doc, dict):
        raise Refused("bad_config")
    if not stems or len(set(stems)) != len(stems):
        raise Refused("stem_not_in_model")
    arch, kwargs = clean_model(doc.get("model"))
    audio = clean_audio(doc.get("audio"), doc.get("inference"))
    outputs = map_outputs(kwargs["num_stems"], doc.get("training"), list(stems))
    params = estimate_params(arch, kwargs)
    if params > MAX_USER_MODEL_PARAMS:
        raise Refused("too_big_model")
    return Cleaned(arch, kwargs, audio, outputs, params)


def check_stored(arch: object, kwargs: object, audio: object, outputs: object, stems) -> None:
    """Raise Refused unless a stored record's values are exactly what
    clean_config() makes: the registry is read back from disk before each
    use, and a value edited there must not reach a constructor."""
    if arch not in ARCHES or not isinstance(kwargs, dict):
        raise Refused("bad_config")
    arch2, kwargs2 = clean_model(kwargs)
    if arch2 != arch or kwargs2 != kwargs:
        raise Refused("bad_config")
    if not isinstance(audio, dict) or set(audio) != {"chunk", "overlap", "batch"}:
        raise Refused("bad_config")
    _int_in(*AUDIO_CHUNK_RANGE)(audio["chunk"])
    _int_in(1, 8)(audio["overlap"])
    _int_in(1, 4)(audio["batch"])
    if not isinstance(outputs, dict) or set(outputs) != set(stems):
        raise Refused("bad_config")
    for index in outputs.values():
        _int_in(0, kwargs["num_stems"] - 1)(index)
    if estimate_params(arch, kwargs) > MAX_USER_MODEL_PARAMS:
        raise Refused("too_big_model")


# ── the parameter count ──


def _hz_to_mel(f):
    import numpy as np

    f_sp = 200.0 / 3
    mels = f / f_sp
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(
        f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep, mels
    )


def _mel_to_hz(m):
    import numpy as np

    f_sp = 200.0 / 3
    freqs = f_sp * m
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), freqs)


def mel_band_sizes(eff: dict) -> list[int]:
    """How many STFT bins each mel band of MelBandRoformer covers: the
    non-zero pattern of librosa.filters.mel (Slaney scale and norm, float32)
    with the class's two corner fixes, done in numpy without librosa. A band
    of size 0, or a bin no band covers, is a model the class refuses to
    build; the first reads as a 0 here, the second as a 0 appended."""
    import numpy as np

    sr = eff["sample_rate"]
    n_fft = eff["stft_n_fft"]
    n_mels = eff["num_bands"]
    fftfreqs = np.fft.rfftfreq(n=n_fft, d=1.0 / sr)
    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(sr / 2.0), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fftfreqs)
    weights = np.zeros((n_mels, 1 + n_fft // 2), dtype=np.float32)
    for i in range(n_mels):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    enorm = 2.0 / (mel_f[2 : n_mels + 2] - mel_f[:n_mels])
    weights *= enorm[:, np.newaxis]
    weights[0][0] = 1.0
    weights[-1, -1] = 1.0
    covered = weights > 0
    sizes = [int(v) for v in covered.sum(axis=1)]
    if not covered.any(axis=0).all():
        sizes.append(0)
    return sizes


def _linear(a: int, b: int, bias: bool = True) -> int:
    return a * b + (b if bias else 0)


def _transformer(dim: int, depth: int, heads: int, dim_head: int, norm: bool) -> int:
    inner = heads * dim_head
    attn = dim + _linear(dim, inner * 3, bias=False) + _linear(inner, dim, bias=False)
    attn += _linear(dim, heads)  # to_gates
    ff = dim + _linear(dim, dim * 4) + _linear(dim * 4, dim)
    return depth * (attn + ff) + (dim if norm else 0)


def estimate_params(arch: str, kwargs: dict) -> int:
    """The parameter count of the model these kwargs build, closed form."""
    eff = {**DEFAULTS[arch], **kwargs}
    dim, heads, dim_head = eff["dim"], eff["heads"], eff["dim_head"]
    channels = 2
    if arch == "mel_band_roformer":
        bands = mel_band_sizes(eff)
        norm = True
        hidden = dim * eff["mlp_expansion_factor"]  # build_model passes it on
        mlp_depth = eff["mask_estimator_depth"]
    else:
        bands = list(eff["freqs_per_bands"])
        norm = False
        hidden = dim * eff["mlp_expansion_factor"]
        mlp_depth = eff["mask_estimator_depth"] - 1
    dims_in = [2 * f * channels for f in bands]
    total = 0
    per_layer = _transformer(
        dim, eff["time_transformer_depth"], heads, dim_head, norm
    ) + _transformer(dim, eff["freq_transformer_depth"], heads, dim_head, norm)
    total += eff["depth"] * per_layer
    total += 2 * (dim_head // 2)  # the two shared rotary embeddings
    if arch == "bs_roformer":
        total += dim  # final_norm
    total += sum(d + _linear(d, dim) for d in dims_in)  # band split
    for _ in range(eff["num_stems"]):
        for d in dims_in:
            sizes = (dim, *((hidden,) * mlp_depth), 2 * d)
            total += sum(_linear(a, b) for a, b in zip(sizes[:-1], sizes[1:], strict=True))
    return total


# ── display text ──

_TAG_RE = re.compile(r"<[^>]*>")


def display_text(value: object, limit: int) -> str | None:
    """``value`` as safe display text: NFC, markup and control characters
    removed, whitespace collapsed, at most ``limit`` characters. None when
    nothing is left."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value)
    text = _TAG_RE.sub("", text)
    text = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in text)
    text = " ".join(text.split())
    return text[:limit].strip() or None
