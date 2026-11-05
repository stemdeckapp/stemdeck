"""Export side of the EQ: the same biquad coefficients as the live engine.

The studio EQ runs in the browser as native BiquadFilterNodes
(static/js/fxChain.js). Their coefficients are fixed by the Web Audio spec's
"Filters Characteristics" (the RBJ Audio EQ Cookbook, with Q in dB for
lowpass/highpass and the shelf slope fixed at S = 1). This module computes
exactly those coefficients and hands them to ffmpeg's ``biquad`` filter, so an
export runs the filter the user heard. Every function mirrors one in
static/js/eqMath.js; tests/test_fx_filters.py holds them to each other.

Pure functions, no I/O. The ``fx=`` export parameter is parsed and validated
here (``parse_fx_param``): base64url JSON, size-capped, every field typed and
range-checked, and nothing but numbers this module formats itself ever reaches
the ffmpeg filter graph. Contract: docs/plans/0.21.0/32-eq-engine-contract.md.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from dataclasses import dataclass, field

EQ_ID = "eq"
FX_WIRE_VERSION = 1

EQ_TYPES = ("bell", "lowshelf", "highshelf", "lowcut", "highcut", "notch", "bandpass", "tilt")
EQ_SLOPES = (12, 24, 36, 48)
FREQ_MIN, FREQ_MAX = 10.0, 20000.0
GAIN_MIN, GAIN_MAX = -30.0, 30.0
Q_MIN, Q_MAX = 0.025, 40.0
OUT_MIN, OUT_MAX = -24.0, 24.0
MAX_BANDS = 12
# Bands carry Q to 0.001, so a cut's "no resonance" reference is 0.707: q =
# 0.707 gives exactly the Butterworth section Qs (mirrors EQ_DEFAULT_Q).
DEFAULT_Q = 0.707

# The raw parameter, before decoding. 12 bands on each of a dozen lanes fit
# in under 6 KB; the cap keeps the whole URL well inside the server's 16 KB
# request-line limit.
FX_PARAM_MAX_CHARS = 8192
MAX_FX_LANES = 32
MAX_EFFECTS_PER_SLOT = 4
# Biquad sections in one ffmpeg command. Each is about 150 characters of
# filter graph, and Windows caps a whole command line at 32767 characters.
MAX_TOTAL_SECTIONS = 160

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_LANE_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")


class FxError(ValueError):
    """A malformed or out-of-range ``fx`` value. The message is safe to show."""


@dataclass(frozen=True)
class Band:
    type: str
    f: float
    g: float
    q: float
    slope: int


@dataclass(frozen=True)
class Eq:
    out: float
    bands: tuple[Band, ...]


@dataclass(frozen=True)
class Section:
    """One Web Audio BiquadFilterNode: its type, frequency, LINEAR Q, gain."""

    type: str
    f: float
    q: float
    g: float


@dataclass
class FxRequest:
    lanes: dict[str, list[Eq]] = field(default_factory=dict)
    master: list[Eq] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Maths (mirrors static/js/eqMath.js)
# ---------------------------------------------------------------------------


def band_is_neutral(band: Band) -> bool:
    if band.type in ("bell", "lowshelf", "highshelf", "tilt"):
        return band.g == 0
    return False


def butterworth_qs(order: int) -> list[float]:
    """Section Qs of an even-order Butterworth filter, lowest first."""
    return sorted(
        1.0 / (2.0 * math.cos((2 * k - 1) * math.pi / (2 * order)))
        for k in range(1, order // 2 + 1)
    )


def band_sections(band: Band) -> list[Section]:
    if band_is_neutral(band):
        return []
    t, f, g, q = band.type, band.f, band.g, band.q
    if t == "bell":
        return [Section("peaking", f, q, g)]
    if t == "lowshelf":
        return [Section("lowshelf", f, 0.0, g)]
    if t == "highshelf":
        return [Section("highshelf", f, 0.0, g)]
    if t == "notch":
        return [Section("notch", f, q, 0.0)]
    if t == "bandpass":
        return [Section("bandpass", f, q, 0.0)]
    if t == "tilt":
        return [Section("lowshelf", f, 0.0, -g / 2), Section("highshelf", f, 0.0, g / 2)]
    if t in ("lowcut", "highcut"):
        wa = "highpass" if t == "lowcut" else "lowpass"
        qs = butterworth_qs(band.slope // 6)
        qs[-1] *= q / DEFAULT_Q
        return [Section(wa, f, sq, 0.0) for sq in qs]
    return []


def eq_sections(eq: Eq) -> list[Section]:
    out: list[Section] = []
    for band in eq.bands:
        out.extend(band_sections(band))
    return out


def node_q(section: Section) -> float:
    """BiquadFilterNode.Q for the section: dB for lowpass/highpass."""
    if section.type in ("lowpass", "highpass"):
        return 20.0 * math.log10(section.q)
    if section.type in ("lowshelf", "highshelf"):
        return 1.0
    return section.q


def section_coefficients(section: Section, fs: float) -> tuple[float, float, float, float, float]:
    """(b0, b1, b2, a1, a2), normalised to a0 = 1, by the Web Audio spec."""
    f0 = min(max(section.f, 0.0), fs / 2)
    a = 10.0 ** (section.g / 40.0)
    w0 = 2.0 * math.pi * f0 / fs
    cw = math.cos(w0)
    sw = math.sin(w0)
    a_q = sw / (2.0 * section.q) if section.q else 0.0
    a_qdb = sw / (2.0 * 10.0 ** (node_q(section) / 20.0))
    a_s = (sw / 2.0) * math.sqrt(2.0)  # S = 1
    t = section.type
    if t == "lowpass":
        b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
        a0, a1, a2 = 1 + a_qdb, -2 * cw, 1 - a_qdb
    elif t == "highpass":
        b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
        a0, a1, a2 = 1 + a_qdb, -2 * cw, 1 - a_qdb
    elif t == "bandpass":
        b0, b1, b2 = a_q, 0.0, -a_q
        a0, a1, a2 = 1 + a_q, -2 * cw, 1 - a_q
    elif t == "notch":
        b0, b1, b2 = 1.0, -2 * cw, 1.0
        a0, a1, a2 = 1 + a_q, -2 * cw, 1 - a_q
    elif t == "peaking":
        b0, b1, b2 = 1 + a_q * a, -2 * cw, 1 - a_q * a
        a0, a1, a2 = 1 + a_q / a, -2 * cw, 1 - a_q / a
    elif t == "lowshelf":
        k = 2 * a_s * math.sqrt(a)
        b0 = a * ((a + 1) - (a - 1) * cw + k)
        b1 = 2 * a * ((a - 1) - (a + 1) * cw)
        b2 = a * ((a + 1) - (a - 1) * cw - k)
        a0 = (a + 1) + (a - 1) * cw + k
        a1 = -2 * ((a - 1) + (a + 1) * cw)
        a2 = (a + 1) + (a - 1) * cw - k
    elif t == "highshelf":
        k = 2 * a_s * math.sqrt(a)
        b0 = a * ((a + 1) + (a - 1) * cw + k)
        b1 = -2 * a * ((a - 1) + (a + 1) * cw)
        b2 = a * ((a + 1) + (a - 1) * cw - k)
        a0 = (a + 1) - (a - 1) * cw + k
        a1 = 2 * ((a - 1) - (a + 1) * cw)
        a2 = (a + 1) - (a - 1) * cw - k
    else:
        return (1.0, 0.0, 0.0, 0.0, 0.0)
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def coefficients_mag_db(c: tuple[float, float, float, float, float], f: float, fs: float) -> float:
    b0, b1, b2, a1, a2 = c
    w = 2.0 * math.pi * f / fs
    num = complex(
        b0 + b1 * math.cos(w) + b2 * math.cos(2 * w), -(b1 * math.sin(w) + b2 * math.sin(2 * w))
    )
    den = complex(
        1 + a1 * math.cos(w) + a2 * math.cos(2 * w), -(a1 * math.sin(w) + a2 * math.sin(2 * w))
    )
    return 20.0 * math.log10(abs(num) / abs(den))


def eq_response_db(eq: Eq, freqs: list[float], fs: float) -> list[float]:
    """|H| in dB at each frequency, output gain included."""
    coefs = [section_coefficients(s, fs) for s in eq_sections(eq)]
    return [eq.out + sum(coefficients_mag_db(c, f, fs) for c in coefs) for f in freqs]


# ---------------------------------------------------------------------------
# ffmpeg filter strings
# ---------------------------------------------------------------------------


def _num(v: float) -> str:
    # repr is the shortest string that reads back as the same double, so
    # ffmpeg gets bit-for-bit the coefficients computed here.
    return repr(float(v))


def section_filter(section: Section, fs: float) -> str:
    b0, b1, b2, a1, a2 = section_coefficients(section, fs)
    # r=f64: filter in double precision. With the default ("auto") the filter
    # would run in whatever format arrives, 16-bit for a 16-bit WAV.
    return (
        f"biquad=b0={_num(b0)}:b1={_num(b1)}:b2={_num(b2)}:a0=1.0:a1={_num(a1)}:a2={_num(a2)}:r=f64"
    )


def eq_filter(eq: Eq, fs: float) -> str:
    """The filter chain for one EQ at sample rate ``fs``, "" when it changes
    nothing (so a flat EQ leaves the graph exactly as it was)."""
    parts = [section_filter(s, fs) for s in eq_sections(eq)]
    if eq.out != 0:
        parts.append(f"volume={_num(10.0 ** (eq.out / 20.0))}")
    return ",".join(parts)


def slot_filter(effects: list[Eq], fs: float) -> str:
    """All effects of one slot, in order, "" when none changes the sound."""
    return ",".join(p for p in (eq_filter(e, fs) for e in effects) if p)


def slot_section_count(effects: list[Eq]) -> int:
    return sum(len(eq_sections(e)) for e in effects)


# ---------------------------------------------------------------------------
# Parsing and validation of the fx= parameter
# ---------------------------------------------------------------------------


def _reject_constant(name: str) -> float:
    raise FxError("fx: numbers must be finite")


def _number(v: object, lo: float, hi: float, what: str) -> float:
    # bool is an int in Python: refuse it explicitly.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise FxError(f"fx: {what} must be a number")
    x = float(v)
    if not math.isfinite(x) or x < lo or x > hi:
        raise FxError(f"fx: {what} out of range")
    return x


def _parse_band(raw: object) -> Band:
    if not isinstance(raw, list) or len(raw) != 5:
        raise FxError("fx: a band is [type, f, g, q, slope]")
    t, f, g, q, slope = raw
    if not isinstance(t, str) or t not in EQ_TYPES:
        raise FxError("fx: unknown band type")
    f = _number(f, FREQ_MIN, FREQ_MAX, "frequency")
    g = _number(g, GAIN_MIN, GAIN_MAX, "gain")
    q = _number(q, Q_MIN, Q_MAX, "Q")
    if isinstance(slope, bool) or not isinstance(slope, int) or slope not in EQ_SLOPES:
        raise FxError("fx: slope must be 12, 24, 36 or 48")
    return Band(t, f, g, q, slope)


def _parse_effect(raw: object) -> Eq:
    if not isinstance(raw, dict):
        raise FxError("fx: an effect must be an object")
    if set(raw) - {"id", "out", "b"}:
        raise FxError("fx: unknown effect field")
    if raw.get("id") != EQ_ID:
        raise FxError("fx: unknown effect")
    out = _number(raw.get("out", 0), OUT_MIN, OUT_MAX, "output gain")
    bands = raw.get("b", [])
    if not isinstance(bands, list) or len(bands) > MAX_BANDS:
        raise FxError("fx: too many bands")
    return Eq(out, tuple(_parse_band(b) for b in bands))


def _parse_slot(raw: object) -> list[Eq]:
    if not isinstance(raw, list) or len(raw) > MAX_EFFECTS_PER_SLOT:
        raise FxError("fx: a slot is a list of at most 4 effects")
    effects = [_parse_effect(e) for e in raw]
    if len(effects) > 1:
        raise FxError("fx: one EQ per slot")
    return effects


def parse_fx_param(raw: str | None) -> FxRequest:
    """Decode and validate ``fx=``. Empty means no effects. Raises FxError."""
    if not raw:
        return FxRequest()
    if len(raw) > FX_PARAM_MAX_CHARS or not _B64URL_RE.match(raw):
        raise FxError("fx: malformed")
    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        body = json.loads(text, parse_constant=_reject_constant)
    except FxError:
        raise
    except (binascii.Error, UnicodeDecodeError, ValueError, RecursionError):
        raise FxError("fx: malformed") from None
    if not isinstance(body, dict) or set(body) - {"v", "lanes", "master"}:
        raise FxError("fx: malformed")
    if body.get("v") != FX_WIRE_VERSION or isinstance(body.get("v"), bool):
        raise FxError("fx: unsupported version")
    lanes_raw = body.get("lanes", {})
    if not isinstance(lanes_raw, dict) or len(lanes_raw) > MAX_FX_LANES:
        raise FxError("fx: malformed lanes")
    lanes: dict[str, list[Eq]] = {}
    for name, slot in lanes_raw.items():
        if not _LANE_KEY_RE.match(name):
            raise FxError("fx: unknown lane")
        lanes[name] = _parse_slot(slot)
    master = _parse_slot(body.get("master", []))
    return FxRequest(lanes=lanes, master=master)


def check_section_budget(sections: int) -> None:
    if sections > MAX_TOTAL_SECTIONS:
        raise FxError("fx: too many EQ bands for one export")
