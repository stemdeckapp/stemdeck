from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from app.core.config import JOBS_DIR, TIMEOUT_ANALYZE, ffmpeg_executable
from app.core.models import Job
from app.pipeline.download import _set

logger = logging.getLogger("stemdeck.analyze")

# Albrecht-Shanahan key profiles, derived from a corpus of popular music
# (Albrecht & Shanahan, 2013). Critically, the minor profile here weights
# b7 high (3.48) and M7 low (0.81) — the opposite of Temperley/Kostka-Payne,
# which were derived from Bach chorales and bias toward harmonic minor's
# leading tone. Pop/rock uses natural minor: the b7 is the diatonic
# seventh and rings out constantly (e.g. open D in "Come As You Are",
# which is in E minor and uses D as the b7). Values rescaled so that the
# tonic weight is ≈5 to match the prior code's magnitude.
_MAJOR_PROFILE = (
    5.47,
    0.14,
    2.55,
    0.14,
    3.15,
    2.16,
    0.37,
    4.92,
    0.21,
    1.84,
    0.18,
    1.86,
)
_MINOR_PROFILE = (
    5.06,
    0.14,
    2.42,
    2.42,
    0.35,
    1.96,
    0.35,
    4.16,
    2.53,
    0.28,
    2.67,
    0.62,
)
_PITCHES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# When the best-major and best-minor scores are this close, we prefer
# minor. Pop/rock has a strong minor-mode prior; the algorithm often
# walks toward the relative major because of an ostinato bass note
# (e.g. "Come As You Are" hammers the open D string in an E minor song),
# and minor is the better default when the call is genuinely ambiguous.
_MINOR_TIE_BREAK_FRAC = 0.05


def _correlate(profile: tuple[float, ...], chroma: list[float], shift: int) -> float:
    n = len(profile)
    rotated = [chroma[(i + shift) % n] for i in range(n)]
    mean_p = sum(profile) / n
    mean_c = sum(rotated) / n
    num = sum((profile[i] - mean_p) * (rotated[i] - mean_c) for i in range(n))
    denom_p = sum((profile[i] - mean_p) ** 2 for i in range(n)) ** 0.5
    denom_c = sum((rotated[i] - mean_c) ** 2 for i in range(n)) ** 0.5
    if denom_p == 0 or denom_c == 0:
        return 0.0
    return num / (denom_p * denom_c)


def _detect_key(chroma_mean: list[float]) -> tuple[str, str, int]:
    """Find the best-matching key by combining profile correlation with
    root prominence. The Pearson correlation alone is fooled by relative
    keys whose diatonic notes happen to overlap with the song's loud
    pitches but whose own tonic is weak (e.g. picking A minor for an
    E-minor song because E is its 5th and D is its 4th). Weighting by
    the candidate root's chroma value forces the algorithm to also
    confirm 'is this proposed tonic actually loud in the audio?'.
    Logs the chroma vector and top-5 candidates for diagnostics.

    Returns (label, scale_name, confidence_pct).
    - label:        e.g. "G# maj"
    - scale_name:   "Major" or "Natural Minor"
    - confidence_pct: 0-100, derived from the gap between the winning
                    candidate and the runner-up, normalized so a clear
                    win ranks high and a near-tie ranks low."""
    raw: list[tuple[float, float, str, int]] = []  # (weighted, pearson, label, root_idx)
    for shift in range(12):
        root_strength = chroma_mean[shift]
        pearson_maj = _correlate(_MAJOR_PROFILE, chroma_mean, shift)
        pearson_min = _correlate(_MINOR_PROFILE, chroma_mean, shift)
        # Multiplicative root weighting. Pearson can be negative; when
        # it is, a low-chroma root makes things less negative (closer to
        # zero), which is actually the desired ordering.
        raw.append((pearson_maj * root_strength, pearson_maj, f"{_PITCHES[shift]} maj", shift))
        raw.append((pearson_min * root_strength, pearson_min, f"{_PITCHES[shift]} min", shift))
    raw.sort(key=lambda x: x[0], reverse=True)

    # Diagnostic log: chroma profile + top 5 candidates with both raw
    # and weighted scores. Lets us see what the algorithm is "hearing".
    chroma_str = ", ".join(f"{_PITCHES[i]}={chroma_mean[i]:.3f}" for i in range(12))
    top5_str = ", ".join(
        f"{label}={weighted:+.3f}(p{pearson:+.2f}*r{chroma_mean[idx]:.2f})"
        for weighted, pearson, label, idx in raw[:5]
    )
    logger.debug("chroma: %s", chroma_str)
    logger.debug("key candidates (top 5): %s", top5_str)

    # Pick best major and best minor for the tie-break, both by the
    # weighted score.
    best_maj = next(c for c in raw if c[2].endswith("maj"))
    best_min = next(c for c in raw if c[2].endswith("min"))

    gap = abs(best_maj[0] - best_min[0])
    threshold = max(abs(best_maj[0]), abs(best_min[0])) * _MINOR_TIE_BREAK_FRAC
    # Near-tie -> prefer minor (pop/rock prior); clear winner -> use it.
    winner = (best_maj if best_maj[0] > best_min[0] else best_min) if gap > threshold else best_min

    # Confidence: gap between the winner and the runner-up that *isn't*
    # the relative major/minor of the winner (those will always be near-
    # ties with the algorithm's profile-correlation approach, so they
    # tell us nothing about real ambiguity). Normalize so a healthy 0.15
    # gap = 100% confident; tiny gap = 0%.
    runner_up = next(c for c in raw if c[2] != winner[2])
    confidence_score = winner[0] - runner_up[0]
    confidence_pct = max(0, min(100, round(confidence_score / 0.15 * 100)))

    label = winner[2]
    scale_name = "Major" if label.endswith("maj") else "Natural Minor"
    return label, scale_name, confidence_pct


