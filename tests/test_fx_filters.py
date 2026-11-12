"""The export EQ maths (app/pipeline/fx_filters.py) against the live engine.

tests/fixtures/eq_golden.json holds, for every band type, both cut slopes,
high and low frequencies and a full cascade, the response Chromium's own
BiquadFilterNodes report at 44.1 and 48 kHz (written by
tests/e2e/eq-engine.spec.mjs, which also holds static/js/eqMath.js to it).
Here the Python coefficients are held to that file, and an ffmpeg render of
an impulse through the filter string the export uses is held to the Python
maths, so the three implementations cannot drift apart.

Then the fx= parser: every malformed or hostile value is a FxError (a 422 at
the endpoint), never an exception that escapes, and nothing but numbers this
module formats reaches the filter graph.
"""

from __future__ import annotations

import base64
import json
import math
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.core.config import ffmpeg_executable
from app.pipeline.fx_filters import (
    FX_PARAM_MAX_CHARS,
    MAX_TOTAL_SECTIONS,
    Band,
    Eq,
    FxError,
    band_sections,
    butterworth_qs,
    check_section_budget,
    eq_filter,
    eq_response_db,
    parse_fx_param,
    section_coefficients,
    slot_filter,
)
from tests.ffmpeg_probe import skip_without_ffmpeg

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "eq_golden.json").read_text("utf-8"))


def _eq(raw: dict) -> Eq:
    """A golden case (eqMath descriptor) as the server parses it."""
    bands = tuple(
        Band(b["type"], round(b["f"], 1), round(b["g"], 2), round(b["q"], 3), b["slope"])
        for b in raw["params"]["bands"]
    )
    return Eq(round(raw["params"]["out"], 2), bands)


def _wire(body: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")


# ---------------------------------------------------------------------------
# Maths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rate", GOLDEN["rates"])
@pytest.mark.parametrize("case", sorted(GOLDEN["cases"]))
def test_python_coefficients_match_chromium(rate, case):
    """Python's response equals what Chromium's nodes report, to 0.001 dB at
    every frequency from 20 Hz to 20 kHz (measured: under 0.0001)."""
    ours = eq_response_db(_eq(GOLDEN["cases"][case]), GOLDEN["freqs"], rate)
    theirs = GOLDEN["chromium"][str(rate)][case]
    worst = max(abs(a - b) for a, b in zip(ours, theirs, strict=True))
    assert worst < 1e-3, f"{case} at {rate}: {worst:.2e} dB"


def test_butterworth_section_qs():
    assert butterworth_qs(2) == pytest.approx([math.sqrt(0.5)])
    assert butterworth_qs(4) == pytest.approx([0.5411961, 1.3065630])
    assert butterworth_qs(8) == pytest.approx([0.5097956, 0.6013449, 0.8999762, 2.5629154])


def test_cut_sections_and_resonance():
    flat = band_sections(Band("lowcut", 100, 0, 0.707, 24))
    assert [s.type for s in flat] == ["highpass", "highpass"]
    assert [s.q for s in flat] == pytest.approx(butterworth_qs(4))
    peaky = band_sections(Band("highcut", 100, 0, 1.414, 24))
    assert [s.type for s in peaky] == ["lowpass", "lowpass"]
    assert peaky[1].q == pytest.approx(butterworth_qs(4)[1] * 2)
    # A Butterworth cut is 3 dB down at its frequency, at any slope.
    for slope in (12, 24, 36, 48):
        db = eq_response_db(Eq(0, (Band("lowcut", 1000, 0, 0.707, slope),)), [1000], 44100)[0]
        assert db == pytest.approx(-3.01, abs=0.02)


def test_neutral_bands_and_flat_eq_add_nothing():
    flat = Eq(0, (Band("bell", 1000, 0, 1, 12), Band("tilt", 500, 0, 1, 12)))
    assert eq_filter(flat, 44100) == ""
    assert slot_filter([flat], 44100) == ""
    out_only = Eq(-6, ())
    assert eq_filter(out_only, 44100) == f"volume={10 ** (-6 / 20)!r}"


def test_tilt_is_two_opposite_shelves():
    s = band_sections(Band("tilt", 1000, 6, 1, 12))
    assert [(x.type, x.g) for x in s] == [("lowshelf", -3), ("highshelf", 3)]
    lo, hi = eq_response_db(Eq(0, (Band("tilt", 1000, 6, 1, 12),)), [20, 20000], 44100)
    assert lo == pytest.approx(-3, abs=0.05)
    assert hi == pytest.approx(3, abs=0.1)


def test_filter_string_is_numbers_only():
    text = eq_filter(_eq(GOLDEN["cases"]["cascade"]), 44100)
    for part in text.split(","):
        name, _, args = part.partition("=")
        assert name in ("biquad", "volume")
        if name == "volume":
            float(args)
            continue
        for kv in args.split(":"):
            key, _, value = kv.partition("=")
            if key == "r":
                assert value == "f64"
                continue
            float(value)  # every other value is a plain number


# ---------------------------------------------------------------------------
# ffmpeg runs exactly these coefficients
# ---------------------------------------------------------------------------


def _ffmpeg_impulse(
    tmp_path: Path, chain: str, rate: int, seconds: float = 1.0, subtype: str = "DOUBLE"
) -> np.ndarray:
    x = np.zeros(int(rate * seconds))
    x[0] = 0.5
    src = tmp_path / "imp.wav"
    dst = tmp_path / "out.wav"
    sf.write(src, x, rate, subtype=subtype)
    subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-af",
            chain,
            "-c:a",
            "pcm_f64le",
            str(dst),
        ],
        check=True,
        timeout=60,
    )
    return sf.read(dst, dtype="float64")[0]


