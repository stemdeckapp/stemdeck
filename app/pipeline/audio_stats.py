from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import soundfile as sf


def scan_stem(path: Path, buckets: int = 1500) -> tuple[list[list[float]], float]:
    """One streamed pass over the WAV at `path`: per-bucket [min, max] over
    channel 0 (for the waveform display) and RMS over channel 0 (for stem
    presence) -- both derived from the same blocks, so a stem is only
    decoded once instead of twice.

    Constant memory via sf.blocks() -- a block is
    ~frames/buckets * channels * 4 bytes, a few MB even for a 20-minute
    stereo stem -- instead of sf.read()'s full-file load (#286, ~420 MB for
    the same file)."""
    info = sf.info(str(path))
    frames = info.frames
    if frames == 0:
        return [], 0.0

    # Floor division, matching the old sf.read()-then-chunk implementation's
    # `n // buckets` exactly: sequential fixed-size blocks with the leftover
    # remainder folded into one final partial block, so peaks are bit-for-bit
    # identical to before, just computed one block at a time instead of after
    # loading the whole file.
    blocksize = max(1, frames // buckets)
    result: list[list[float]] = []
    sumsq = 0.0
    n = 0
    for block in sf.blocks(str(path), blocksize=blocksize, dtype="float32", always_2d=True):
        ch = block[:, 0]
        if ch.size == 0:
            continue
        result.append([float(np.min(ch)), float(np.max(ch))])
        sumsq += float(np.sum(ch.astype(np.float64) ** 2))
        n += ch.size

    rms = math.sqrt(sumsq / n) if n else 0.0
    return result[:buckets], rms
