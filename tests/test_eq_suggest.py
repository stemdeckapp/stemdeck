"""Suggest EQ, the measurement and the fit, on signals whose spectrum is known.

- The gated third-octave analysis: pink noise reads flat, silence and quiet
  passages are gated out, loudness agrees with pyloudnorm.
- The fit: noise already shaped like a target gets nothing; a +6 dB bump at
  250 Hz comes back as a cut there; a dark signal gets a high boost; the fit
  always lowers the distance to the target; it is idempotent; it never leaves
  its bounds (band count, gain, Q, the combined curve, the panels' knobs).
- Loudness-neutral: the suggested EQ rendered by ffmpeg (the export's own
  filters) changes the measured integrated loudness by under 0.5 LU.
- The master preset never asks the limiter for more than the allowance.
- The targets file is complete and consistent.
"""

from __future__ import annotations

import subprocess

import numpy as np
import pyloudnorm as pyln
import pytest
import soundfile as sf

from app.core.config import ffmpeg_executable
from app.pipeline import eq_analysis as A
from app.pipeline import eq_fit as F
from app.pipeline.eq_suggest import make_suggestion
from app.pipeline.eq_targets import default_target, get_target, load_targets, uses_for_lane
from app.pipeline.fx_filters import Band, Eq, eq_filter
from tests.ffmpeg_probe import skip_without_ffmpeg

SR = A.ANALYSIS_RATE
FC = np.asarray(A.CENTRES_HZ)
FLAT = [0.0] * A.N_BANDS
BALANCED = get_target("mix.balanced").bands_db


def shaped_noise(levels_db, seconds=12.0, extra=(), seed=1) -> np.ndarray:
    """Stereo noise whose third-octave levels follow ``levels_db`` (pink =
    flat), through the bands ``extra``. RMS about -20 dBFS."""
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    f = np.fft.rfftfreq(n, 1 / SR)
    f[0] = 1.0
    level = np.interp(np.log2(f), np.log2(FC), np.asarray(levels_db, dtype=float))
    mag = np.sqrt(10 ** (level / 10) / f)
    if extra:
        mag = mag * np.sqrt(F.eq_mag2(list(extra), f))
    chans = [np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * mag, n) for _ in range(2)]
    x = np.stack(chans, 1)
    return (0.1 * x / np.std(x)).astype(np.float32)


def fit_of(x, target, layout="free", tolerance=1.0, max_bands=F.MAX_BANDS_LANE):
    s = A.analyse(x)
    fit = F.fit_eq(s.bands, target, layout, max_bands, tolerance=tolerance)
    bands = [fb.band for fb in fit.bands]
    out = F.neutral_out_db(s.power, bands)
    w = F.weights_for(s.bands)
    before = F.weighted_rms_db(np.asarray(target) - s.bands, w)
    after = F.weighted_rms_db(np.asarray(target) - F.band_levels_after(s.power, bands, out), w)
    return s, fit, bands, out, before, after


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------


def test_pink_noise_reads_flat_in_third_octaves():
    s = A.analyse(shaped_noise(FLAT))
    b = s.bands - s.bands[17]
    # 100 Hz to 12.5 kHz: within 0.3 dB. The lowest bands hold one or two
    # FFT bins of random noise, and 20 kHz is cut at Nyquist.
    assert np.max(np.abs(b[7:29])) < 0.3
    assert np.max(np.abs(b[:7])) < 1.5


def test_loudness_of_the_gated_spectrum_matches_pyloudnorm():
    x = shaped_noise(BALANCED)
    s = A.analyse(x)
    ref = pyln.Meter(SR).integrated_loudness(x.astype(np.float64))
    assert A.loudness_of_power(s.power) == pytest.approx(ref, abs=0.2)


def test_silence_and_quiet_passages_are_gated_out():
    x = shaped_noise(FLAT, seconds=20)
    loud = A.analyse(x[: SR * 10])
    y = x.copy()
    y[SR * 10 :] = 0.0  # ten seconds of digital silence
    y[SR * 5 : SR * 7] *= 0.01  # a passage 40 dB down (bleed level)
    s = A.analyse(y)
    assert s.gated_seconds == pytest.approx(8.0, abs=0.6)
    assert np.max(np.abs((s.bands - s.bands[17]) - (loud.bands - loud.bands[17]))[7:29]) < 0.5


