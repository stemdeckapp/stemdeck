"""Weights in the safetensors file format, read and written without pickle.

A model's weights kept as a safetensors file (https://huggingface.co/docs/
safetensors, Apache-2.0) load as tensors and nothing else: no object of the
file is ever constructed, so a file that is not what it claims is refused,
not run. The safetensors package itself is not in uv.lock, and the format is
small enough to read and write here (the desktop-update-gate skill):

    8 bytes   the header's length N, unsigned, little-endian
    N bytes   a JSON object: {name: {"dtype", "shape", "data_offsets":
              [begin, end]}, "__metadata__"?: {str: str}}, padded with
              spaces to a multiple of 8 bytes
    the rest  every tensor's bytes, little-endian, C order, at its offsets
              (counted from the end of the header), with no gap and no
              overlap

write() is deterministic: names in sorted order, the tensors packed in that
order, the header written with sorted keys and no spaces, so the same
tensors always give the same bytes (and so the same SHA-256). read() checks
everything the format promises before it makes a tensor of any of it.

Torch is imported only when a function is called: the server imports this
module without it.
"""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path
from typing import Any

# The dtypes StemDeck writes and reads: (safetensors name, bytes per element).
# Only what the models here hold; any other is refused.
_DTYPES = {"F32": 4, "F16": 2, "I64": 8, "I32": 4, "BOOL": 1}
# A header this long is not a weights file (YourMT3+'s is about 60 KB).
MAX_HEADER_BYTES = 16 * 2**20


class TensorFileError(ValueError):
    """The file is not a safetensors file this module accepts."""


def _little_endian() -> None:
    # Every machine StemDeck ships for (x86-64, arm64) is little-endian; a
    # big-endian one would need every tensor byte-swapped, so it is refused.
    if sys.byteorder != "little":
        raise TensorFileError("a big-endian machine is not supported")


def _torch_dtypes():
    import torch

    return {
        "F32": torch.float32,
        "F16": torch.float16,
        "I64": torch.int64,
        "I32": torch.int32,
        "BOOL": torch.bool,
    }


def _name_of(dtype) -> str:
    for name, value in _torch_dtypes().items():
        if value == dtype:
            return name
    raise TensorFileError(f"dtype {dtype} is not supported")


def header_bytes(tensors: dict[str, Any], metadata: dict[str, str] | None = None) -> bytes:
    """The length prefix and padded JSON header for ``tensors`` (name to a
    torch tensor), in write()'s order."""
    header: dict[str, Any] = {}
    offset = 0
    for name in sorted(tensors):
        t = tensors[name]
        size = t.numel() * t.element_size()
        header[name] = {
            "dtype": _name_of(t.dtype),
            "shape": list(t.shape),
            "data_offsets": [offset, offset + size],
        }
        offset += size
    if metadata:
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()):
            raise TensorFileError("metadata keys and values must be strings")
        header["__metadata__"] = dict(sorted(metadata.items()))
    raw = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    raw += b" " * (-len(raw) % 8)
    return struct.pack("<Q", len(raw)) + raw


def write(path: Path, tensors: dict[str, Any], metadata: dict[str, str] | None = None) -> None:
    """Write ``tensors`` to ``path``, flushed and synced before this returns
    (the caller renames it into place). Raises OSError or TensorFileError."""
    _little_endian()
    if not tensors:
        raise TensorFileError("no tensors to write")
    with open(path, "wb") as handle:
        handle.write(header_bytes(tensors, metadata))
        for name in sorted(tensors):
            # The machine's own order is little-endian (_little_endian), so
            # the bytes in memory are the format's, in C order.
            t = tensors[name].detach().cpu().contiguous()
            handle.write(t.numpy().tobytes(order="C"))
        handle.flush()
        os.fsync(handle.fileno())


def read_header(data: bytes | memoryview) -> tuple[dict[str, Any], dict[str, str], int]:
    """(tensor entries, metadata, where the tensor bytes start) of a file's
    bytes, every promise of the format checked. Raises TensorFileError."""
    if len(data) < 8:
        raise TensorFileError("too short")
    (length,) = struct.unpack("<Q", bytes(data[:8]))
    if length > MAX_HEADER_BYTES or 8 + length > len(data):
        raise TensorFileError("header length out of range")
    try:
        header = json.loads(bytes(data[8 : 8 + length]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TensorFileError("header is not JSON") from exc
    if not isinstance(header, dict):
        raise TensorFileError("header is not an object")
    metadata = header.pop("__metadata__", {}) or {}
    if not isinstance(metadata, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
    ):
        raise TensorFileError("bad metadata")
    start = 8 + length
    body = len(data) - start
    spans = []
    for name, entry in header.items():
        if not isinstance(entry, dict):
            raise TensorFileError(f"{name}: not an entry")
        dtype, shape, offsets = entry.get("dtype"), entry.get("shape"), entry.get("data_offsets")
        if dtype not in _DTYPES:
            raise TensorFileError(f"{name}: dtype {dtype!r} is not supported")
        if not isinstance(shape, list) or not all(
            isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in shape
        ):
            raise TensorFileError(f"{name}: bad shape")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) for n in offsets)
        ):
            raise TensorFileError(f"{name}: bad offsets")
        begin, end = offsets
        count = 1
        for n in shape:
            count *= n
        if not 0 <= begin <= end <= body or end - begin != count * _DTYPES[dtype]:
            raise TensorFileError(f"{name}: offsets do not fit its shape")
        spans.append((begin, end))
    spans.sort()
    at = 0
    for begin, end in spans:
        if begin != at:
            raise TensorFileError("tensors overlap or leave a gap")
        at = end
    if at != body:
        raise TensorFileError("bytes after the last tensor")
    return header, metadata, start


def read(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """({name: tensor}, metadata) of the file at ``path``: each tensor a
    copy in its own memory, on the CPU. Raises OSError or TensorFileError."""
    import torch

    _little_endian()
    data = Path(path).read_bytes()
    header, metadata, start = read_header(data)
    dtypes = _torch_dtypes()
    out: dict[str, Any] = {}
    for name, entry in header.items():
        begin, end = entry["data_offsets"]
        dtype = dtypes[entry["dtype"]]
        if end == begin:
            out[name] = torch.empty(entry["shape"], dtype=dtype)
            continue
        chunk = bytearray(data[start + begin : start + end])
        out[name] = torch.frombuffer(chunk, dtype=dtype).reshape(entry["shape"])
    return out, metadata
