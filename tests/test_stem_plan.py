"""stem_plan: which stem comes from which pass, the residual "other", the one
shared scale, and the progress phases. Pure: numpy arrays stand in for the
worker's tensors, and no model runs."""

from __future__ import annotations

import numpy as np
import pytest

from app.core import stem_models
from app.pipeline import stem_plan

SIX = "htdemucs_6s"
FT = "htdemucs_ft"
KIM = "kim_mel_roformer"

rng = np.random.default_rng(804)
MIX = rng.uniform(-0.6, 0.6, size=(2, 512))


def _plan(**over: str) -> dict[str, str]:
    return {**{stem: SIX for stem in stem_models.SELECTABLE_STEMS}, **over}


def _demucs_out(audio: np.ndarray, sources: tuple[str, ...], seed: int) -> dict[str, np.ndarray]:
    """A stand-in Demucs pass: a distinct share of ``audio`` per source."""
    shares = np.random.default_rng(seed).uniform(0.05, 0.3, size=len(sources))
    return {name: audio * share for name, share in zip(sources, shares, strict=True)}


SIX_SOURCES = ("drums", "bass", "other", "vocals", "guitar", "piano")
FT_SOURCES = ("drums", "bass", "other", "vocals")


def test_kim_and_6s_take_vocals_from_kim_and_the_rest_from_6s_on_the_rest():
    plan = _plan(vocals=KIM)
    assert stem_models.passes(plan) == [
        (KIM, "mix", ("vocals",)),
        (SIX, "rest", ("drums", "bass", "guitar", "piano")),
    ]
    vocals = MIX * 0.4
    six = _demucs_out(MIX - vocals, SIX_SOURCES, 1)
    stems = stem_plan.assemble(MIX, plan, {KIM: {"vocals": vocals}, SIX: six})
    assert list(stems) == ["vocals", "drums", "bass", "guitar", "piano", "other"]
    assert stems["vocals"] is vocals  # never Demucs' own vocals
    for name in ("drums", "bass", "guitar", "piano"):
        assert stems[name] is six[name]
    np.testing.assert_allclose(sum(stems.values()), MIX, atol=1e-6)


def test_kim_ft_bass_and_6s_drums_each_come_from_their_model():
    plan = _plan(vocals=KIM, bass=FT)
    assert stem_models.passes(plan) == [
        (KIM, "mix", ("vocals",)),
        (SIX, "rest", ("drums", "guitar", "piano")),
        (FT, "rest", ("bass",)),
    ]
    vocals = MIX * 0.4
    rest = MIX - vocals
    six = _demucs_out(rest, SIX_SOURCES, 2)
    ft = _demucs_out(rest, FT_SOURCES, 3)
    stems = stem_plan.assemble(MIX, plan, {KIM: {"vocals": vocals}, SIX: six, FT: ft})
    assert stems["vocals"] is vocals
    assert stems["drums"] is six["drums"]
    assert stems["bass"] is ft["bass"]
    assert stems["guitar"] is six["guitar"]
    assert stems["piano"] is six["piano"]
    np.testing.assert_allclose(sum(stems.values()), MIX, atol=1e-6)


def test_all_6s_is_one_pass_and_other_is_the_residual_not_demucs_own():
    plan = _plan()
    assert stem_models.passes(plan) == [
        (SIX, "mix", ("vocals", "drums", "bass", "guitar", "piano"))
    ]
    six = _demucs_out(MIX, SIX_SOURCES, 4)
    stems = stem_plan.assemble(MIX, plan, {SIX: six})
    expected_other = MIX - sum(six[n] for n in ("vocals", "drums", "bass", "guitar", "piano"))
    np.testing.assert_allclose(stems["other"], expected_other, atol=1e-12)
    assert not np.allclose(stems["other"], six["other"])


