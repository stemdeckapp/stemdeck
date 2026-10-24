"""Suggest EQ: turn "measured spectrum minus target" into a few ordinary EQ
bands, and pick a master loudness preset.

This is the fitter agreed with the mastering report (01-mastering.md 5.5,
``fit_master_eq``); it works for a lane as well as for the master. Its output
is nothing but the E1 EQ descriptor's bands (bell, shelves, low cut, the
Keys panel's tilt) with the engine's own maths (fx_filters.py), so a
suggestion is an EQ the user can see, drag, preview, keep or undo.

The fit, on the 31 third-octave bands (eq_analysis.py):

1. Error = target - measured, with the overall level removed (the weighted
   mean of the difference: only the shape is compared; loudness is the
   output gain's job, below). Clipped to +-15 dB, then smoothed over three
   neighbouring bands (2/3 octave), so a narrow resonance or one loud note
   is not chased: suggestions are broad, the kind a person can judge.
2. A tolerance range around the target (TOL_DB: +-1 dB in the mids, up to
   +-5 dB at the extremes, where songs differ most): only what lies outside
   it is asked for (a soft threshold), so a suggestion moves a lane into the
   normal range rather than onto one line. Too little bass is tolerated twice
   as much as too much (Elowsson and Friberg: deviations below the mean in
   the bass are "generally less alarming").
3. Weights: a band counts fully where the signal has energy (within 30 dB
   of its loudest band) and fades to nothing 45 dB down; the extremes
   (20 to 31.5 Hz, 16 and 20 kHz) count less. No boost is asked where the
   signal lacks 12 to 20 dB or more against the target: then the content is
   missing, not dull (a song with no bass instrument is not given one, an
   encoder's cut-off is not lifted). Cuts are allowed anywhere.
4. Greedy: of every candidate band the layout allows (each type at the
   nominal third-octave frequencies in its range and a few Q values), the
   one that lowers the cost most is added, its gain from weighted least
   squares on its per-dB shape, clamped to +-SUGGEST_GAIN_MAX, rounded to
   0.5 dB and checked one step either way. Cost = weighted mean square of
   what is still outside the tolerance (level removed) plus LAMBDA *
   (gain/6)^2 per band, so a big band must earn its size. A band is added
   only while it improves the cost by at least max(MU_ABS, MU_REL * cost);
   after each addition every band is refined (frequency one step either way,
   Q, gain). Bands of one type stay an octave apart, the whole curve stays
   within +-COMBINED_MAX_DB from 40 Hz up, and bands under MIN_GAIN_DB are
   dropped.
5. Loudness-neutral by design: the output gain is minus the change in
   K-weighted (BS.1770) loudness the bands cause on the measured, gated
   spectrum, so the suggestion is not simply louder. The estimate and what
   is left of it are reported; tests render through ffmpeg and measure it.

Layouts: "free" (the graphical panels: any number of bells and shelves up to
the band limit, plus a gentle low cut), "master" (the same, low cut only in
the sub range), and the fixed panels "bass", "guitar" and "keys", whose
knobs are fixed bands (static/js/eq/eqPedalMath.js, mirrored here): each
slot is used at most once, in its own frequency range, and the result maps
back onto the knobs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from app.pipeline.eq_analysis import (
    _BIN_FREQS,
    _K2,
    ANALYSIS_RATE,
    CENTRES_HZ,
    N_BANDS,
    NOMINAL_HZ,
    bands_db,
)
from app.pipeline.fx_filters import Band, band_sections, section_coefficients

SUGGEST_GAIN_MAX = 6.0
MIN_GAIN_DB = 1.0
GAIN_STEP = 0.5
LAMBDA = 0.15
MU_ABS = 0.1
MU_REL = 0.06
ERROR_CLIP_DB = 15.0
OUT_MAX_DB = 12.0
MAX_BANDS_LANE = 5
MAX_BANDS_MASTER = 6
REFINE_ROUNDS = 2
# Bands of one type at least this far apart (octaves).
CROWD_OCT = 1.0
# The combined suggestion stays within this, from 40 Hz up (dB).
COMBINED_MAX_DB = 7.0

# The tolerance range around a target (dB, either side), per band: a
# deviation inside it is normal and asks for nothing. Wider in the bass and
# the top octave, where songs differ most (Elowsson and Friberg 2017, figure 3:
# the standard deviation of the LTAS across 12,345 tracks is highest at low
# and high frequencies and lowest from 200 Hz to 4 kHz).
TOL_DB = np.array(
    [5.0, 5.0, 5.0, 4.5, 4.0, 3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
     1.0, 1.0, 1.0, 1.0, 1.5, 2.0, 2.0, 2.0, 2.5, 2.5, 3.0, 3.5, 4.0, 5.0]
)  # fmt: skip
# "Deviations below the mean in the bass are generally less alarming" (the
# same paper): less bass than the target is tolerated this much more.
BASS_LOW_HZ = 125.0
BASS_SHORT_FACTOR = 2.0
# Boosts only where there is something to lift: how far under the target a
# band may be and still be boosted (dB, level removed).
BOOST_DEFICIT_FULL_DB = 12.0
BOOST_DEFICIT_NONE_DB = 20.0

FREE_Q = (0.7, 1.0, 1.4, 2.0)
PEDAL_Q = (1.0, 1.4, 2.0)

# Band weights before the signal's own (index = band; 20, 25, 31.5 Hz and
# 16, 20 kHz count less).
_BASE_W = np.ones(N_BANDS)
_BASE_W[[0, 1]] = 0.25
_BASE_W[2] = 0.5
_BASE_W[29] = 0.5
_BASE_W[30] = 0.25

# Points each band's response is evaluated at (power-averaged): four inside
# the band, so a band's level after an EQ is close to what the fine spectrum
# gives (checked exactly on the fine spectrum afterwards).
_SUB = np.array([-1 / 8, -1 / 24, 1 / 24, 1 / 8])
_EVAL_F = np.array([fc * 2.0**s for fc in CENTRES_HZ for s in _SUB])


def _nominal_between(lo: float, hi: float) -> tuple[float, ...]:
    return tuple(float(f) for f in NOMINAL_HZ if lo <= f <= hi)


@dataclass(frozen=True)
class Slot:
    """One place a suggested band may go. ``reuse``: any number of bands of
    this kind (the graphical panels); otherwise one at most (a knob)."""

    id: str
    type: str
    freqs: tuple[float, ...]
    qs: tuple[float, ...] = (0.707,)
    gain_max: float = SUGGEST_GAIN_MAX
    reuse: bool = False
    dynamic_shelf: bool = False  # the Bass EQ's low shelf: its corner moves with the gain

    @property
    def gainless(self) -> bool:
        return self.type in ("lowcut", "highcut")


# The Bass EQ's low shelf corner for a gain (eqPedalMath.dynamicShelfFreq).
def dynamic_shelf_freq(g: float, f_min: float = 75.0, f_max: float = 400.0) -> float:
    a = abs(g)
    if a <= 3:
        return f_min
    return round(f_min * (f_max / f_min) ** ((min(a, 15.0) - 3) / 12.0), 1)


_FREE = (
    Slot("bell", "bell", _nominal_between(40, 16000), FREE_Q, reuse=True),
    # One shelf of each kind: two stacked shelves read as one, less clearly.
    Slot("lowshelf", "lowshelf", _nominal_between(63, 250)),
    Slot("highshelf", "highshelf", _nominal_between(2000, 10000)),
)
LAYOUTS: dict[str, tuple[Slot, ...]] = {
    "free": (*_FREE, Slot("lowcut", "lowcut", _nominal_between(25, 125))),
    "master": (*_FREE, Slot("lowcut", "lowcut", _nominal_between(20, 31.5))),
    # static/js/eq/eqPedalMath.js PEDALS.bass
    "bass": (
        Slot("hpf", "lowcut", _nominal_between(25, 63)),
        Slot("ls", "lowshelf", (75.0,), dynamic_shelf=True),
        Slot("low", "bell", _nominal_between(35, 500), PEDAL_Q),
        Slot("mid", "bell", _nominal_between(100, 1600), PEDAL_Q),
        Slot("high", "bell", _nominal_between(375, 9700), PEDAL_Q),
        Slot("hs", "highshelf", (1000.0,)),
    ),
    # PEDALS.guitar
    "guitar": (
        Slot("hpf", "lowcut", _nominal_between(40, 160)),
        Slot("ls", "lowshelf", (200.0,)),
        Slot("low", "bell", _nominal_between(35, 500), PEDAL_Q),
        Slot("mid", "bell", _nominal_between(250, 5000), PEDAL_Q),
        Slot("high", "bell", _nominal_between(1000, 16000), PEDAL_Q),
        Slot("hs", "highshelf", (1000.0,)),
    ),
    # KEYS (lowcut 20-300, body 120 shelf, warmth 250 Q 0.8, presence 2-5k Q 1,
    # air 10k shelf, tone tilt at 1 kHz +-6)
    "keys": (
        Slot("lowcut", "lowcut", _nominal_between(40, 160)),
        Slot("body", "lowshelf", (120.0,)),
        Slot("warmth", "bell", (250.0,), (0.8,)),
        Slot("presence", "bell", _nominal_between(2000, 5000), (1.0,)),
        Slot("air", "highshelf", (10000.0,)),
        Slot("tone", "tilt", (1000.0,)),
    ),
}
FIXED_LAYOUTS = ("bass", "guitar", "keys")


@dataclass(frozen=True)
class FitBand:
    slot: str
    band: Band

    def to_json(self) -> dict:
        b = self.band
        return {"slot": self.slot, "type": b.type, "f": b.f, "g": b.g, "q": b.q, "slope": b.slope}


@dataclass
class FitResult:
    bands: list[FitBand] = field(default_factory=list)
    error_db: np.ndarray = field(
        default_factory=lambda: np.zeros(N_BANDS)
    )  # smoothed target - measured
    weights: np.ndarray = field(default_factory=lambda: np.zeros(N_BANDS))


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _sections_mag2(band: Band, freqs: np.ndarray, fs: float) -> np.ndarray:
    """|H|^2 of one band at ``freqs`` (the engine's own sections)."""
    out = np.ones(freqs.shape)
    z = np.exp(-1j * 2.0 * np.pi * freqs / fs)
    for sec in band_sections(band):
        b0, b1, b2, a1, a2 = section_coefficients(sec, fs)
        h = (b0 + b1 * z + b2 * z * z) / (1.0 + a1 * z + a2 * z * z)
        out *= np.abs(h) ** 2
    return out


def eq_mag2(bands: list[Band], freqs: np.ndarray, out_db: float = 0.0, fs: float = ANALYSIS_RATE):
    """|H|^2 of a whole EQ (bands and output gain) at ``freqs``."""
    m = np.full(freqs.shape, 10.0 ** (out_db / 10.0))
    for b in bands:
        m *= _sections_mag2(b, freqs, fs)
    return m


def band_response_db(band: Band) -> np.ndarray:
    """A band's response on the 31 bands (power average of 4 points in each)."""
    m = _sections_mag2(band, _EVAL_F, ANALYSIS_RATE).reshape(N_BANDS, len(_SUB))
    return 10.0 * np.log10(np.maximum(m.mean(axis=1), 1e-30))


def apply_eq(power: np.ndarray, bands: list[Band], out_db: float = 0.0) -> np.ndarray:
    """A fine (FFT-bin) power spectrum after an EQ: exact, at the analysis rate."""
    return power * eq_mag2(bands, _BIN_FREQS, out_db)


def loudness_change_db(power: np.ndarray, bands: list[Band], out_db: float = 0.0) -> float:
    """How much an EQ changes the K-weighted loudness of ``power`` (dB/LU)."""
    before = float(np.sum(power * _K2))
    if before <= 0:
        return 0.0
    after = float(np.sum(apply_eq(power, bands, out_db) * _K2))
    return 10.0 * math.log10(max(after, 1e-30) / before)


# ---------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------


def weights_for(measured_db: np.ndarray) -> np.ndarray:
    peak = float(np.max(measured_db))
    audible = np.clip((measured_db - (peak - 45.0)) / 15.0, 0.0, 1.0)
    return _BASE_W * audible


def _demean(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    sw = float(w.sum())
    return x - (float(w @ x) / sw if sw > 0 else 0.0)


def _cost(resid: np.ndarray, w: np.ndarray) -> float:
    sw = float(w.sum())
    if sw <= 0:
        return 0.0
    r = _demean(resid, w)
    return float(w @ (r * r)) / sw


def _tcost(
    resid: np.ndarray, w: np.ndarray, boost_ok: np.ndarray | None = None, scale: float = 1.0
) -> float:
    """The fit's cost: weighted mean square of what lies outside the
    tolerance range, after the level is removed."""
    sw = float(w.sum())
    if sw <= 0:
        return 0.0
    r = tolerated_error(_demean(resid, w), boost_ok, scale)
    return float(w @ (r * r)) / sw


def outside_rms_db(diff: np.ndarray, w: np.ndarray) -> float:
    """RMS (dB) of the deviation outside the tolerance range."""
    return math.sqrt(_tcost(np.asarray(diff, dtype=np.float64), w))


def weighted_rms_db(diff: np.ndarray, w: np.ndarray) -> float:
    """RMS deviation (dB) with the level removed: the distance to a target."""
    return math.sqrt(_cost(np.asarray(diff, dtype=np.float64), w))


def smoothed_error(measured_db: np.ndarray, target_db: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Target minus measured, level removed, clipped and smoothed (2/3 octave)."""
    e = _demean(np.asarray(target_db, float) - np.asarray(measured_db, float), w)
    e = np.clip(e, -ERROR_CLIP_DB, ERROR_CLIP_DB)
    padded = np.concatenate([[e[0]], e, [e[-1]]])
    return 0.25 * padded[:-2] + 0.5 * padded[1:-1] + 0.25 * padded[2:]


def tolerated_error(
    error: np.ndarray, boost_ok: np.ndarray | None = None, scale: float = 1.0
) -> np.ndarray:
    """The part of the error outside the tolerance range (soft threshold):
    inside the range nothing is asked for, outside only the excess. Below
    BASS_LOW_HZ a lack of bass is tolerated BASS_SHORT_FACTOR times more.
    ``boost_ok`` (0..1 per band) scales what a boost may be asked for."""
    tol = TOL_DB * scale
    tol_up = tol.copy()  # error > 0: measured under the target (a boost)
    tol_up[np.asarray(CENTRES_HZ) < BASS_LOW_HZ] *= BASS_SHORT_FACTOR
    up = np.maximum(0.0, error - tol_up)
    if boost_ok is not None:
        up = up * boost_ok
    return np.where(error > 0, up, np.minimum(0.0, error + tol))


def boost_allowed(measured_db: np.ndarray, target_db) -> np.ndarray:
    """Where a boost may be suggested: not where the signal lacks so much
    against the target that the content is missing rather than dull (a song
    with no bass instrument, an encoder's cut-off, a stem's empty octaves).
    Full up to BOOST_DEFICIT_FULL_DB under the target (level removed,
    smoothed), none from BOOST_DEFICIT_NONE_DB."""
    m = np.asarray(measured_db, dtype=float)
    deficit = smoothed_error(m, target_db, weights_for(m))
    span = BOOST_DEFICIT_NONE_DB - BOOST_DEFICIT_FULL_DB
    return np.clip((BOOST_DEFICIT_NONE_DB - deficit) / span, 0.0, 1.0)


def _make_band(slot: Slot, f: float, g: float, q: float) -> Band:
    if slot.dynamic_shelf:
        f = dynamic_shelf_freq(g)
    return Band(slot.type, float(f), float(g), float(q), 12)


def _round_gain(g: float, gmax: float) -> float:
    g = max(-gmax, min(gmax, g))
    return round(round(g / GAIN_STEP) * GAIN_STEP, 2)


class _Fitter:
    def __init__(self, error: np.ndarray, w: np.ndarray, boost_ok: np.ndarray, scale: float):
        self.scale = scale
        self.e = error
        self.w = w
        self.boost_ok = boost_ok
        self.cache: dict[tuple, np.ndarray] = {}

    def resp(self, slot: Slot, f: float, g: float, q: float) -> np.ndarray:
        key = (slot.id, slot.type, f, g, q, slot.dynamic_shelf)
        r = self.cache.get(key)
        if r is None:
            r = band_response_db(_make_band(slot, f, g, q))
            self.cache[key] = r
        return r

    def penalty(self, g: float) -> float:
        return LAMBDA * (g / 6.0) ** 2

    def best_for(self, slot: Slot, f: float, q: float, rest: np.ndarray):
        """Best gain for this band shape given the response of the others.
        Returns (cost, gain, response) or None."""
        target = self.e - rest
        w = self.w
        if slot.gainless:
            r = self.resp(slot, f, 0.0, q)
            if _too_much(rest + r):
                return None
            return _tcost(target - r, w, self.boost_ok, self.scale), 0.0, r
        # Per-dB shape at +-3 dB (the dB response is close to linear in the
        # gain, and odd for these filters): least squares on the demeaned
        # shapes against what is outside the tolerance, then the exact cost
        # at that gain and its neighbours on the 0.5 dB grid.
        shape = (self.resp(slot, f, 3.0, q) - self.resp(slot, f, -3.0, q)) / 6.0
        s = _demean(shape, w)
        tdm = tolerated_error(_demean(target, w), self.boost_ok, self.scale)
        denom = float(w @ (s * s)) + LAMBDA / 36.0 * float(w.sum())
        if denom <= 0:
            return None
        g0 = _round_gain(float(w @ (s * tdm)) / denom, slot.gain_max)
        best = None
        for d in (-GAIN_STEP, 0.0, GAIN_STEP):
            g = _round_gain(g0 + d, slot.gain_max)
            if abs(g) < MIN_GAIN_DB:
                continue
            r = self.resp(slot, f, g, q)
            if _too_much(rest + r):
                continue
            cost = _tcost(target - r, w, self.boost_ok, self.scale) + self.penalty(g)
            if best is None or cost < best[0]:
                best = (cost, g, r)
        return best


def _too_much(curve: np.ndarray) -> bool:
    """The whole suggestion's curve may not pass +-COMBINED_MAX_DB at any band
    from 40 Hz up (two overlapping +6 dB bands would make +12): a low cut is
    allowed to go deeper below 40 Hz, that is what it is for."""
    return bool(np.max(np.abs(curve[3:])) > COMBINED_MAX_DB)


def _crowded(chosen, slot: Slot, f: float, order: list[str] | None = None) -> bool:
    """Two bands of one type closer than CROWD_OCT would add up past the
    gain limit (two +6 dB shelves at 4 kHz are a +12 dB shelf): not allowed.
    On a fixed panel (``order``: its slot ids) the bell knobs also keep their
    order, so "low" never sits above "mid"."""
    for c in chosen:
        if c[0].type == slot.type and abs(math.log2(c[1] / f)) < CROWD_OCT - 1e-6:
            return True
        if order and slot.type == "bell" and c[0].type == "bell" and c[0].id != slot.id:
            mine, theirs = order.index(slot.id), order.index(c[0].id)
            if (mine < theirs and f >= c[1]) or (mine > theirs and f <= c[1]):
                return True
    return False


def fit_eq(
    measured_db,
    target_db,
    layout: str = "free",
    max_bands: int = MAX_BANDS_LANE,
    tolerance: float = 1.0,
    boost_ref=None,
) -> FitResult:
    """Fit a few bands of ``layout`` so the measured third-octave spectrum
    moves towards the target. Both are 31 levels in dB (any offset).
    ``tolerance`` scales the tolerance range (0: fit the line itself; tests).
    ``boost_ref``: the levels that decide where a boost may go (default the
    measured ones). A fit on top of an EQ passes the lane's raw levels, so a
    boost already applied does not make room for more (no ratchet)."""
    measured = np.asarray(measured_db, dtype=np.float64)
    w = weights_for(measured)
    error = smoothed_error(measured, target_db, w)
    result = FitResult(error_db=error, weights=w)
    slots = LAYOUTS[layout]
    order = [sl.id for sl in slots] if layout in FIXED_LAYOUTS else None
    boost_ok = boost_allowed(
        measured if boost_ref is None else np.asarray(boost_ref, float), target_db
    )
    fitter = _Fitter(error, w, boost_ok, tolerance)
    chosen: list[tuple[Slot, float, float, float, np.ndarray]] = []  # slot, f, g, q, resp

    def total(skip: int = -1) -> np.ndarray:
        r = np.zeros(N_BANDS)
        for k, c in enumerate(chosen):
            if k != skip:
                r += c[4]
        return r

    def full_cost() -> float:
        return _tcost(error - total(), w, boost_ok, tolerance) + sum(
            fitter.penalty(c[2]) for c in chosen
        )

    current = full_cost()
    while len(chosen) < max_bands:
        used = {c[0].id for c in chosen}
        rest = total()
        best = None
        for slot in slots:
            if not slot.reuse and slot.id in used:
                continue
            for f in slot.freqs:
                if _crowded(chosen, slot, f, order):
                    continue
                for q in slot.qs:
                    got = fitter.best_for(slot, f, q, rest)
                    if got is None:
                        continue
                    cost = got[0] + sum(fitter.penalty(c[2]) for c in chosen)
                    if best is None or cost < best[0]:
                        best = (cost, slot, f, got[1], q, got[2])
        if best is None or current - best[0] < max(MU_ABS, MU_REL * current):
            break
        chosen.append((best[1], best[2], best[3], best[4], best[5]))
        current = best[0]
        for _ in range(REFINE_ROUNDS):
            for k in range(len(chosen)):
                slot, f, g, q, _r = chosen[k]
                rest = total(skip=k)
                others = sum(fitter.penalty(c[2]) for j, c in enumerate(chosen) if j != k)
                i = slot.freqs.index(f)
                near = slot.freqs[max(0, i - 1) : i + 2]
                cand = None
                others_list = chosen[:k] + chosen[k + 1 :]
                for f2 in near:
                    if _crowded(others_list, slot, f2, order):
                        continue
                    for q2 in slot.qs:
                        got = fitter.best_for(slot, f2, q2, rest)
                        if got is None:
                            continue
                        if cand is None or got[0] < cand[0]:
                            cand = (got[0], f2, got[1], q2, got[2])
                if cand is not None and cand[0] + others < full_cost() - 1e-9:
                    chosen[k] = (slot, cand[1], cand[2], cand[3], cand[4])
            current = full_cost()

    # Bands that refining left tiny are not worth a handle.
    kept = [c for c in chosen if c[0].gainless or abs(c[2]) >= MIN_GAIN_DB]
    ordered = sorted(kept, key=lambda c: c[1])
    if order:
        ordered = sorted(kept, key=lambda c: order.index(c[0].id))
    result.bands = [FitBand(c[0].id, _make_band(c[0], c[1], c[2], c[3])) for c in ordered]
    return result


def fit_master_eq(mix_db, target_db, max_bands: int = MAX_BANDS_MASTER) -> FitResult:
    """The interface named in 01-mastering.md 5.5: the master's fit."""
    return fit_eq(mix_db, target_db, layout="master", max_bands=max_bands)


def neutral_out_db(power: np.ndarray, bands: list[Band], base_out: float = 0.0) -> float:
    """The output gain that keeps ``power``'s K-weighted loudness where it
    was with ``base_out``, after ``bands`` (rounded to 0.1 dB)."""
    change = loudness_change_db(power, bands, 0.0)
    out = base_out - change
    return round(max(-OUT_MAX_DB, min(OUT_MAX_DB, out)), 1)


# ---------------------------------------------------------------------------
# Plain-language reasons
# ---------------------------------------------------------------------------


def region_of(f: float) -> str:
    if f < 60:
        return "sub"
    if f < 250:
        return "bass"
    if f < 600:
        return "lowmid"
    if f < 2000:
        return "mid"
    if f < 5000:
        return "uppermid"
    if f < 10000:
        return "high"
    return "air"


def _nearest_band(f: float) -> int:
    return int(np.argmin(np.abs(np.log2(np.asarray(CENTRES_HZ) / f))))


def _mean_dev(dev: np.ndarray, w: np.ndarray, mask: np.ndarray) -> float:
    ww = w * mask
    if ww.sum() <= 0:
        return float(dev[mask].mean()) if mask.any() else 0.0
    return float(ww @ dev) / float(ww.sum())


def band_reasons(fit: FitResult) -> list[dict]:
    """One reason per band, from the numbers: how far the measured spectrum
    is from the target where the band acts, and what the band does. ``dev``
    is measured minus target (positive: too much there)."""
    dev = -fit.error_db
    fc = np.asarray(CENTRES_HZ)
    out = []
    for fb in fit.bands:
        b = fb.band
        f = b.f
        if b.type == "lowcut":
            mask = fc < f
            out.append(
                {
                    "key": "suggest.why.lowCut",
                    "vars": {"f": f, "dev": round(_mean_dev(dev, fit.weights, mask), 1)},
                }
            )
            continue
        if b.type == "lowshelf":
            mask = fc <= f * 1.5
            key = "suggest.why.lowShelfCut" if b.g < 0 else "suggest.why.lowShelfBoost"
        elif b.type == "highshelf":
            mask = fc >= f / 1.5
            key = "suggest.why.highShelfCut" if b.g < 0 else "suggest.why.highShelfBoost"
        elif b.type == "tilt":
            mask = fc >= f
            key = "suggest.why.tiltDarker" if b.g < 0 else "suggest.why.tiltBrighter"
        else:
            i = _nearest_band(f)
            mask = np.zeros(N_BANDS, dtype=bool)
            mask[max(0, i - 1) : i + 2] = True
            key = "suggest.why.bellCut" if b.g < 0 else "suggest.why.bellBoost"
        out.append(
            {
                "key": key,
                "vars": {
                    "f": f,
                    "g": b.g,
                    "dev": round(_mean_dev(dev, fit.weights, mask), 1),
                    "region": region_of(f),
                },
            }
        )
    return out


# ---------------------------------------------------------------------------
# The master preset
# ---------------------------------------------------------------------------

# How much the limiter may take off the loudest peaks before a preset asks
# too much of the material (dB). Past it a quieter target is suggested.
LIMIT_ALLOWANCE_DB = 4.0
# Material this dense (peak-to-loudness ratio under DENSE_PLR) and already
# this loud is mastered loud on purpose: "Louder" keeps its character.
DENSE_PLR = 11.0
DENSE_LUFS = -11.0
LOUDER_LUFS = -9.0
CEILING_DBTP = -1.0
CUSTOM_MIN_LUFS = -24.0


def recommend_master(i: float | None, tp: float | None, target_lufs: float | None) -> dict:
    """A master preset (masterMath.js MASTER_PRESETS) from the mix's
    integrated loudness ``i`` and true peak ``tp``: never louder than the
    material allows, i.e. never asking the limiter for more than
    LIMIT_ALLOWANCE_DB on the loudest peaks. Returns the preset, its
    settings, the estimated limiting and the reason (an i18n key and its
    numbers)."""
    want = -14.0 if target_lufs is None else float(target_lufs)
    if i is None or tp is None:
        return {
            "preset": "gentle",
            "target_lufs": None,
            "ceiling_dbtp": CEILING_DBTP,
            "comp": 0.0,
            "limit_db": 0.0,
            "why": {"key": "suggest.why.master.silent", "vars": {}},
        }
    plr = tp - i
    # The loudest target whose peaks the limiter trims by at most the allowance.
    allowed = CEILING_DBTP - plr + LIMIT_ALLOWANCE_DB

    def limiting(target: float) -> float:
        return round(max(0.0, target - CEILING_DBTP + plr), 1)

    nums = {"i": round(i, 1), "plr": round(plr, 1)}
    if plr < DENSE_PLR and i > DENSE_LUFS and allowed >= LOUDER_LUFS:
        return {
            "preset": "louder",
            "target_lufs": LOUDER_LUFS,
            "ceiling_dbtp": CEILING_DBTP,
            "comp": 0.5,
            "limit_db": limiting(LOUDER_LUFS),
            "why": {"key": "suggest.why.master.louder", "vars": {**nums, "target": LOUDER_LUFS}},
        }
    if allowed >= want:
        return {
            "preset": "streaming",
            "target_lufs": want,
            "ceiling_dbtp": CEILING_DBTP,
            "comp": 0.0,
            "limit_db": limiting(want),
            "why": {
                "key": "suggest.why.master.streaming",
                "vars": {**nums, "target": want, "limit": limiting(want)},
            },
        }
    if allowed < CUSTOM_MIN_LUFS:
        # Even the quietest target would squash the peaks: no loudness
        # change at all, only the limiter against clipping.
        return {
            "preset": "gentle",
            "target_lufs": None,
            "ceiling_dbtp": CEILING_DBTP,
            "comp": 0.0,
            "limit_db": round(max(0.0, tp - CEILING_DBTP), 1),
            "why": {"key": "suggest.why.master.gentle", "vars": nums},
        }
    target = math.floor(allowed * 2.0) / 2.0
    return {
        "preset": "custom",
        "target_lufs": target,
        "ceiling_dbtp": CEILING_DBTP,
        "comp": 0.0,
        "limit_db": limiting(target),
        "why": {
            "key": "suggest.why.master.dynamic",
            "vars": {**nums, "target": target, "want": want, "limit": limiting(target)},
        },
    }


def band_levels_after(power: np.ndarray, bands: list[Band], out_db: float = 0.0) -> np.ndarray:
    return bands_db(apply_eq(power, bands, out_db))