def test_streaming_in_odd_chunks_gives_the_same_result():
    x = shaped_noise(BALANCED, seconds=6)
    whole = A.analyse(x)
    acc = A.SpectrumAccumulator()
    flat = x.reshape(-1)
    # Any whole stereo frames, as the server feeds them.
    for a, b in [(0, 1000), (1000, 77778), (77778, 300002), (300002, flat.size)]:
        acc.feed(flat[a:b])
    acc.finish()
    part = acc.result()
    assert part.gated_frames == whole.gated_frames
    assert np.allclose(part.power, whole.power)


def test_digital_silence_is_silent():
    s = A.analyse(np.zeros((SR * 3, 2), dtype=np.float32))
    assert s.silent


def test_cache_form_round_trips():
    s = A.analyse(shaped_noise(BALANCED, seconds=4))
    back = A.spectrum_from_json(A.spectrum_to_json(s))
    assert back.gated_frames == s.gated_frames
    assert np.max(np.abs(back.bands - s.bands)) < 0.01


# ---------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------


def test_pink_noise_against_a_flat_target_needs_nothing():
    _s, fit, bands, out, before, after = fit_of(shaped_noise(FLAT), FLAT)
    assert bands == []
    assert out == 0.0
    assert before < 0.5


def test_noise_shaped_like_balanced_needs_nothing():
    _s, fit, bands, _out, before, _after = fit_of(shaped_noise(BALANCED), BALANCED, "master")
    assert bands == []
    assert before < 0.5


@pytest.mark.parametrize("target", [FLAT, BALANCED])
def test_a_bump_at_250_hz_comes_back_as_a_cut_there(target):
    bump = Band("bell", 250.0, 6.0, 1.0, 12)
    x = shaped_noise(target, extra=[bump])
    # The line itself (no tolerance range): the cut matches the bump.
    _s, fit, bands, _o, before, after = fit_of(x, target, tolerance=0.0)
    cuts = [b for b in bands if b.type == "bell" and 160 <= b.f <= 400]
    assert cuts, bands
    assert -6.0 <= min(b.g for b in cuts) <= -4.0
    assert after < before / 2
    # The default (with the tolerance range): a smaller cut at the same place.
    _s, fit, bands, _o, before, after = fit_of(x, target)
    cuts = [b for b in bands if b.type == "bell" and 160 <= b.f <= 400]
    assert cuts and -6.0 <= min(b.g for b in cuts) <= -2.5
    assert after < before


def test_a_dark_signal_gets_a_high_boost():
    x = shaped_noise(BALANCED, extra=[Band("highshelf", 4000.0, -8.0, 0.707, 12)])
    _s, fit, bands, _o, before, after = fit_of(x, BALANCED, "master", max_bands=F.MAX_BANDS_MASTER)
    high = [b for b in bands if b.f >= 2000 and b.type in ("highshelf", "bell")]
    assert high and all(b.g > 0 for b in high)
    assert after < before - 0.5


def test_the_fit_lowers_the_distance_to_the_target():
    cases = [
        (FLAT, [Band("bell", 250.0, 6.0, 1.0, 12)]),
        (BALANCED, [Band("highshelf", 5000.0, 5.0, 0.707, 12), Band("bell", 300.0, 4.0, 1.4, 12)]),
        (BALANCED, [Band("lowshelf", 100.0, 6.0, 0.707, 12)]),
        (BALANCED, [Band("bell", 2500.0, -6.0, 0.7, 12)]),
    ]
    for target, extra in cases:
        s, _fit, bands, out, before, after = fit_of(shaped_noise(target, extra=extra), target)
        assert after <= before + 1e-9
        w = F.weights_for(s.bands)
        outside = F.outside_rms_db(np.asarray(target) - s.bands, w)
        if outside > 1.0:  # something outside the tolerance range
            left = F.outside_rms_db(
                np.asarray(target) - F.band_levels_after(s.power, bands, out), w
            )
            assert bands and after < before and left < outside


def test_suggesting_on_a_suggested_result_changes_little():
    x = shaped_noise(
        BALANCED,
        extra=[Band("bell", 300.0, 6.0, 1.0, 12), Band("highshelf", 6000.0, -6.0, 0.707, 12)],
    )
    s = A.analyse(x)
    first = F.fit_eq(s.bands, BALANCED, "master", F.MAX_BANDS_MASTER)
    bands = [fb.band for fb in first.bands]
    out = F.neutral_out_db(s.power, bands)
    assert bands
    again = F.fit_eq(F.band_levels_after(s.power, bands, out), BALANCED, "master")
    assert all(abs(fb.band.g) <= 1.5 for fb in again.bands if fb.band.type != "lowcut")
    assert len(again.bands) <= 1