@pytest.mark.parametrize(
    "plan",
    [_plan(), _plan(vocals=KIM), _plan(vocals=KIM, drums=FT, bass=FT), _plan(drums=FT)],
)
def test_the_six_stems_add_up_to_the_mix_exactly(plan):
    outputs: dict[str, dict[str, np.ndarray]] = {}
    rest = None
    for model_id, source, _ in stem_models.passes(plan):
        if model_id == KIM:
            outputs[KIM] = {"vocals": MIX * 0.45}
            rest = MIX - outputs[KIM]["vocals"]
            continue
        audio = rest if source == "rest" else MIX
        sources = SIX_SOURCES if model_id == SIX else FT_SOURCES
        outputs[model_id] = _demucs_out(audio, sources, 6 if model_id == SIX else 7)
    stems = stem_plan.assemble(MIX, plan, outputs)
    assert set(stems) == {"vocals", "drums", "bass", "guitar", "piano", "other"}
    assert np.max(np.abs(sum(stems.values()) - MIX)) < 1e-6


def test_a_stem_its_model_does_not_make_is_left_in_other():
    plan = {stem: "htdemucs" for stem in stem_models.SELECTABLE_STEMS}
    four = _demucs_out(MIX, FT_SOURCES, 5)
    stems = stem_plan.assemble(MIX, plan, {"htdemucs": four})
    assert list(stems) == ["vocals", "drums", "bass", "other"]
    np.testing.assert_allclose(sum(stems.values()), MIX, atol=1e-12)


def _over_ceiling(peak: float) -> float:
    return peak * (1 + stem_plan.CLAMP_MARGIN) / stem_plan.CLAMP_CEILING


def test_one_scale_is_shared_by_every_stem():
    quiet = np.full((2, 4), 0.1)
    loud = np.array([[0.2, -1.6, 0.0, 0.3], [0.0, 0.1, 0.2, 0.3]])
    s = stem_plan.shared_scale({"vocals": quiet, "drums": loud, "other": quiet})
    assert s == pytest.approx(_over_ceiling(1.6))
    # Under the clamp's ceiling nothing is touched.
    assert stem_plan.shared_scale({"vocals": quiet, "other": quiet * 9}) == 1.0
    assert stem_plan.shared_scale({"vocals": np.full((2, 2), 0.98)}) == 1.0
    # At the ceiling itself the scale already applies.
    assert stem_plan.shared_scale({"vocals": np.full((2, 2), 0.99)}) > 1.0


@pytest.mark.parametrize("peak", [0.9899, 0.99, 0.990099, 1.0, 1.6, 37.0])
def test_after_the_scale_the_real_clamp_changes_nothing(peak):
    """demucs' own prevent_clip(..., "clamp") is the write's last step: with
    the scale taken from its ceiling it must not cut a single sample, or the
    stems would no longer add up."""
    torch = pytest.importorskip("torch")
    audio_mod = pytest.importorskip("demucs.audio")
    rng_t = torch.Generator().manual_seed(7)
    stems = {
        "vocals": torch.rand(2, 4096, generator=rng_t) * 2 - 1,
        "drums": torch.rand(2, 4096, generator=rng_t) * 2 - 1,
    }
    stems["drums"][1, 100] = -peak
    stems["vocals"] = stems["vocals"] * min(1.0, peak)
    s = stem_plan.shared_scale(stems)
    for audio in stems.values():
        scaled = audio / s
        torch.testing.assert_close(audio_mod.prevent_clip(scaled, "clamp"), scaled, atol=0, rtol=0)


def test_a_stem_that_is_not_a_number_is_refused():
    bad = np.array([[0.1, np.nan]])
    with pytest.raises(ValueError, match="not numbers"):
        stem_plan.shared_scale({"vocals": np.zeros((1, 2)), "drums": bad})
    with pytest.raises(ValueError, match="not numbers"):
        stem_plan.shared_scale({"drums": np.array([[np.inf]])})
    with pytest.raises(ValueError, match="not numbers"):
        stem_plan.shared_scale({"drums": np.array([[0.5, -np.inf]])})