def _measure_loudness(y: object, sr: int) -> tuple[float | None, float | None]:
    """Compute integrated loudness (LUFS, BS.1770) and sample peak (dBFS)
    of the loaded mono signal. Returns (lufs, peak_db); either may be
    None on failure or silence. We use sample peak rather than oversampled
    true peak -- the difference is typically <1 dB and not worth the 4x
    resample cost for a display field."""
    import numpy as np

    if y is None or getattr(y, "size", 0) == 0:
        return None, None

    peak_lin = float(np.abs(y).max())
    peak_db = 20.0 * float(np.log10(peak_lin)) if peak_lin > 1e-9 else None

    lufs: float | None = None
    try:
        import pyloudnorm as pyln

        meter = pyln.Meter(sr)  # BS.1770-4 with default 400ms blocks
        lufs_raw = float(meter.integrated_loudness(y))
        # pyloudnorm returns -inf for silence; surface as None instead so
        # the frontend can hide the field rather than render "-inf LUFS".
        if np.isfinite(lufs_raw):
            lufs = lufs_raw
    except (ImportError, ValueError) as e:
        # ValueError fires if the clip is shorter than the gating window.
        logger.warning("LUFS measurement failed: %s", e)
    return lufs, peak_db


def _load_audio_ffmpeg(
    source: Path, sr: int = 22050, duration: float = 180.0
) -> tuple[object, int] | None:
    """Decode `source` to a mono float32 numpy array at `sr` via ffmpeg.
    Bypasses librosa's deprecated audioread fallback (which fires a
    FutureWarning on .webm/.m4a/.opus inputs because soundfile can't
    read those directly). Returns (samples, sr) or None on failure."""
    import numpy as np

    # Defence in depth: even though `source` is constructed by the server
    # (never user-typed), confirm it's a real file inside JOBS_DIR before
    # handing it to a subprocess. Belt-and-suspenders against a future
    # caller change that would let a path slip in from elsewhere.
    resolved = source.resolve()
    jobs_resolved = JOBS_DIR.resolve()
    if not resolved.is_file():
        logger.warning("analyze source is not a file: %s", source)
        return None
    if not resolved.is_relative_to(jobs_resolved):
        logger.warning(
            "analyze source escapes JOBS_DIR (%s not under %s)",
            resolved,
            jobs_resolved,
        )
        return None

    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(resolved),
        "-ac",
        "1",  # mono
        "-ar",
        str(sr),  # resample
        "-f",
        "f32le",  # raw 32-bit float little-endian
        "-t",
        str(duration),  # cap input duration
        "-",  # write to stdout
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True, timeout=TIMEOUT_ANALYZE)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("ffmpeg decode failed for %s: %s", source, e)
        return None
    y = np.frombuffer(proc.stdout, dtype=np.float32)
    if y.size == 0:
        return None
    return y, sr


def analyze(job: Job, source: Path) -> tuple[int | None, str | None]:
    """Best-effort BPM and key detection. On failure, returns (None, None)
    and leaves job fields untouched -- the chips stay as placeholders."""
    logger.info("analyze: entering for job %s, source=%s", job.id, source)
    _set(job, status="analyzing", progress=0.0, stage="Analyzing audio...")
    try:
        import librosa
    except ImportError:
        logger.warning("librosa not installed -- skipping BPM/key analysis")
        return None, None

    try:
        # Analyse the first 180 s. Decode via ffmpeg directly into numpy
        # to avoid librosa's deprecated audioread fallback for
        # .webm/.m4a/.opus inputs.
        loaded = _load_audio_ffmpeg(source, sr=22050, duration=180.0)
        if loaded is None:
            return None, None
        y, sr = loaded

        # Harmonic / percussive separation. Beat tracking sees a cleaner
        # onset envelope on the percussive component; chroma sees a
        # cleaner pitch profile on the harmonic component (no cymbal
        # smear, no kick fundamentals leaking in).
        y_harmonic, y_percussive = librosa.effects.hpss(y)

        tempo_arr, _ = librosa.beat.beat_track(y=y_percussive, sr=sr)
        try:
            tempo = float(tempo_arr[0])  # type: ignore[index]
        except (TypeError, IndexError):
            tempo = float(tempo_arr)
        bpm = int(round(tempo)) if tempo > 0 else None

        # chroma_cqt is constant-Q based — better pitch resolution than
        # chroma_stft, especially in the bass register where the open
        # strings of a guitar live.
        chroma = librosa.feature.chroma_cqt(y=y_harmonic, sr=sr)
        chroma_mean = chroma.mean(axis=1).tolist()
        if any(chroma_mean):
            key, scale, key_confidence = _detect_key(chroma_mean)
        else:
            key, scale, key_confidence = None, None, None

        # LUFS / peak. Computed on the same 22 kHz mono buffer; this
        # loses a few dB of accuracy vs full-sample-rate stereo, but
        # it's good enough for a UI display and adds ~50 ms to analyze.
        lufs, peak_db = _measure_loudness(y, sr)

        _set(
            job,
            bpm=bpm,
            key=key,
            scale=scale,
            key_confidence=key_confidence,
            lufs=lufs,
            peak_db=peak_db,
            progress=1.0,
            stage="Analysis complete",
        )
        return bpm, key
    except Exception as e:
        logger.exception("analyze failed for job %s", job.id)
        _set(job, stage=f"Analysis skipped ({e})")
        return None, None
