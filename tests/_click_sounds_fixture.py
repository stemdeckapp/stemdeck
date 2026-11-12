"""Measurements of every click-sound preset voice, generated from the Python side.

The live click is synthesised in the browser (static/js/clickSounds.js) and an
export in Python (app/pipeline/click_sounds.py), from one parameter table. Two
implementations of the same formulas can still drift: a sign, a rounding rule,
a filter coefficient. The two CI jobs cannot run both languages (pytest has no
node, js-syntax has no python), so a test that shelled out to the other side
would simply skip. Like the click levels (tests/_click_parity.py), both sides
are pinned to one committed artifact instead:

- tests/test_click_sounds.py asserts tests/fixtures/click_sounds.json is what
  Python measures now;
- tests/js/click-sounds.test.mjs asserts the browser synthesis measures the
  same, within the tolerances it states.

What is measured, per preset, level and sample rate, and why each:

- length in samples: the voice's duration, exactly;
- peak index and peak value: where the attack lands and how loud it is;
- envelope: RMS in 1 ms frames, which is the shape a listener hears as the
  click's attack and decay, and is insensitive to phase;
- spectral centroid of the whole voice: its brightness, the thing that makes a
  woodblock not sound like a cowbell; and
- a coarse sample checksum (sum of |x|): catches a component dropped or
  doubled even if it hid under another one's envelope.

These are robust to the last-bit differences between Math.sin and numpy's sin
(which is why the comparison is a tolerance and not equality of samples), and
none of them can pass if the two sides synthesise different sounds.

Regenerate after an intentional change to the table or the synthesis:

    uv run python -c "import json,sys; sys.path.insert(0,'tests'); \\
from _click_sounds_fixture import build; \\
json.dump(build(), open('tests/fixtures/click_sounds.json','w'), indent=1, sort_keys=True)"
"""

from __future__ import annotations

import numpy as np

from app.pipeline.click_sounds import CLICK_SOUND_IDS, render_voice

RATES = (44100, 48000)
LEVELS = (0, 1, 2)
FRAME_SEC = 0.001


def measure(voice: np.ndarray, sample_rate: int) -> dict:
    x = np.asarray(voice, dtype=np.float64)
    hop = max(1, int(round(FRAME_SEC * sample_rate)))
    env = [round(float(np.sqrt(np.mean(x[i : i + hop] ** 2))), 6) for i in range(0, len(x), hop)]
    size = 1
    while size < len(x):
        size *= 2
    # Power-weighted: brightness as heard, not the sidelobes of a short burst.
    power = np.abs(np.fft.rfft(x, n=size)) ** 2
    freqs = np.arange(len(power)) * sample_rate / size
    centroid = float(np.sum(freqs * power) / np.sum(power)) if np.sum(power) > 0 else 0.0
    peak = int(np.argmax(np.abs(x)))
    return {
        "length": len(x),
        "peakIndex": peak,
        "peak": round(float(abs(x[peak])), 6),
        "envelope": env,
        "centroid": round(centroid, 3),
        "absSum": round(float(np.sum(np.abs(x))), 4),
    }


def build() -> dict:
    out: dict = {"frameSec": FRAME_SEC, "voices": []}
    for sound in CLICK_SOUND_IDS:
        for sr in RATES:
            for level in LEVELS:
                entry = {"sound": sound, "sampleRate": sr, "level": level}
                entry.update(measure(render_voice(sound, level, sr), sr))
                out["voices"].append(entry)
    return out