def test_an_empty_stem_does_not_break_the_scale():
    assert stem_plan.shared_scale(
        {"vocals": np.zeros((2, 0)), "drums": np.full((2, 1), 2.0)}
    ) == pytest.approx(_over_ceiling(2.0))


def test_the_shared_scale_works_on_torch_tensors():
    torch = pytest.importorskip("torch")
    got = stem_plan.shared_scale({"vocals": torch.tensor([[0.5, -2.0]])})
    assert got == pytest.approx(_over_ceiling(2.0))
    with pytest.raises(ValueError, match="not numbers"):
        stem_plan.shared_scale({"vocals": torch.tensor([[0.5, float("nan")]])})


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        ("not a dict", "object of stem to model"),
        ({**_plan(), "kazoo": SIX}, "cannot be chosen"),
        ({k: v for k, v in _plan().items() if k != "piano"}, "for piano"),
        (_plan(guitar=FT), "for guitar"),
        (_plan(vocals="a_model_from_the_future"), "for vocals"),
        (_plan(drums=7), "for drums"),
    ],
)
def test_a_plan_that_cannot_run_is_refused(plan, message):
    with pytest.raises(ValueError, match=message):
        stem_plan.check_plan(plan, base=SIX)


def test_no_plan_is_the_base_model_for_every_stem():
    assert stem_plan.check_plan(None, base=SIX) == _plan()
    # A deployment's own base model may supply every stem.
    assert stem_plan.check_plan({s: "htdemucs" for s in _plan()}, base="htdemucs") == {
        s: "htdemucs" for s in _plan()
    }
    assert stem_plan.check_plan(_plan(vocals=KIM, bass=FT), base=SIX) == _plan(vocals=KIM, bass=FT)


def test_one_pass_reads_separating_as_before():
    assert stem_plan.phases(_plan(), "cuda", "standard") == [
        stem_plan.Phase(SIX, "Separating", 0.0, 1.0)
    ]


def test_passes_share_the_bar_by_their_measured_speed():
    phases = stem_plan.phases(_plan(vocals=KIM, drums=FT, bass=FT), "cuda", "standard")
    assert [(p.model_id, p.label) for p in phases] == [
        (KIM, "Separating vocals"),
        (FT, "Separating drums and bass"),
        (SIX, "Separating instruments"),
    ]
    kim, ft, six = (stem_models.MODELS[m].gpu_rtf for m in (KIM, FT, SIX))
    total = kim + ft + six
    assert phases[0].start == 0.0
    assert phases[0].span == pytest.approx(kim / total)
    assert phases[1].start == pytest.approx(kim / total)
    assert phases[2].start + phases[2].span == pytest.approx(1.0)


def test_best_quality_slows_the_demucs_passes_only_and_the_cpu_uses_cpu_speeds():
    phases = stem_plan.phases(_plan(vocals=KIM), "cpu", "best")
    kim = stem_models.MODELS[KIM].cpu_rtf
    six = stem_models.MODELS[SIX].cpu_rtf * stem_plan.BEST_QUALITY_FACTOR
    assert phases[0].span == pytest.approx(kim / (kim + six))


def test_a_single_stem_pass_is_named_for_its_stem():
    phases = stem_plan.phases(_plan(drums=FT), "cpu", "standard")
    assert [p.label for p in phases] == ["Separating instruments", "Separating drums"]


def test_the_plan_reads_as_one_line():
    assert stem_plan.describe(None) == "(none)"
    assert stem_plan.describe(_plan(vocals=KIM)) == (
        "vocals=kim_mel_roformer drums=htdemucs_6s bass=htdemucs_6s"
        " guitar=htdemucs_6s piano=htdemucs_6s"
    )


# ── models the person added (My models) ──

USER_A = "user_0123456789ab"
USER_B = "user_ba9876543210"


