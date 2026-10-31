"""Mastering of an export: loudness target, optional glue compressor and a
true-peak limiter on the mix bus, rendered by ffmpeg.

The design and its measurements are in docs/plans/0.21.0/01-mastering.md
(section 4.1). In order, after the lanes are summed and the master EQ has run:

    [glue compressor] -> volume (measured gain) -> 4x oversampled alimiter -> back

1. Measure the mix as it reaches the master chain (ffmpeg ``ebur128``,
   BS.1770 gated integrated loudness and 4x true peak). The job's stored
   loudness is never reused: the mix depends on gains, mutes, EQ and takes.
2. With the compressor on, its threshold sits relative to that measured
   loudness (so an amount means the same on a quiet and a hot mix), and the
   mix is measured again after it: a compressor lowers loudness, and a gain
   computed before it lands short (01-mastering 4.1, measured 0.4 LU).
3. Gain = target - measured, then the limiter at the ceiling. ``alimiter``
   gets ``level=disabled`` (its default re-normalises to 0 dB) and runs at 4x
   the rate, so its ceiling holds the true peak, not only the samples.
4. The result is measured, and when limiting cost more than CORRECTION_TOL_LU
   one correction pass adds the difference to the gain and measures again.

The plan (the measurements and the final gain) is always made on the whole
song, so a region export or a short "render and listen" excerpt gets exactly
the gain of the full master. Pure functions here build filter strings and
parse ffmpeg's output; ``run_plan`` runs the measurements.

The ``master=`` export parameter is parsed here (``parse_master_param``):
base64url JSON, size-capped, every field typed and range-checked, and only
numbers formatted by this module reach the ffmpeg graph.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import re
from collections import deque
from dataclasses import asdict, dataclass

from app.core.config import (
    EBUR128_FRAMELOG,
    EXPORT_LIMIT_LOSSY_DBTP,
    TIMEOUT_FFMPEG,
    ffmpeg_executable,
)

logger = logging.getLogger("stemdeck.mastering")

MASTER_WIRE_VERSION = 1
MASTER_PARAM_MAX_CHARS = 256

# Integrated loudness targets a user may ask for (LUFS). -24 is broadcast's
# quiet end, -6 is louder than any streaming service plays back.
TARGET_MIN, TARGET_MAX = -24.0, -6.0
# True-peak ceilings (dBTP). Lossy exports never go above LOSSY_CEILING_DBTP
# whatever the user asks: the encoder adds peak of its own (task B measured
# up to +0.7 dB for MP3 and up to 2.7 dB for AAC with intensity stereo).
CEILING_MIN, CEILING_MAX = -3.0, -0.1
LOSSY_CEILING_DBTP = EXPORT_LIMIT_LOSSY_DBTP
# Glue compressor amount: 0 off, 1 the strongest the MVP offers (3:1).
COMP_MIN, COMP_MAX = 0.0, 1.0

# The gain the master may apply. A near-silent mix is not raised 40 dB into
# the limiter: past +20 dB the target is reported as not reached instead.
GAIN_MAX_DB = 20.0
GAIN_MIN_DB = -30.0
# Correction passes when the limiter cost more than CORRECTION_TOL_LU: the
# first adds the miss to the gain, the second steps along the measured slope
# (dense material gains about 0.6 LU per dB once the limiter works: measured
# on three real songs, one pass left -9.24, -9.30 and -9.04 for -9). Each step
# is at most CORRECTION_MAX_DB: more gain into a limiter that is already
# working hard buys less and less loudness.
CORRECTION_TOL_LU = 0.1
CORRECTION_MAX_DB = 3.0
CORRECTION_PASSES = 2
# ebur128 reports -70 LUFS (its absolute gate) for silence.
_SILENCE_LUFS = -69.9

OVERSAMPLE = 4
# The limiter holds its 4x samples at the ceiling; the resample back to the
# stem rate and a meter's own interpolation read up to 0.07 dB higher
# (measured on three songs at -1 dBTP: -0.93 to -0.98). The limit sits this
# far under the ceiling so the measured true peak does not cross it.
LIMITER_MARGIN_DB = 0.1
LIMITER_ATTACK_MS = 5
LIMITER_RELEASE_MS = 50

# Glue compressor, as a function of the amount a (0..1): ratio 1 + 2a (2:1 at
# the default 0.5), threshold measured loudness + 2 - 6a dB (so loud passages,
# a few dB over the average, get 1 to 3 dB of gain reduction), RMS detection
# with a slow attack and release (bus "glue", not peak control: the limiter
# does that), 6 dB soft knee, no makeup (the loudness gain is the makeup).
COMP_ATTACK_MS = 20
COMP_RELEASE_MS = 250
COMP_KNEE_DB = 6.0
COMP_THRESHOLD_FLOOR_DB = -60.0  # acompressor's own range ends here

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_I_RE = re.compile(r"lavfi\.r128\.I=(\S+)")
_TP_RE = re.compile(r"lavfi\.r128\.true_peak=(\S+)")
_LRA_RE = re.compile(r"lavfi\.r128\.LRA=(\S+)")


class MasterError(ValueError):
    """A malformed or out-of-range ``master`` value. The message is safe to show."""


@dataclass(frozen=True)
class MasterSettings:
    """What the user asked for. ``target`` None: no loudness change, only the
    limiter ("prevent clipping")."""

    target: float | None
    ceiling: float
    comp: float = 0.0

    def fragment(self) -> str:
        t = "none" if self.target is None else f"{self.target:.1f}"
        return f"t={t};c={self.ceiling:.1f};k={self.comp:.2f}"


@dataclass(frozen=True)
class Loudness:
    i: float | None  # integrated LUFS, None for silence
    tp: float | None  # true peak dBTP, None for digital silence
    lra: float | None = None


@dataclass(frozen=True)
class MasterPlan:
    """The measured master of one mix: what the render applies and reports."""

    target: float | None
    ceiling: float
    comp: float
    pre: Loudness  # the mix before the master chain (after the master EQ)
    comp_threshold_db: float | None  # None: compressor off
    comp_i: float | None  # loudness after the compressor (None when off)
    gain_db: float
    post: Loudness  # the mastered result, measured before the encoder
    reached: bool  # within CORRECTION_TOL_LU of the target (True with no target)
    passes: int  # measurement passes run

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> MasterPlan:
        return cls(
            target=data["target"],
            ceiling=data["ceiling"],
            comp=data["comp"],
            pre=Loudness(**data["pre"]),
            comp_threshold_db=data["comp_threshold_db"],
            comp_i=data["comp_i"],
            gain_db=data["gain_db"],
            post=Loudness(**data["post"]),
            reached=data["reached"],
            passes=data["passes"],
        )


# ---------------------------------------------------------------------------
# The master= parameter
# ---------------------------------------------------------------------------


def _reject_constant(name: str) -> float:
    raise MasterError("master: numbers must be finite")


def _number(v: object, lo: float, hi: float, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise MasterError(f"master: {what} must be a number")
    x = float(v)
    if not math.isfinite(x) or x < lo or x > hi:
        raise MasterError(f"master: {what} out of range")
    return x


def parse_master_param(raw: str | None) -> MasterSettings | None:
    """Decode and validate ``master=``: base64url (no padding) of
    ``{"v": 1, "target": <LUFS> | null, "ceiling": <dBTP>, "comp": <0..1>}``.
    Empty means mastering off (None). Raises MasterError."""
    if not raw:
        return None
    if len(raw) > MASTER_PARAM_MAX_CHARS or not _B64URL_RE.match(raw):
        raise MasterError("master: malformed")
    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        body = json.loads(text, parse_constant=_reject_constant)
    except MasterError:
        raise
    except (binascii.Error, UnicodeDecodeError, ValueError, RecursionError):
        raise MasterError("master: malformed") from None
    if not isinstance(body, dict) or set(body) - {"v", "target", "ceiling", "comp"}:
        raise MasterError("master: malformed")
    if isinstance(body.get("v"), bool) or body.get("v") != MASTER_WIRE_VERSION:
        raise MasterError("master: unsupported version")
    if "target" not in body or "ceiling" not in body:
        raise MasterError("master: target and ceiling are required")
    target = body["target"]
    if target is not None:
        target = round(_number(target, TARGET_MIN, TARGET_MAX, "target"), 1)
    ceiling = round(_number(body["ceiling"], CEILING_MIN, CEILING_MAX, "ceiling"), 1)
    comp = round(_number(body.get("comp", 0), COMP_MIN, COMP_MAX, "compressor"), 2)
    return MasterSettings(target=target, ceiling=ceiling, comp=comp)


def encode_master_param(settings: MasterSettings) -> str:
    """The wire form (tests and scripts; the browser builds the same)."""
    body = {"v": MASTER_WIRE_VERSION, "target": settings.target, "ceiling": settings.ceiling}
    if settings.comp:
        body["comp"] = settings.comp
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def effective_ceiling(settings: MasterSettings, lossy: bool) -> float:
    """The ceiling the render uses: never above LOSSY_CEILING_DBTP for a lossy
    encoder."""
    return min(settings.ceiling, LOSSY_CEILING_DBTP) if lossy else settings.ceiling


# ---------------------------------------------------------------------------
# Filter strings
# ---------------------------------------------------------------------------


def _lin(db: float) -> str:
    return f"{10.0 ** (db / 20.0):.8f}"


def comp_threshold_db(comp: float, pre_i: float | None) -> float | None:
    """The compressor threshold for a measured loudness, None when off."""
    if comp <= 0:
        return None
    base = pre_i if pre_i is not None else -40.0
    return round(min(0.0, max(COMP_THRESHOLD_FLOOR_DB, base + 2.0 - 6.0 * comp)), 2)


def comp_ratio(comp: float) -> float:
    return round(1.0 + 2.0 * comp, 3)


def comp_filter(comp: float, threshold_db: float | None) -> str:
    """``acompressor`` for the glue compressor, "" when off."""
    if comp <= 0 or threshold_db is None:
        return ""
    knee = 10.0 ** (COMP_KNEE_DB / 20.0)
    return (
        f"acompressor=threshold={_lin(threshold_db)}:ratio={comp_ratio(comp)}"
        f":attack={COMP_ATTACK_MS}:release={COMP_RELEASE_MS}:knee={knee:.6f}"
        ":detection=rms:link=average:makeup=1"
    )


def limiter_filter(ceiling_db: float, rate: int) -> str:
    """True-peak limiter: alimiter at OVERSAMPLE x the rate, back to the rate."""
    return (
        f"aresample={OVERSAMPLE * rate},"
        f"alimiter=limit={_lin(ceiling_db - LIMITER_MARGIN_DB)}:attack={LIMITER_ATTACK_MS}"
        f":release={LIMITER_RELEASE_MS}:level=disabled:latency=1,"
        f"aresample={rate}"
    )


def master_chain(
    comp: float, threshold_db: float | None, gain_db: float, ceiling_db: float, rate: int
) -> str:
    """The whole master chain after the master EQ, without a trailing comma."""
    parts = [comp_filter(comp, threshold_db)]
    if gain_db != 0:
        parts.append(f"volume={_lin(gain_db)}")
    parts.append(limiter_filter(ceiling_db, rate))
    return ",".join(p for p in parts if p)


def plan_chain(plan: MasterPlan, rate: int) -> str:
    return master_chain(plan.comp, plan.comp_threshold_db, plan.gain_db, plan.ceiling, rate)


def meter_filter(rate: int) -> str:
    """Appended to a chain to measure it: one-second frames (so a long song
    prints a line a second, not 43), ebur128 with its cumulative values as
    frame metadata, printed. The last values cover the whole signal."""
    return (
        f"asetnsamples=n={rate}:p=0,"
        f"ebur128=peak=true:metadata=1:framelog={EBUR128_FRAMELOG},"
        "ametadata=mode=print:key=lavfi.r128.I,"
        "ametadata=mode=print:key=lavfi.r128.LRA,"
        "ametadata=mode=print:key=lavfi.r128.true_peak"
    )


def _last_float(regex: re.Pattern[str], text: str) -> float | None:
    found = regex.findall(text)
    if not found:
        return None
    try:
        value = float(found[-1])
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_meter(text: str) -> Loudness:
    """Loudness from ``meter_filter``'s printed metadata."""
    i = _last_float(_I_RE, text)
    if i is not None and i <= _SILENCE_LUFS:
        i = None
    tp_lin = _last_float(_TP_RE, text)
    tp = 20.0 * math.log10(tp_lin) if tp_lin and tp_lin > 0 else None
    lra = _last_float(_LRA_RE, text)
    return Loudness(
        i=None if i is None else round(i, 3),
        tp=None if tp is None else round(tp, 3),
        lra=None if lra is None else round(lra, 3),
    )