def _random_spectrum(rng) -> np.ndarray:
    pts = rng.normal(0, 6, 8)
    return np.interp(np.arange(A.N_BANDS), np.linspace(0, A.N_BANDS - 1, 8), pts) + np.asarray(
        BALANCED
    )


@pytest.mark.parametrize("layout", ["free", "master", "bass", "guitar", "keys"])
def test_bounds_are_respected(layout):
    rng = np.random.default_rng(7)
    slots = {s.id: s for s in F.LAYOUTS[layout]}
    max_bands = F.MAX_BANDS_MASTER if layout == "master" else F.MAX_BANDS_LANE
    target = get_target(
        {"bass": "bass.balanced", "guitar": "guitar.balanced", "keys": "keys.balanced"}.get(
            layout, "mix.balanced"
        )
    ).bands_db
    for _ in range(25):
        fit = F.fit_eq(_random_spectrum(rng), target, layout, max_bands)
        assert len(fit.bands) <= max_bands
        curve = np.zeros(A.N_BANDS)
        for fb in fit.bands:
            b, slot = fb.band, slots[fb.slot]
            assert b.type == slot.type
            assert abs(b.g) <= F.SUGGEST_GAIN_MAX
            if b.type != "lowcut":
                assert abs(b.g) >= F.MIN_GAIN_DB
            if not slot.dynamic_shelf:
                assert b.f in slot.freqs
            assert b.q in slot.qs
            curve += F.band_response_db(b)
        assert np.max(np.abs(curve[3:])) <= F.COMBINED_MAX_DB + 1e-9
        if layout in F.FIXED_LAYOUTS:
            ids = [fb.slot for fb in fit.bands]
            assert len(ids) == len(set(ids)), "a knob used twice"
            bells = [
                (list(slots).index(fb.slot), fb.band.f)
                for fb in fit.bands
                if fb.band.type == "bell"
            ]
            assert [f for _i, f in sorted(bells)] == sorted(f for _i, f in bells), (
                "bell knobs out of order"
            )


def test_the_bass_shelf_corner_moves_with_its_gain_like_the_panel():
    assert F.dynamic_shelf_freq(2) == 75
    assert F.dynamic_shelf_freq(-15) == 400
    assert F.dynamic_shelf_freq(9) == pytest.approx(75 * (400 / 75) ** 0.5, abs=0.1)


def test_no_boost_where_there_is_nothing_to_lift():
    # A mix with no low end at all (60 dB down under 80 Hz).
    levels = np.asarray(BALANCED).copy()
    levels[:6] -= 60
    fit = F.fit_eq(levels, BALANCED, "master", F.MAX_BANDS_MASTER)
    assert not [fb for fb in fit.bands if fb.band.f < 100 and fb.band.g > 0]


def test_reasons_follow_the_numbers():
    x = shaped_noise(FLAT, extra=[Band("bell", 250.0, 6.0, 1.0, 12)])
    s = A.analyse(x)
    fit = F.fit_eq(s.bands, FLAT, "free")
    why = F.band_reasons(fit)
    cut = next(w for w in why if w["key"] == "suggest.why.bellCut")
    assert cut["vars"]["region"] == "lowmid"
    assert cut["vars"]["dev"] > 2.5  # that much above the target there
    assert cut["vars"]["g"] < 0


# ---------------------------------------------------------------------------
# Loudness-neutral, measured on a render
# ---------------------------------------------------------------------------


def _ffmpeg_render(src, dst, chain):
    subprocess.run(
        [ffmpeg_executable(), "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-af", chain, "-c:a", "pcm_f32le", str(dst)],
        check=True,
        timeout=120,
    )  # fmt: skip


def test_the_suggested_eq_keeps_the_loudness(tmp_path):
    skip_without_ffmpeg()
    meter = pyln.Meter(SR)
    for extra, target in [
        ([Band("bell", 250.0, 6.0, 1.0, 12)], FLAT),
        ([Band("highshelf", 4000.0, -8.0, 0.707, 12)], BALANCED),
        ([Band("lowshelf", 120.0, 6.0, 0.707, 12), Band("bell", 3000.0, -5.0, 1.0, 12)], BALANCED),
    ]:
        x = shaped_noise(target, extra=extra, seconds=8)
        src = tmp_path / "in.wav"
        sf.write(src, x, SR, subtype="FLOAT")
        s = A.analyse(x)
        fit = F.fit_eq(s.bands, target, "master", F.MAX_BANDS_MASTER)
        bands = [fb.band for fb in fit.bands]
        assert bands
        out = F.neutral_out_db(s.power, bands)
        dst = tmp_path / "out.wav"
        _ffmpeg_render(src, dst, eq_filter(Eq(out, tuple(bands)), SR))
        y, _ = sf.read(dst, dtype="float64")
        change = meter.integrated_loudness(y) - meter.integrated_loudness(x.astype(np.float64))
        # Without the output gain the same bands would change it by:
        raw = F.loudness_change_db(s.power, bands)
        assert abs(change) < 0.5, (change, raw)


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


