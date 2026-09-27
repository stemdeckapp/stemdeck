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


# The level a frame of pure digital silence reports, and the floor every frame
# is clamped to. Far below anything a vocals stem carries even as bleed.
ENVELOPE_FLOOR_DB = -90


def vocal_envelope(path: Path, hop_sec: float = 0.04) -> tuple[float, list[int]]:
    """RMS level of the WAV at `path` every `hop_sec`, in whole dBFS clamped to
    [ENVELOPE_FLOOR_DB, 0], over all channels. For the lyrics wipe (#699),
    which follows the vocals stem's energy to tell singing from silence.

    Returns (hop actually used, levels). The hop is a whole number of frames,
    so it can differ from `hop_sec` by a fraction of a sample. Whole decibels
    because the wipe needs no finer: a word boundary is a change of ten or
    more, and integers keep the JSON for a ten minute track near 60 KB.

    One streamed pass in blocks of whole hops, so memory stays constant
    whatever the track's length, the same way scan_stem does (#286)."""
    info = sf.info(str(path))
    if info.frames == 0 or info.samplerate <= 0:
        return hop_sec, []
    hop = max(1, round(info.samplerate * hop_sec))
    # A few seconds per block: large enough that the per-block overhead is
    # nothing, small enough to stay a few MB for any track.
    blocksize = hop * 128
    levels: list[int] = []
    for block in sf.blocks(str(path), blocksize=blocksize, dtype="float32", always_2d=True):
        if block.size == 0:
            continue
        whole = (len(block) // hop) * hop
        parts = [block[:whole].reshape(-1, hop * block.shape[1])] if whole else []
        if whole < len(block):
            # The last partial hop of the file: its own frame, averaged over
            # what it has.
            parts.append(block[whole:].reshape(1, -1))
        for frames in parts:
            mean_sq = np.mean(frames.astype(np.float64) ** 2, axis=1)
            db = 10.0 * np.log10(np.maximum(mean_sq, 1e-12))
            clipped = np.clip(np.round(db), ENVELOPE_FLOOR_DB, 0).astype(int)
            levels.extend(clipped.tolist())
    return hop / info.samplerate, levels
