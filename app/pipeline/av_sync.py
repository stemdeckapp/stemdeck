"""Where a take's camera file starts on the take's audio timeline, measured.

The picture of a take is recorded by the browser's MediaRecorder together with
the camera's own microphone, so inside the camera file picture and sound share
one clock (the browser aligns them by capture time). The take's sound,
audio.raw.wav, is recorded by another path (the capture graph's worklet), and
the two recordings start at different moments that the browser can only
estimate (measured on a Logitech StreamCam: the estimate was 430 to 530 ms
wrong, and varied by 100 ms between takes).

So the camera file's own sound is compared with audio.raw.wav: where it
matches is where the camera file's t=0 sits on the audio timeline, and that is
also where its picture sits.

    offset_ms = time of camera-file t=0 (its first video frame) on the raw
                audio clock, in ms after raw audio frame 0. Positive: the
                picture starts after the sound. The same sign and clock as
                video.firstFrameAtMs (static/js/video/videoCodec.js).

The comparison is GCC-PHAT on both signals downmixed to mono and resampled to
8 kHz by ffmpeg, band-limited to 100 Hz to 3.5 kHz, in windows of 30 s over the
first 180 s of the camera file, searched within +-2 s of the browser's
estimate. The earliest clear window gives the offset; clear windows that
disagree by more than MAX_WINDOW_DISAGREEMENT_MS give none. Whitening (PHAT) keeps the peak sharp when the camera microphone
hears the instrument through the room while the take is a direct line.

A match is accepted only when it is clear (see MIN_PMR, MIN_PEAK_RATIO): the
camera microphone can hear the room more than the instrument, or nothing at
all, and a wrong offset is worse than the estimate. Then the caller keeps the
estimate and records why (`reason`).

Pure numpy below `measure_offset`; ffmpeg only in `read_mono` and
`measure_take_offset`, which block and run in a thread.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import TIMEOUT_FFMPEG, ffmpeg_executable, ffprobe_executable

logger = logging.getLogger("stemdeck.takes")

# The rate both signals are compared at. 0.125 ms a sample: the offset is
# found far finer than one video frame (33 ms at 30 fps) needs.
SYNC_RATE = 8000
# Only these frequencies take part: below, room rumble and mains hum; above,
# what a webcam microphone and its AAC or Opus encoder keep least of.
BAND_HZ = (100.0, 3500.0)
# How far from the browser's estimate the match is looked for. The estimate
# was off by up to 530 ms on real hardware; 2 s leaves room for a slow camera.
SEARCH_MS = 2000.0
# One comparison window, and how much of the camera file is scanned. Short
# windows keep clock drift between the two microphones (up to about 100 ppm,
# 3 ms over 30 s) from smearing the peak; several give a take that starts
# quietly another chance.
WINDOW_SEC = 30.0
MIN_WINDOW_SEC = 5.0
MAX_SCAN_SEC = 180.0
# Below this RMS (dBFS) a window counts as silent on that side.
SILENCE_DBFS = -60.0
# Acceptance, set from synthetic runs (60 matched pairs per room, 30
# unrelated pairs each; the cases are kept in tests/test_av_sync.py):
# - unrelated signals (other music through a room, noise against music) gave
#   a peak-to-median ratio (PMR) of 3.0 to 6.1 and a peak ratio of 0.5 to
#   1.05 over the +-2 s search;
# - the camera hearing the instrument through a room gave a PMR of 21 to 270
#   and a peak ratio of 2.0 to 23 (rooms from RT60 0.4 s at 10 dB SNR to
#   RT60 0.8 s with the reverb 9 dB over the direct sound at -5 dB SNR), all
#   within 0.1 ms of the truth;
# - the harshest room (RT60 1.0 s, -10 dB SNR) gave PMR 7.9 to 30: 54 of 60
#   accepted, none more than 10 ms off, the rest fell back to the estimate.
# 12 is twice the highest unrelated PMR; 1.3 is above every unrelated peak
# ratio. The peak ratio compares the peak with anything outside it and its
# reflections (REFLECTION_MS before it, PEAK_EXCLUSION_MS after).
MIN_PMR = 12.0
MIN_PEAK_RATIO = 1.3
PEAK_EXCLUSION_MS = 20.0
# Reflections: how far after the highest peak the direct sound is looked
# for, and how high its peak must reach.
REFLECTION_MS = 120.0
DIRECT_FRACTION = 0.5
# Clear windows whose offsets differ by more than this are not trusted:
# clock drift between the two microphones (up to about 100 ppm) moves the
# match 18 ms over the 180 s scanned, far less.
MAX_WINDOW_DISAGREEMENT_MS = 40.0
# A peak this close to the edge of the search is not trusted: the true match
# may lie just outside it.
EDGE_MS = 20.0


@dataclass
class SyncResult:
    """The outcome of one measurement. `offset_ms` is None unless `ok`."""

    ok: bool
    reason: str
    offset_ms: float | None = None
    confidence: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


def _rms_dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return -200.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20.0 * np.log10(rms) if rms > 0 else -200.0


def _confidence(pmr: float, peak_ratio: float) -> float:
    """0..1; 0.5 is exactly the acceptance line (MIN_PMR and MIN_PEAK_RATIO
    both just met), 1.0 twice the PMR needed with a clear peak."""
    by_pmr = min(1.0, 0.5 * pmr / MIN_PMR)
    by_ratio = min(1.0, 0.5 * peak_ratio / MIN_PEAK_RATIO)
    return round(min(by_pmr, by_ratio), 3)


def gcc_phat(c: np.ndarray, r: np.ndarray, rate: int, max_lag: int) -> np.ndarray:
    """|GCC-PHAT| of `r` against `c` for lags 0..max_lag: entry k is how well
    c[m] matches r[m + k]. Band-limited to BAND_HZ."""
    n = 1
    while n < len(c) + len(r):
        n *= 2
    spec = np.fft.rfft(r, n) * np.conj(np.fft.rfft(c, n))
    mag = np.abs(spec)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    band = (freqs >= BAND_HZ[0]) & (freqs <= BAND_HZ[1])
    floor = 1e-9 * float(mag[band].max()) if band.any() and mag[band].max() > 0 else 1e-30
    weighted = np.where(band, spec / np.maximum(mag, floor), 0.0)
    corr = np.fft.irfft(weighted, n)
    return np.abs(corr[: max_lag + 1])


def _peak_width_ms(corr: np.ndarray, k: int, rate: int) -> float:
    half = corr[k] / 2.0
    lo = k
    while lo > 0 and corr[lo - 1] > half:
        lo -= 1
    hi = k
    while hi < len(corr) - 1 and corr[hi + 1] > half:
        hi += 1
    return (hi - lo + 1) * 1000.0 / rate


def _direct_path(corr: np.ndarray, k: int, rate: int) -> int:
    """The direct sound's lag, from the highest peak `k`.

    The camera microphone hears the instrument first by the direct path and
    then by reflections, each later, so each matches the take at a smaller
    lag. In a reverberant room a reflection can beat the direct sound to the
    highest peak (measured on the synthetic rooms: up to 29 ms off). So the
    largest lag up to REFLECTION_MS after `k` whose peak reaches
    DIRECT_FRACTION of the highest is the direct sound."""
    refl = int(round(REFLECTION_MS * rate / 1000.0))
    hi = min(len(corr) - 1, k + refl)
    need = DIRECT_FRACTION * corr[k]
    best = k
    for j in range(k + 1, hi):
        if corr[j] >= need and corr[j] >= corr[j - 1] and corr[j] >= corr[j + 1]:
            best = j
    return best


def _window(
    cam: np.ndarray, raw: np.ndarray, rate: int, start: int, length: int, estimate_ms: float
) -> SyncResult:
    """Compare cam[start:start+length] with the raw audio around the estimate."""
    search = int(round(SEARCH_MS * rate / 1000.0))
    est = int(round(estimate_ms * rate / 1000.0))
    c = cam[start : start + length]
    # raw[s0 : s0 + length + 2 * search], zeros where the take has no sample.
    s0 = start + est - search
    seg = np.zeros(length + 2 * search, dtype=np.float64)
    a, b = max(0, s0), min(len(raw), s0 + len(seg))
    if b > a:
        seg[a - s0 : b - s0] = raw[a:b]
    window_sec = round(length / rate, 3)
    start_sec = round(start / rate, 3)
    cam_db, raw_db = _rms_dbfs(c), _rms_dbfs(seg)
    base = {"windowSec": window_sec, "windowStartSec": start_sec}
    if cam_db < SILENCE_DBFS or raw_db < SILENCE_DBFS:
        return SyncResult(
            False,
            "silent",
            metrics={**base, "cameraDbfs": round(cam_db, 1), "takeDbfs": round(raw_db, 1)},
        )
    corr = gcc_phat(c.astype(np.float64), seg, rate, 2 * search)
    k = _direct_path(corr, int(np.argmax(corr)), rate)
    peak = float(corr[k])
    median = float(np.median(corr))
    pmr = peak / median if median > 0 else 0.0
    # Rivals: everything but the peak itself and the reflections before it
    # (a reflection reaches the camera later, so it matches at a smaller lag).
    excl = int(round(PEAK_EXCLUSION_MS * rate / 1000.0))
    refl = int(round(REFLECTION_MS * rate / 1000.0))
    rivals = np.concatenate([corr[: max(0, k - refl)], corr[k + excl + 1 :]])
    rival = float(rivals.max()) if rivals.size else 0.0
    peak_ratio = peak / rival if rival > 0 else float("inf")
    # Sub-sample peak: a parabola through the peak and its neighbours.
    frac = 0.0
    if 0 < k < len(corr) - 1:
        y0, y1, y2 = corr[k - 1], corr[k], corr[k + 1]
        den = y0 - 2.0 * y1 + y2
        if den < 0:
            frac = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
    lag = k + frac + est - search  # camera sample 0 of the file, in raw samples
    offset_ms = lag * 1000.0 / rate
    edge = int(round(EDGE_MS * rate / 1000.0))
    metrics = {
        **base,
        "pmr": round(pmr, 2),
        "peakRatio": round(min(peak_ratio, 1e6), 3),
        "widthMs": round(_peak_width_ms(corr, k, rate), 3),
    }
    confidence = _confidence(pmr, min(peak_ratio, 1e6))
    if k < edge or k > len(corr) - 1 - edge:
        return SyncResult(False, "low-confidence", offset_ms, min(confidence, 0.49), metrics)
    if pmr < MIN_PMR or peak_ratio < MIN_PEAK_RATIO:
        return SyncResult(False, "low-confidence", offset_ms, min(confidence, 0.49), metrics)
    return SyncResult(True, "ok", offset_ms, confidence, metrics)


def measure_offset(
    cam: np.ndarray, raw: np.ndarray, rate: int = SYNC_RATE, estimate_ms: float = 0.0
) -> SyncResult:
    """Where cam's sample 0 sits on raw's clock, in ms (pure).

    `cam` is the camera file's sound, sample 0 at the file's t=0; `raw` the
    take's sound from raw frame 0; both mono at `rate`. The earliest clear
    window wins, unless clear windows disagree; without a clear one the
    result says why ("silent" when every window was
    quiet on one side, else "low-confidence")."""
    cam = np.asarray(cam, dtype=np.float64).ravel()
    raw = np.asarray(raw, dtype=np.float64).ravel()
    if not np.isfinite(estimate_ms):
        estimate_ms = 0.0
    scan = min(len(cam), int(MAX_SCAN_SEC * rate))
    win = int(WINDOW_SEC * rate)
    min_win = int(MIN_WINDOW_SEC * rate)
    if scan < min_win or len(raw) < min_win:
        return SyncResult(False, "silent", metrics={"cameraSec": round(len(cam) / rate, 3)})
    results: list[SyncResult] = []
    start = 0
    while start < scan:
        length = min(win, scan - start)
        if length < min_win and results:
            break
        results.append(_window(cam, raw, rate, start, length, estimate_ms))
        start += win
    good = [r for r in results if r.ok]
    if good:
        offsets = [r.offset_ms for r in good if r.offset_ms is not None]
        spread = max(offsets) - min(offsets)
        if spread > MAX_WINDOW_DISAGREEMENT_MS:
            # Two clear matches at different places: one of them is the wrong
            # thing (a repeated riff, the backing through the speakers). A
            # wrong offset is worse than the estimate.
            best = max(good, key=lambda r: r.confidence or 0.0)
            metrics = {**best.metrics, "disagreeMs": round(spread, 2)}
            return SyncResult(False, "low-confidence", None, min(best.confidence, 0.49), metrics)
        # The earliest clear window: the offset is that of the file's t=0,
        # and the two microphones' clocks drift apart later in the take.
        return good[0]
    measured = [r for r in results if r.reason == "low-confidence"]
    if measured:
        best = max(measured, key=lambda r: r.metrics.get("pmr", 0.0))
        return SyncResult(False, "low-confidence", None, best.confidence, best.metrics)
    return SyncResult(False, "silent", metrics=results[0].metrics if results else {})


# ---------------------------------------------------------------------------
# ffmpeg side (blocking)


class SyncReadError(RuntimeError):
    """ffmpeg or ffprobe could not read a file."""


def probe_streams(path: Path) -> dict[str, float | None]:
    """{"video": start_time or None, "audio": start_time or None} of the
    first video and audio streams; a key is absent when there is no such
    stream."""
    proc = subprocess.run(  # noqa: S603 -- list args, no shell, trusted ffprobe
        [
            ffprobe_executable(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,start_time",
            "-of",
            "json",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_FFMPEG,
    )
    if proc.returncode != 0:
        raise SyncReadError(proc.stderr.strip()[-400:] or "ffprobe failed")
    out: dict[str, float | None] = {}
    for stream in json.loads(proc.stdout or "{}").get("streams") or []:
        kind = stream.get("codec_type")
        if kind not in ("video", "audio") or kind in out:
            continue
        try:
            out[kind] = float(stream.get("start_time"))
        except (TypeError, ValueError):
            out[kind] = None
    return out


def read_mono(
    path: Path, seconds: float, *, start_at: float | None = None, rate: int = SYNC_RATE
) -> np.ndarray:
    """The first audio stream of `path`, mono at `rate`, up to `seconds`.

    With `start_at` (a time in the file, in seconds) sample 0 is that time:
    the audio is padded with silence or cut so (aresample first_pts=0), which
    puts a camera file's sound on its picture's clock even when the two
    streams start at different times or the encoder has a start delay."""
    af = []
    if start_at is not None:
        af.append(f"asetpts=PTS-({start_at:.6f})/TB")
        af.append("aresample=first_pts=0")
    af.append(f"aresample={rate}")
    proc = subprocess.run(  # noqa: S603 -- list args, no shell, trusted ffmpeg
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            ",".join(af),
            "-ac",
            "1",
            "-t",
            f"{seconds:.3f}",
            "-f",
            "f32le",
            "pipe:1",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=TIMEOUT_FFMPEG,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip()[-400:]
        raise SyncReadError(tail or "ffmpeg failed")
    return np.frombuffer(proc.stdout, dtype="<f4")


def measure_take_offset(camera: Path, raw_wav: Path, estimate_ms: float) -> SyncResult:
    """Measure a finished take (blocking, ffmpeg). Never raises: a failure
    is a result with reason "measure-failed" (logged)."""
    try:
        streams = probe_streams(camera)
        if "audio" not in streams:
            return SyncResult(False, "no-camera-audio")
        # The picture's t=0, as the render takes it (setpts=PTS-STARTPTS).
        video_start = streams.get("video") or 0.0
        cam = read_mono(camera, MAX_SCAN_SEC, start_at=video_start)
        need = max(0.0, estimate_ms / 1000.0) + MAX_SCAN_SEC + SEARCH_MS / 1000.0 + 1.0
        raw = read_mono(raw_wav, need)
    except (OSError, subprocess.SubprocessError, SyncReadError, ValueError):
        logger.warning("av sync: could not read %s", camera, exc_info=True)
        return SyncResult(False, "measure-failed")
    result = measure_offset(cam, raw, SYNC_RATE, estimate_ms)
    logger.info(
        "av sync %s: %s offset %s ms, confidence %s, %s",
        camera.parent.name,
        result.reason,
        None if result.offset_ms is None else round(result.offset_ms, 2),
        result.confidence,
        result.metrics,
    )
    return result
