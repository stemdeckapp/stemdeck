"""Long-term average spectrum (LTAS) of a lane or a mix, loud passages only.

This is what "Suggest EQ" compares with a target curve (eq_targets.json) and
what the fitter (eq_fit.py) works on. Pure numpy; the audio arrives as 32-bit
float stereo at ANALYSIS_RATE (ffmpeg decodes it, app/api/suggest.py).

Method:

- Frames of FRAME (16384 samples, 371 ms) every HOP (4410 samples, 100 ms),
  Hann window, one real FFT per channel. 16384 points give 2.7 Hz bins, so
  even the 20 Hz third-octave band (17.8 to 22.4 Hz) holds a bin.
- Every frame's loudness is computed BS.1770-style from its spectrum: the
  power of each bin weighted by the K-filter's magnitude (the RBJ high shelf
  +4 dB at 1500 Hz and the high pass at 38 Hz that pyloudnorm uses to
  re-derive BS.1770's 48 kHz filters at other rates), summed over the two
  channels, -0.691 + 10 log10. Parseval makes that the windowed mean square
  of the K-filtered frame, so the number is a loudness in LUFS. It is an
  approximation of the standard's 400 ms rectangular blocks, used only to
  choose frames; the loudness the UI shows comes from ffmpeg's ebur128.
- **Gating (the rule that keeps silence, fades and bleed out):** the two
  gates of ITU-R BS.1770-4/5, applied to these frames. Absolute: frames under
  -70 LUFS are dropped. Relative: frames more than 10 LU under the energy mean
  of the frames that passed the absolute gate are dropped. What remains is
  the part of the song where the lane is actually playing; a guitar that only
  enters in the chorus is measured on the chorus, not on the verses where its
  stem carries a little bleed 20 dB down. The relative gate is the
  standard's own rule, used here for the same reason it exists there.
- One pass, constant memory: frames are accumulated into a histogram of
  loudness in GATE_STEP_LU steps (the way libebur128 gates), each bin holding
  the sum of its frames' power spectra. After the last frame the relative
  threshold is known and the bins above it are summed. The result is the
  mean power spectrum of the loud frames, at FFT resolution, so any EQ can be
  applied to it exactly afterwards (eq_fit.py).
- Third-octave bands: the 31 ISO 266 bands from 20 Hz to 20 kHz, exact
  centres 1000 * 2^(n/3), edges at a sixth of an octave either side; a band's
  level is the summed power of the bins inside it. Pink noise (equal power
  per octave) reads flat in these bands.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

ANALYSIS_RATE = 44100
FRAME = 16384
HOP = 4410
GATE_ABS_LUFS = -70.0
GATE_REL_LU = -10.0
GATE_STEP_LU = 0.25
_GATE_TOP_LUFS = 10.0
_GATE_BINS = int((_GATE_TOP_LUFS - GATE_ABS_LUFS) / GATE_STEP_LU)

# ISO 266 nominal centre frequencies, 20 Hz to 20 kHz (labels for people).
NOMINAL_HZ = (
    20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
    1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000,
)  # fmt: skip
N_BANDS = len(NOMINAL_HZ)
# Exact base-2 centres: 1000 * 2^(n/3), n = -17..13.
CENTRES_HZ = tuple(1000.0 * 2.0 ** (n / 3.0) for n in range(-17, 14))
_EDGE = 2.0 ** (1.0 / 6.0)
_TINY = 1e-30


def bin_freqs(rate: int = ANALYSIS_RATE, frame: int = FRAME) -> np.ndarray:
    return np.fft.rfftfreq(frame, 1.0 / rate)


def _rbj_mag2(f: np.ndarray, rate: float, kind: str, f0: float, q: float, gain_db: float = 0.0):
    """|H|^2 of an RBJ cookbook biquad at the frequencies ``f`` (Hz)."""
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / rate
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)
    if kind == "highshelf":
        k = 2.0 * math.sqrt(a) * alpha
        b = (a * ((a + 1) + (a - 1) * cw + k), -2 * a * ((a - 1) + (a + 1) * cw),
             a * ((a + 1) + (a - 1) * cw - k))  # fmt: skip
        den = ((a + 1) - (a - 1) * cw + k, 2 * ((a - 1) - (a + 1) * cw), (a + 1) - (a - 1) * cw - k)
    else:  # highpass
        b = ((1 + cw) / 2, -(1 + cw), (1 + cw) / 2)
        den = (1 + alpha, -2 * cw, 1 - alpha)
    z = np.exp(-1j * 2.0 * np.pi * np.asarray(f, dtype=np.float64) / rate)
    num = b[0] + b[1] * z + b[2] * z * z
    dd = den[0] + den[1] * z + den[2] * z * z
    return np.abs(num / dd) ** 2


def k_weight2(f: np.ndarray, rate: float = ANALYSIS_RATE) -> np.ndarray:
    """|K(f)|^2 of BS.1770's K-weighting at ``rate`` (pyloudnorm's design)."""
    shelf = _rbj_mag2(
        f, rate, "highshelf", 1681.974450955533, 0.7071752369554196, 3.999843853973347
    )
    hp = _rbj_mag2(f, rate, "highpass", 38.13547087602444, 0.5003270373238773)
    return shelf * hp


def band_index(freqs: np.ndarray) -> np.ndarray:
    """For every FFT bin, the third-octave band it belongs to (-1: none)."""
    idx = np.full(freqs.shape, -1, dtype=np.int64)
    for b, fc in enumerate(CENTRES_HZ):
        lo, hi = fc / _EDGE, fc * _EDGE
        idx[(freqs >= lo) & (freqs < hi)] = b
    return idx


_BIN_FREQS = bin_freqs()
_BAND_OF_BIN = band_index(_BIN_FREQS)
_K2 = k_weight2(_BIN_FREQS)
_WINDOW = np.hanning(FRAME).astype(np.float64)
# One-sided spectrum: every bin but DC and Nyquist stands for two.
_SIDES = np.full(FRAME // 2 + 1, 2.0)
_SIDES[0] = 1.0
_SIDES[-1] = 1.0
# |X|^2 * _SCALE is the bin's share of the windowed mean square (Parseval).
_SCALE = _SIDES / (FRAME * float(np.sum(_WINDOW**2)))


def bands_db(power: np.ndarray, band_of_bin: np.ndarray = _BAND_OF_BIN) -> np.ndarray:
    """Third-octave band levels (dB) of a power spectrum at FFT resolution."""
    sums = np.bincount(
        band_of_bin[band_of_bin >= 0], weights=power[band_of_bin >= 0], minlength=N_BANDS
    )
    return 10.0 * np.log10(sums[:N_BANDS] + _TINY)


def loudness_of_power(power: np.ndarray) -> float:
    """BS.1770-style loudness (LUFS) of a mean power spectrum."""
    return -0.691 + 10.0 * math.log10(float(np.sum(power * _K2)) + _TINY)


@dataclass
class Spectrum:
    """The gated long-term average of a signal."""

    power: np.ndarray  # mean power per FFT bin of the loud frames (both channels summed)
    frames: int  # frames analysed
    gated_frames: int  # frames that passed both gates
    seconds: float  # length of the audio analysed

    @property
    def silent(self) -> bool:
        return self.gated_frames == 0

    @property
    def bands(self) -> np.ndarray:
        return bands_db(self.power)

    @property
    def gated_seconds(self) -> float:
        return self.gated_frames * HOP / ANALYSIS_RATE


class SpectrumAccumulator:
    """Feed interleaved stereo float32 samples in any chunk sizes; ``result()``
    gives the gated average. Memory: the histogram, about 21 MB, plus one
    frame of carry-over."""

    def __init__(self) -> None:
        self._hist = np.zeros((_GATE_BINS, FRAME // 2 + 1), dtype=np.float64)
        self._count = np.zeros(_GATE_BINS, dtype=np.int64)
        self._energy = np.zeros(_GATE_BINS, dtype=np.float64)
        self._buf = np.zeros((0, 2), dtype=np.float32)
        self._frames = 0
        self._samples = 0

    def feed(self, interleaved: np.ndarray) -> None:
        x = np.asarray(interleaved, dtype=np.float32).reshape(-1, 2)
        self._samples += x.shape[0]
        buf = np.concatenate([self._buf, x]) if self._buf.size else x
        n = buf.shape[0]
        if n < FRAME:
            self._buf = buf
            return
        starts = range(0, n - FRAME + 1, HOP)
        for s in starts:
            self._frame(buf[s : s + FRAME])
        nxt = starts[-1] + HOP
        self._buf = buf[nxt:].copy()

    def _frame(self, frame: np.ndarray) -> None:
        self._frames += 1
        spec = np.fft.rfft(frame.astype(np.float64) * _WINDOW[:, None], axis=0)
        power = (np.abs(spec[:, 0]) ** 2 + np.abs(spec[:, 1]) ** 2) * _SCALE
        ms_k = float(np.sum(power * _K2))
        if ms_k <= 0.0:
            return
        lufs = -0.691 + 10.0 * math.log10(ms_k)
        if lufs <= GATE_ABS_LUFS:
            return
        b = min(_GATE_BINS - 1, int((lufs - GATE_ABS_LUFS) / GATE_STEP_LU))
        self._hist[b] += power
        self._count[b] += 1
        self._energy[b] += ms_k

    def finish(self) -> None:
        """Analyse the tail: the last partial frame, zero padded, when it is at
        least a quarter of a frame long (so a short sound is not lost)."""
        rest = self._buf.shape[0]
        if rest and (self._frames == 0 or rest >= FRAME // 4):
            pad = np.zeros((FRAME, 2), dtype=np.float32)
            pad[:rest] = self._buf[:FRAME]
            self._frame(pad)
        self._buf = np.zeros((0, 2), dtype=np.float32)

    def result(self) -> Spectrum:
        seconds = self._samples / ANALYSIS_RATE
        total = int(self._count.sum())
        if total == 0:
            return Spectrum(np.zeros(FRAME // 2 + 1), self._frames, 0, seconds)
        mean_energy = float(self._energy.sum()) / total
        threshold = -0.691 + 10.0 * math.log10(mean_energy) + GATE_REL_LU
        # A histogram bin is in when its centre is above the threshold.
        centres = GATE_ABS_LUFS + (np.arange(_GATE_BINS) + 0.5) * GATE_STEP_LU
        keep = centres > threshold
        gated = int(self._count[keep].sum())
        if gated == 0:
            return Spectrum(np.zeros(FRAME // 2 + 1), self._frames, 0, seconds)
        power = self._hist[keep].sum(axis=0) / gated
        return Spectrum(power, self._frames, gated, seconds)


def analyse(samples: np.ndarray) -> Spectrum:
    """The gated LTAS of a whole (n, 2) or interleaved float signal at
    ANALYSIS_RATE. Tests and scripts; the server streams into the accumulator."""
    acc = SpectrumAccumulator()
    acc.feed(np.asarray(samples, dtype=np.float32).reshape(-1))
    acc.finish()
    return acc.result()


# ---------------------------------------------------------------------------
# Cache form: the gated power spectrum in dB, three decimals (about 65 KB).
# ---------------------------------------------------------------------------


def spectrum_to_json(s: Spectrum) -> dict:
    db = 10.0 * np.log10(s.power + _TINY)
    return {
        "frames": s.frames,
        "gated_frames": s.gated_frames,
        "seconds": round(s.seconds, 3),
        "power_db": [round(float(v), 3) for v in db],
    }


def spectrum_from_json(data: dict) -> Spectrum:
    db = np.asarray(data["power_db"], dtype=np.float64)
    if db.shape != (FRAME // 2 + 1,) or not np.all(np.isfinite(db)):
        raise ValueError("bad cached spectrum")
    power = 10.0 ** (db / 10.0)
    power[db <= -299.0] = 0.0
    return Spectrum(power, int(data["frames"]), int(data["gated_frames"]), float(data["seconds"]))