@pytest.mark.parametrize("rate", GOLDEN["rates"])
@pytest.mark.parametrize("case", sorted(GOLDEN["cases"]))
def test_ffmpeg_render_matches_the_maths(tmp_path, rate, case):
    """The impulse response ffmpeg renders through the export's filter string
    has the response computed here (and so Chromium's), to 0.00001 dB from 20
    Hz to 20 kHz (measured: under 0.000002 dB)."""
    skip_without_ffmpeg()
    eq = _eq(GOLDEN["cases"][case])
    h = 2 * _ffmpeg_impulse(tmp_path, eq_filter(eq, rate), rate)
    n = len(h)
    spec = np.fft.rfft(h)
    freqs = np.unique(np.round(np.geomspace(20, 20000, 150)).astype(int))
    measured = 20 * np.log10(np.abs(spec[(freqs * n) // rate]))
    expected = np.array(eq_response_db(eq, [float(f) for f in freqs], rate))
    worst = float(np.max(np.abs(measured - expected)))
    assert worst < 1e-5, f"{case} at {rate}: {worst:.2e} dB"


def test_ffmpeg_runs_in_double_precision(tmp_path):
    """r=f64: a 16-bit stem is not filtered in 16 bits. With ffmpeg's default
    (auto) precision the filter runs in the input's format, so its output
    would be whole multiples of 1/32768; here it is not."""
    skip_without_ffmpeg()
    eq = Eq(0, (Band("bell", 1000, 6, 1, 12),))
    h = _ffmpeg_impulse(tmp_path, eq_filter(eq, 44100), 44100, seconds=0.1, subtype="PCM_16")
    steps = h[:2000] * 32768
    assert np.any(np.abs(steps - np.round(steps)) > 1e-3)
    auto = _ffmpeg_impulse(
        tmp_path, eq_filter(eq, 44100).replace(":r=f64", ""), 44100, 0.1, "PCM_16"
    )
    auto_steps = auto[:2000] * 32768
    assert np.all(np.abs(auto_steps - np.round(auto_steps)) < 1e-6)


# ---------------------------------------------------------------------------
# Coefficients by hand for one case (the formulas, independent of the file)
# ---------------------------------------------------------------------------


def test_peaking_coefficients_by_the_spec():
    from app.pipeline.fx_filters import Section

    b0, b1, b2, a1, a2 = section_coefficients(Section("peaking", 1000, 1, 12), 48000)
    a = 10 ** (12 / 40)
    w0 = 2 * math.pi * 1000 / 48000
    alpha = math.sin(w0) / 2
    a0 = 1 + alpha / a
    assert b0 == pytest.approx((1 + alpha * a) / a0, rel=1e-15)
    assert b1 == pytest.approx(-2 * math.cos(w0) / a0, rel=1e-15)
    assert a2 == pytest.approx((1 - alpha / a) / a0, rel=1e-15)
    assert a1 == b1
    assert b2 == pytest.approx((1 - alpha * a) / a0, rel=1e-15)


# ---------------------------------------------------------------------------
# Parsing fx=
# ---------------------------------------------------------------------------


def test_parse_valid_and_empty():
    assert parse_fx_param("").lanes == {}
    req = parse_fx_param(
        _wire(
            {
                "v": 1,
                "lanes": {"bass": [{"id": "eq", "out": -1.5, "b": [["lowcut", 40, 0, 0.707, 24]]}]},
                "master": [{"id": "eq", "out": 0, "b": [["bell", 3000, -2, 1.2, 12]]}],
            }
        )
    )
    assert req.lanes["bass"][0] == Eq(-1.5, (Band("lowcut", 40.0, 0.0, 0.707, 24),))
    assert req.master[0].bands[0].type == "bell"
    # Recording tracks and takes are lane keys too.
    assert "k0123abcd" in parse_fx_param(_wire({"v": 1, "lanes": {"k0123abcd": []}})).lanes


BAD = [
    ("not base64", "!!!!"),
    ("path", "../../etc/passwd"),
    ("not json", _wire("x")[:-2] + "@@"),
    ("json but list", _wire([1, 2])),
    ("no version", _wire({"lanes": {}})),
    ("future version", _wire({"v": 2, "lanes": {}})),
    ("bool version", _wire({"v": True, "lanes": {}})),
    ("extra key", _wire({"v": 1, "lanes": {}, "cmd": "rm"})),
    ("lane name with filter syntax", _wire({"v": 1, "lanes": {"bass[a]": []}})),
    ("lane name upper", _wire({"v": 1, "lanes": {"BASS": []}})),
    ("lanes not a dict", _wire({"v": 1, "lanes": ["bass"]})),
    ("effect not eq", _wire({"v": 1, "lanes": {"bass": [{"id": "afir"}]}})),
    ("two eqs", _wire({"v": 1, "lanes": {"bass": [{"id": "eq"}, {"id": "eq"}]}})),
    ("effect extra field", _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "f": "x"}]}})),
    (
        "type injection",
        _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "b": [["bell,anull", 1e3, 1, 1, 12]]}]}}),
    ),
    ("string number", _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "out": "3"}]}})),
    ("bool number", _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "out": True}]}})),
    ("out of range out", _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "out": 99}]}})),
    (
        "freq above range",
        _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "b": [["bell", 30000, 1, 1, 12]]}]}}),
    ),
    (
        "q zero",
        _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "b": [["bell", 100, 1, 0, 12]]}]}}),
    ),
    (
        "gain too big",
        _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "b": [["bell", 100, 31, 1, 12]]}]}}),
    ),
    (
        "odd slope",
        _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "b": [["lowcut", 100, 0, 1, 18]]}]}}),
    ),
    (
        "float slope",
        _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "b": [["lowcut", 100, 0, 1, 12.0]]}]}}),
    ),
    ("short band", _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "b": [["bell", 100]]}]}})),
    (
        "too many bands",
        _wire({"v": 1, "lanes": {"bass": [{"id": "eq", "b": [["bell", 100, 1, 1, 12]] * 13}]}}),
    ),
    ("too long", "A" * (FX_PARAM_MAX_CHARS + 1)),
]


@pytest.mark.parametrize(("label", "raw"), BAD, ids=[b[0] for b in BAD])
def test_parse_rejects(label, raw):
    with pytest.raises(FxError):
        parse_fx_param(raw)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_parse_rejects_non_finite(constant):
    text = '{"v": 1, "lanes": {"bass": [{"id": "eq", "out": ' + constant + "}]}}"
    raw = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    with pytest.raises(FxError):
        parse_fx_param(raw)


def test_parse_rejects_deep_nesting_without_crashing():
    text = "[" * 5000 + "]" * 5000
    raw = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")[:FX_PARAM_MAX_CHARS]
    with pytest.raises(FxError):
        parse_fx_param(raw)


def test_section_budget():
    check_section_budget(MAX_TOTAL_SECTIONS)
    with pytest.raises(FxError):
        check_section_budget(MAX_TOTAL_SECTIONS + 1)
