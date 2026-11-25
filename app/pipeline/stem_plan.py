"""How a separation plan becomes six stems, without torch.

A plan names the model each selectable stem is separated with
(app/core/stem_models.py). The worker runs one pass per distinct model,
stem_models.passes() in order, and hands the outputs here: each stem is taken
from the model the plan assigned it, and "other" is the residual, the mix
minus the five kept stems, so the six stems always add up to the song. The
same assembly with a plan that is the base model alone is what every song
gets that was separated without a plan.

Everything stays in floating point between the passes. At the final write one
scale is shared by every stem, so that none clips and they still add up:
s = max(1, the largest peak of any stem / the clamp's ceiling, plus a hair),
each stem divided by s, so the clamp at the write never touches a sample.

Pure and array-agnostic (numpy arrays and torch tensors both work: only
+, -, /, abs() and .max() are used), so it is unit-tested without a model.
The progress phases the parent maps one progress bar onto live here too.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from app.core import stem_models
from app.core.config import DEMUCS_MODEL
from app.core.stem_models import SELECTABLE_STEMS

# The extra work "best" quality adds to a Demucs pass (shifts 2), measured on
# the maintainer's machines: 1.62 times the time of "standard".
BEST_QUALITY_FACTOR = 1.62

# demucs.audio.prevent_clip(wav, "clamp") clamps to +-0.99: the scale keeps
# every sample under it, with this relative margin for the float rounding of
# the division, so the clamp changes nothing and the stems still add up.
CLAMP_CEILING = 0.99
CLAMP_MARGIN = 1e-4


def base_plan(base: str = DEMUCS_MODEL) -> dict[str, str]:
    """Every selectable stem from the base model: what a request without a
    plan means, and what a song separated before plans existed was made with."""
    return {stem: base for stem in SELECTABLE_STEMS}


def check_plan(
    plan: object,
    base: str = DEMUCS_MODEL,
    *,
    user_models: Mapping[str, Collection[str]] | None = None,
    allow_gone: bool = False,
) -> dict[str, str]:
    """``plan`` as a plan this install can run, or ValueError with a fixed
    message. None is the base plan. Every selectable stem must be named, to the
    base model or to one of the choices for that stem; a key that is not a
    selectable stem is refused.

    A model the person added is checked one of two ways. In the worker,
    ``user_models`` is what its request line carries, id -> the stems that
    model declares, and the worker never reads the store: an id is accepted
    for a stem it declares there. In the server (``user_models`` None) it must
    be one of stem_models.choices_for(stem), usable now; with ``allow_gone``
    any id of that shape is accepted, for a caller that says itself why one
    cannot run (stem_fill.recorded_plan)."""
    if plan is None:
        return base_plan(base)
    if not isinstance(plan, Mapping):
        raise ValueError("the separation plan must be an object of stem to model")
    if any(key not in SELECTABLE_STEMS for key in plan):
        raise ValueError("the separation plan names a stem that cannot be chosen")
    out: dict[str, str] = {}
    for stem in SELECTABLE_STEMS:
        model_id = plan.get(stem)
        if not isinstance(model_id, str) or not (
            model_id == base
            or model_id in stem_models.CHOICES[stem]
            or _user_model_ok(model_id, stem, user_models, allow_gone)
        ):
            raise ValueError(f"the separation plan has no usable model for {stem}")
        out[stem] = model_id
    return out


def _user_model_ok(
    model_id: str,
    stem: str,
    user_models: Mapping[str, Collection[str]] | None,
    allow_gone: bool,
) -> bool:
    if not stem_models.is_user_model(model_id):
        return False
    if user_models is not None:
        declared = user_models.get(model_id)
        return declared is not None and stem in declared
    return allow_gone or model_id in stem_models.choices_for(stem)


def assemble(
    mix: Any, plan: Mapping[str, str], outputs: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """The stems to write: each selectable stem from the model ``plan``
    assigned it (``outputs`` is model id -> {stem: array} from that model's
    pass), then "other" = ``mix`` minus every stem kept.

    A stem its model does not produce (a deployment's four-stem base model has
    no guitar or piano) is not written, and what it would have held stays in
    "other"."""
    kept: dict[str, Any] = {}
    for stem in SELECTABLE_STEMS:
        produced = outputs.get(plan.get(stem, ""), {})
        if stem in produced:
            kept[stem] = produced[stem]
    other = mix
    for stem_audio in kept.values():
        other = other - stem_audio
    return {**kept, "other": other}


def shared_scale(stems: Mapping[str, Any]) -> float:
    """The one divisor every stem is written with: 1 when every sample is under
    the clamp's ceiling, else the largest peak over that ceiling (with
    CLAMP_MARGIN), so no stem clips and the stems still add up after the
    write. ValueError when a stem holds a sample that is not a number, which
    no write could make right. Reads each stem's max and min, so no copy of
    a stem is made."""
    peak = 0.0
    for audio in stems.values():
        count = audio.numel() if hasattr(audio, "numel") else audio.size
        if count == 0:
            continue
        high, low = float(audio.max()), float(audio.min())
        if not (math.isfinite(high) and math.isfinite(low)):
            raise ValueError("a separation pass produced samples that are not numbers")
        peak = max(peak, high, -low)
    return max(1.0, peak * (1.0 + CLAMP_MARGIN) / CLAMP_CEILING)


@dataclass(frozen=True)
class Phase:
    """One pass as the progress bar shows it: its model, its stage words, and
    the share of the bar it starts at and spans (0..1)."""

    model_id: str
    label: str
    start: float
    span: float


def _label(stems: tuple[str, ...]) -> str:
    if stems == ("vocals",):
        return "Separating vocals"
    if set(stems) == {"drums", "bass"}:
        return "Separating drums and bass"
    if len(stems) == 1:
        return f"Separating {stems[0]}"
    return "Separating instruments"


RtfOf = Callable[[str, str], float | None]


def _weight(model_id: str, device: str, best: bool, rtf_of: RtfOf | None = None) -> float:
    """A pass's expected seconds per second of audio, from the registry's
    measured real-time factors. A model outside the registry counts as
    ``rtf_of(model_id, device)`` when that knows it (a model the person
    added: its dry run's measure), else as 1."""
    model = stem_models.MODELS.get(model_id)
    if model is None:
        rtf = rtf_of(model_id, device) if rtf_of is not None else None
        return max(rtf, 1e-6) if rtf is not None and rtf > 0 else 1.0
    rtf = model.gpu_rtf if device != "cpu" and model.gpu_rtf is not None else model.cpu_rtf
    if best and model.engine == "demucs":
        rtf *= BEST_QUALITY_FACTOR
    return max(rtf, 1e-6)


def phases(
    plan: Mapping[str, str],
    device: str,
    quality: str | None,
    rtf_of: RtfOf | None = None,
) -> list[Phase]:
    """The passes of ``plan`` mapped onto one progress bar, in the order the
    worker runs them, each sized by its expected time on ``device``. A single
    pass reads "Separating", as before plans existed. ``rtf_of`` sizes a pass
    whose model is outside the registry (the parent passes
    stem_models.user_model_rtf)."""
    steps = stem_models.passes(plan)
    if not steps:
        return [Phase(DEMUCS_MODEL, "Separating", 0.0, 1.0)]
    best = quality == "best"
    weights = [_weight(model_id, device, best, rtf_of) for model_id, _, _ in steps]
    total = sum(weights)
    out: list[Phase] = []
    start = 0.0
    for (model_id, _, stems), weight in zip(steps, weights, strict=True):
        span = weight / total
        label = "Separating" if len(steps) == 1 else _label(stems)
        out.append(Phase(model_id, label, start, span))
        start += span
    return out


def describe(plan: Mapping[str, str] | None) -> str:
    """The plan in one line for logs and error.txt: "vocals=kim_mel_roformer
    drums=htdemucs_6s ...", or "(none)"."""
    if not plan:
        return "(none)"
    return " ".join(f"{stem}={plan.get(stem)}" for stem in SELECTABLE_STEMS if stem in plan)


def describe_fallbacks(fallbacks: Mapping[str, Mapping[str, object]] | None) -> str:
    """The stand-ins of a job in one line for logs and error.txt:
    "vocals=user_0123456789ab(load_failed) ...", or "(none)"."""
    if not fallbacks:
        return "(none)"
    return " ".join(
        f"{stem}={entry.get('model')}({entry.get('code')})"
        for stem in SELECTABLE_STEMS
        if isinstance(entry := fallbacks.get(stem), Mapping)
    )
