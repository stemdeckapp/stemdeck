"""A Suggest EQ answer, assembled from a measured spectrum, a target and the
lane's current EQ. Pure: the API (app/api/suggest.py) measures, this decides
and explains. Everything returned is numbers and i18n keys; the browser turns
them into text and into an ordinary EQ descriptor.

Modes:
- "replace": the suggestion is fitted to the lane as it sounds with no EQ,
  and replaces its EQ.
- "merge" (graphical panels and the master): fitted to the lane as it sounds
  now, through its current EQ; the new bands are added to the current ones
  (up to the engine's 12) and the output gain is adjusted by what they
  change.

Loudness-neutral: the output gain cancels the estimated K-weighted loudness
change of the new bands on the measured, gated spectrum (eq_fit.py), so
Preview compares like with like. ``loudness`` reports what is left of it.
"""

from __future__ import annotations

import numpy as np

from app.pipeline.eq_analysis import NOMINAL_HZ, Spectrum, bands_db, loudness_of_power
from app.pipeline.eq_fit import (
    LAYOUTS,
    apply_eq,
    band_reasons,
    fit_eq,
    loudness_change_db,
    neutral_out_db,
    outside_rms_db,
    weighted_rms_db,
    weights_for,
)
from app.pipeline.eq_targets import Target
from app.pipeline.fx_filters import MAX_BANDS, OUT_MAX, OUT_MIN, Eq

# A lane must play this long (loud frames) and this loud to be measured.
MIN_GATED_SECONDS = 3.0
MIN_LANE_LUFS = -50.0


def _round_list(xs, nd: int = 2) -> list[float]:
    return [round(float(x), nd) for x in xs]


def quiet_answer(spec: Spectrum, target: Target, choices: list[dict]) -> dict:
    return {
        "status": "quiet",
        "target": target.id,
        "choices": choices,
        "analysis": {
            "seconds": round(spec.seconds, 1),
            "gated_seconds": round(spec.gated_seconds, 1),
        },
        "why": [{"key": "suggest.why.quiet", "vars": {}}],
    }


def is_quiet(spec: Spectrum) -> bool:
    return (
        spec.silent
        or spec.gated_seconds < MIN_GATED_SECONDS
        or loudness_of_power(spec.power) < MIN_LANE_LUFS
    )


def make_suggestion(
    spec: Spectrum,
    target: Target,
    layout: str,
    mode: str,
    current: Eq | None,
    max_bands: int,
    choices: list[dict],
) -> dict:
    if layout not in LAYOUTS:
        raise ValueError("unknown layout")
    if is_quiet(spec):
        return quiet_answer(spec, target, choices)
    power0 = spec.power
    cur_bands = list(current.bands) if current else []
    cur_out = current.out if current else 0.0
    power_cur = apply_eq(power0, cur_bands, cur_out)
    merge = mode == "merge"
    base = power_cur if merge else power0
    measured = bands_db(base)
    room = MAX_BANDS - len(cur_bands) if merge else max_bands
    # Where a boost may go is decided on the lane as it is, before any EQ.
    fit = fit_eq(
        measured, target.bands_db, layout, max(0, min(max_bands, room)), boost_ref=bands_db(power0)
    )
    new_bands = [fb.band for fb in fit.bands]

    if merge:
        out = neutral_out_db(base, new_bands, cur_out)
        final_bands = cur_bands + new_bands
    else:
        out = neutral_out_db(power0, new_bands, 0.0)
        final_bands = new_bands
    out = round(max(OUT_MIN, min(OUT_MAX, out)), 1)

    raw_db = bands_db(power0)
    cur_db = bands_db(power_cur)
    after_db = bands_db(apply_eq(power0, final_bands, out))
    tgt = np.asarray(target.bands_db, dtype=np.float64)
    w = weights_for(raw_db)
    # The target drawn level with the measured lane (only shapes compare).
    offset = float(w @ (raw_db - tgt)) / float(w.sum()) if w.sum() > 0 else 0.0

    def dist(levels: np.ndarray) -> dict:
        return {
            "rms": round(weighted_rms_db(tgt - levels, w), 2),
            "outside": round(outside_rms_db(tgt - levels, w), 2),
        }

    new_change = loudness_change_db(base, new_bands, 0.0)
    why = band_reasons(fit)
    if new_bands:
        why.append(
            {
                "key": "suggest.why.out",
                "vars": {
                    "d": round(new_change, 1),
                    "out": round(out - (cur_out if merge else 0), 1),
                },
            }
        )
    else:
        why.append({"key": "suggest.why.close", "vars": {}})
    before = dist(cur_db if merge else raw_db)
    after = dist(after_db)
    why.append(
        {"key": "suggest.why.distance", "vars": {"before": before["rms"], "after": after["rms"]}}
    )

    return {
        "status": "ok",
        "target": target.id,
        "mode": mode,
        "layout": layout,
        "choices": choices,
        "freqs": list(NOMINAL_HZ),
        "levels": {
            "raw": _round_list(raw_db),
            "current": _round_list(cur_db),
            "after": _round_list(after_db),
            "target": _round_list(tgt + offset),
        },
        "distance": {"raw": dist(raw_db), "current": dist(cur_db), "suggested": after},
        "loudness": {
            # Estimated K-weighted loudness change against the lane with no EQ.
            "current_change": round(loudness_change_db(power0, cur_bands, cur_out), 2),
            "suggested_change": round(loudness_change_db(power0, final_bands, out), 2),
            "bands_change": round(new_change, 2),
        },
        "bands": [fb.to_json() for fb in fit.bands],
        "out": out,
        "eq": {
            "out": out,
            "bands": [
                {"type": b.type, "f": b.f, "g": b.g, "q": b.q, "slope": b.slope}
                for b in final_bands
            ],
        },
        "why": why,
        "analysis": {
            "seconds": round(spec.seconds, 1),
            "gated_seconds": round(spec.gated_seconds, 1),
        },
    }