def test_the_worker_accepts_a_user_model_its_request_describes_for_a_declared_stem():
    plan = _plan(drums=USER_A, bass=USER_A)
    described = {USER_A: ("drums", "bass")}
    assert stem_plan.check_plan(plan, base=SIX, user_models=described) == plan
    # Not described, or described without that stem: refused.
    with pytest.raises(ValueError, match="no usable model for drums"):
        stem_plan.check_plan(plan, base=SIX, user_models={})
    with pytest.raises(ValueError, match="no usable model for bass"):
        stem_plan.check_plan(plan, base=SIX, user_models={USER_A: ("drums",)})


def test_the_server_accepts_a_user_model_only_while_it_is_a_choice(monkeypatch):
    monkeypatch.setattr(
        stem_models,
        "choices_for",
        lambda stem: (*stem_models.CHOICES[stem], USER_A) if stem == "drums" else (),
    )
    assert stem_plan.check_plan(_plan(drums=USER_A))["drums"] == USER_A
    with pytest.raises(ValueError, match="no usable model for bass"):
        stem_plan.check_plan(_plan(bass=USER_A))
    # A caller that classifies itself (a fill) may let a gone one through.
    assert stem_plan.check_plan(_plan(bass=USER_B), allow_gone=True)["bass"] == USER_B
    # Shape still counts: an id that is not a user model's is refused.
    with pytest.raises(ValueError):
        stem_plan.check_plan(_plan(bass="user_XYZ"), allow_gone=True)


def test_other_is_the_residual_whichever_models_made_the_stems():
    """E-sum: the six stems add up to the mix for every mix of engines."""
    plans = [
        _plan(vocals=KIM),
        _plan(drums=USER_A, bass=USER_A),
        _plan(drums=USER_A, bass=FT),
        _plan(drums=USER_A, bass=USER_B, vocals=KIM),
    ]
    for plan in plans:
        outputs: dict[str, dict] = {}
        for i, (model_id, _src, stems) in enumerate(stem_models.passes(plan)):
            outputs[model_id] = {s: MIX * (0.05 + 0.03 * i) for s in stems}
        stems = stem_plan.assemble(MIX, plan, outputs)
        assert set(stems) == {*stem_models.SELECTABLE_STEMS, "other"}
        np.testing.assert_allclose(sum(stems.values()), MIX, atol=1e-12)


def test_a_user_pass_is_sized_by_its_measured_speed():
    plan = _plan(drums=USER_A)
    phases = stem_plan.phases(plan, "cpu", "standard", lambda m, d: 4.0 if m == USER_A else None)
    assert [p.model_id for p in phases] == [USER_A, SIX]
    assert phases[0].label == "Separating drums"
    six = stem_models.MODELS[SIX].cpu_rtf
    assert phases[0].span == pytest.approx(4.0 / (4.0 + six))
    # Without a measure it weighs 1, as any model outside the registry.
    unknown = stem_plan.phases(plan, "cpu", "standard")
    assert unknown[0].span == pytest.approx(1.0 / (1.0 + six))
    # Best quality slows Demucs only.
    best = stem_plan.phases(plan, "cpu", "best", lambda m, d: 4.0)
    assert best[0].span == pytest.approx(4.0 / (4.0 + six * stem_plan.BEST_QUALITY_FACTOR))


def test_the_stand_ins_read_as_one_line():
    assert stem_plan.describe_fallbacks(None) == "(none)"
    assert stem_plan.describe_fallbacks({}) == "(none)"
    fallbacks = {
        "bass": {"model": USER_A, "name": "Mine", "code": "key_mismatch"},
        "drums": {"model": USER_A, "name": "Mine", "code": "key_mismatch"},
    }
    assert stem_plan.describe_fallbacks(fallbacks) == (
        f"drums={USER_A}(key_mismatch) bass={USER_A}(key_mismatch)"
    )