def plan_graph(lane_chains: list[str], master_eq: str) -> str:
    """The filter graph of the whole song up to the master chain, ending in
    the label [pre]: input i through ``lane_chains[i]`` (its EQ, pitch shift
    and gain, no trim, no delay), summed, then the master EQ (``master_eq`` is
    "" or a prefix ending in a comma). No click: the click is a practice aid,
    not part of the music the loudness is set for."""
    filters = [f"[{i}:a]{chain}[a{i}]" for i, chain in enumerate(lane_chains)]
    n = len(lane_chains)
    if n > 1:
        labels = "".join(f"[a{i}]" for i in range(n))
        filters.append(f"{labels}amix=inputs={n}:normalize=0,{master_eq}anull[pre]")
    else:
        filters.append(f"[a0]{master_eq}anull[pre]")
    return ";".join(filters)


def clamp_gain(gain_db: float) -> float:
    return round(min(GAIN_MAX_DB, max(GAIN_MIN_DB, gain_db)), 4)


# ---------------------------------------------------------------------------
# Running the plan
# ---------------------------------------------------------------------------


class MeasureError(RuntimeError):
    """ffmpeg failed while measuring. The message is for the log only."""


async def _measure(inputs: list[str], graph: str, chain: str, rate: int) -> Loudness:
    """Run ``graph`` (whose output label is [pre]) through ``chain`` and the
    meter, to a null output. ``inputs`` are ffmpeg's -i arguments."""
    tail = f"{chain}," if chain else ""
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "info",
        *inputs,
        "-filter_complex",
        f"{graph};[pre]{tail}{meter_filter(rate)}[out]",
        "-map",
        "[out]",
        "-f",
        "null",
        "-",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_FFMPEG)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        await proc.wait()
        raise MeasureError("loudness measurement timed out") from None
    finally:
        # A cancelled request (the client went away) unwinds through here.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    text = (err or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        tail_lines = deque(text.splitlines(), maxlen=8)
        raise MeasureError(f"ffmpeg exit {proc.returncode}: {' | '.join(tail_lines)}")
    return parse_meter(text)