def test_merge_keeps_the_current_bands_and_adds_new_ones():
    x = shaped_noise(
        BALANCED,
        extra=[Band("bell", 300.0, 6.0, 1.0, 12), Band("highshelf", 6000.0, -6.0, 0.707, 12)],
    )
    s = A.analyse(x)
    current = Eq(1.5, (Band("bell", 300.0, -5.0, 1.0, 12),))
    t = get_target("mix.balanced")
    ans = make_suggestion(s, t, "master", "merge", current, F.MAX_BANDS_MASTER, [])
    assert ans["status"] == "ok"
    assert ans["eq"]["bands"][0] == {"type": "bell", "f": 300.0, "g": -5.0, "q": 1.0, "slope": 12}
    assert len(ans["eq"]["bands"]) == 1 + len(ans["bands"])
    assert ans["bands"] and all(b["f"] >= 2000 for b in ans["bands"])  # the mud is already handled
    assert ans["distance"]["suggested"]["rms"] < ans["distance"]["current"]["rms"]
    replace = make_suggestion(s, t, "master", "replace", current, F.MAX_BANDS_MASTER, [])
    assert any(160 <= b["f"] <= 400 and b["g"] < 0 for b in replace["bands"])
    assert abs(replace["loudness"]["suggested_change"]) < 0.15


def test_a_quiet_lane_gets_no_suggestion():
    x = shaped_noise(FLAT, seconds=10) * 10 ** (-45 / 20)  # about -65 LUFS
    ans = make_suggestion(
        A.analyse(x), get_target("other.balanced"), "free", "replace", None, 5, []
    )
    assert ans["status"] == "quiet"
    assert ans["why"][0]["key"] == "suggest.why.quiet"


# ---------------------------------------------------------------------------
# The master preset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("i", "tp", "preset", "target"),
    [
        (-16.2, -2.0, "streaming", -14.0),  # Shining Star: PLR 14
        (-13.6, 1.4, "streaming", -14.0),  # Happy: PLR 15, 2 dB of limiting at -14
        (-9.1, 0.75, "louder", -9.0),  # Come As You Are: dense and loud
        (-20.0, -0.5, "custom", -16.5),  # PLR 19.5: -14 would need 6.5 dB
        (-30.0, -2.0, "gentle", None),  # PLR 28: even -24 would need 5 dB
    ],
)
def test_master_preset_respects_the_material(i, tp, preset, target):
    m = F.recommend_master(i, tp, -14.0)
    assert m["preset"] == preset
    assert m["target_lufs"] == target
    assert m["limit_db"] <= F.LIMIT_ALLOWANCE_DB + 1e-9
    assert m["ceiling_dbtp"] == -1.0


def test_master_preset_for_silence_is_gentle():
    assert F.recommend_master(None, None, -14.0)["preset"] == "gentle"


# ---------------------------------------------------------------------------
# The targets
# ---------------------------------------------------------------------------


def test_targets_are_complete():
    targets = load_targets()
    assert len(targets) == len({t for t in targets})
    for t in targets.values():
        assert len(t.bands_db) == 31
        assert all(np.isfinite(t.bands_db))
        assert t.label_key.startswith("suggest.target.")
        assert t.id == f"{t.use}.{t.variant}"
        if t.use == "mix":
            assert t.target_lufs == -14
        else:
            assert t.target_lufs is None
        assert t.to_json()["bands_db"] == list(t.bands_db)


def test_every_lane_with_a_panel_has_targets():
    for lane in ("bass", "drums", "guitar", "piano", "other", "original"):
        assert default_target(lane, recorded=False) is not None
    assert default_target("vocals", recorded=False) is None  # the voice placeholder
    assert len(uses_for_lane("k0123abcd", recorded=True)) >= 4


def test_balanced_is_darker_than_pink_above_1_khz():
    # The point of the published curves (Elowsson and Friberg 2017): music
    # falls faster than pink noise at the top.
    b = np.asarray(BALANCED)
    assert b[17] - b[27] > 10  # 1 kHz vs 10 kHz
    assert abs(b[11] - b[15]) < 1.5  # 250 vs 630 Hz: close to pink