async def measure_file(path: str) -> Loudness:
    """Loudness and true peak of a rendered file, decoded (a lossy file is
    measured as a player would play it)."""
    return await _measure(["-i", path], "[0:a]anull[pre]", "", 44100)


def trim_filter(trim_db: float) -> str:
    """A level trim after the limiter (the lossy retry), "" for none."""
    return f",volume={_lin(-trim_db)}" if trim_db > 0 else ""


async def run_plan(
    inputs: list[str], graph: str, settings: MasterSettings, ceiling_db: float, rate: int
) -> MasterPlan:
    """Measure the mix and decide the gain. ``graph`` is the filter graph of
    the whole song up to the master chain, ending in the label [pre]."""
    pre = await _measure(inputs, graph, "", rate)
    passes = 1
    threshold = comp_threshold_db(settings.comp, pre.i)
    comp_i = None
    level = pre.i
    if threshold is not None:
        after = await _measure(inputs, graph, comp_filter(settings.comp, threshold), rate)
        passes += 1
        comp_i = after.i
        level = after.i
    # Prevent clipping only, or silence: no gain, the limiter alone.
    no_gain = settings.target is None or level is None
    gain = 0.0 if no_gain else clamp_gain(settings.target - level)
    chain = master_chain(settings.comp, threshold, gain, ceiling_db, rate)
    post = await _measure(inputs, graph, chain, rate)
    passes += 1
    tried = [(gain, post.i)]
    for _ in range(0 if no_gain else CORRECTION_PASSES):
        if post.i is None:
            break
        miss = settings.target - post.i
        if abs(miss) <= CORRECTION_TOL_LU:
            break
        slope = 1.0
        if len(tried) > 1:
            (g0, l0), (g1, l1) = tried[-2], tried[-1]
            if g1 != g0 and l0 is not None and l1 is not None:
                # Between 0.1 (a limiter at its wall) and 1 (no limiting).
                slope = min(1.0, max(0.1, (l1 - l0) / (g1 - g0)))
        step = max(-CORRECTION_MAX_DB, min(CORRECTION_MAX_DB, miss / slope))
        fixed = clamp_gain(gain + step)
        if fixed == gain:
            break
        gain = fixed
        chain = master_chain(settings.comp, threshold, gain, ceiling_db, rate)
        post = await _measure(inputs, graph, chain, rate)
        passes += 1
        tried.append((gain, post.i))
    reached = settings.target is None or (
        post.i is not None and abs(settings.target - post.i) <= CORRECTION_TOL_LU + 1e-9
    )
    return MasterPlan(
        target=settings.target,
        ceiling=ceiling_db,
        comp=settings.comp,
        pre=pre,
        comp_threshold_db=threshold,
        comp_i=comp_i,
        gain_db=gain,
        post=post,
        reached=reached,
        passes=passes,
    )
