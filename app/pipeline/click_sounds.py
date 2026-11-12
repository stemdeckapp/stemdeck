"""Click-sound presets (#805), the Python half.

The presets are defined once, in static/js/clickSounds.js, because the live
click is synthesised in the browser and an export is rendered here: a preset
that sounded different in the exported file than it did while monitoring would
defeat the point. This module parses the strict-JSON block between that file's
markers and runs the same synthesis as its `renderVoice()`, formula for
formula. tests/fixtures/click_sounds.json pins both sides to one set of
measurements (tests/test_click_sounds.py here, tests/js/click-sounds.test.mjs
there), the same discipline that pins the click levels (#595).

`DEFAULT_CLICK_SOUND` is the click StemDeck always had, and its render is
byte-identical to the renderer before presets existed (pinned by a fingerprint
in tests/test_click_sounds.py).
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

import numpy as np

from app.core.config import CLICK_SOUNDS_TABLE

_JSON_BLOCK = re.compile(
    r"/\*\s*CLICK_SOUNDS_JSON_BEGIN\s*\*/(.*?)/\*\s*CLICK_SOUNDS_JSON_END\s*\*/", re.DOTALL
)

# Click strength at one beat, as click_render.py numbers it.
_LEVEL_VOICE = ("beat", "group", "accent")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ClickSoundTableError(RuntimeError):
    """static/js/clickSounds.js is missing, or its table is malformed."""


def _check_component(pid: str, c: dict) -> None:
    kind = c.get("type")
    if kind not in ("sine", "noise"):
        raise ClickSoundTableError(f"{pid}: unknown component type {kind!r}")
    for key in ("attack", "decay", "peak"):
        if not isinstance(c.get(key), (int, float)):
            raise ClickSoundTableError(f"{pid}: component needs a number for {key}")
    if not (0 < c["attack"] < c["decay"] <= 1.0) or not (0 < c["peak"] <= 1.0):
        raise ClickSoundTableError(f"{pid}: envelope out of range")
    if kind == "sine" and not (20 <= float(c.get("freq", 0)) <= 20000):
        raise ClickSoundTableError(f"{pid}: sine needs a freq in 20..20000")
    if kind == "noise":
        if not (20 <= float(c.get("center", 0)) <= 20000) or not float(c.get("q", 0)) > 0:
            raise ClickSoundTableError(f"{pid}: noise needs a center and a q")
        if not isinstance(c.get("seed"), int) or not 0 < c["seed"] < 2**32:
            raise ClickSoundTableError(f"{pid}: noise needs a 32-bit seed")


def parse_table(text: str) -> dict:
    """The preset table from the source of clickSounds.js, validated.

    Strict: a malformed table raises rather than falling back, because the
    export whitelist is derived from it and a silently empty one would refuse
    every sound, including the default."""
    m = _JSON_BLOCK.search(text)
    if m is None:
        raise ClickSoundTableError("clickSounds.js has no CLICK_SOUNDS_JSON block")
    try:
        table = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ClickSoundTableError(f"clickSounds.js table is not strict JSON: {e}") from e
    presets = table.get("presets")
    if not isinstance(presets, list) or not presets:
        raise ClickSoundTableError("clickSounds.js has no presets")
    seen: set[str] = set()
    for p in presets:
        pid = p.get("id")
        if not isinstance(pid, str) or not _ID_RE.match(pid) or pid in seen:
            raise ClickSoundTableError(f"bad or duplicate preset id {pid!r}")
        seen.add(pid)
        if p.get("kind") != "synth":
            raise ClickSoundTableError(f"{pid}: only synth presets are supported")
        if not isinstance(p.get("gain"), (int, float)) or not 0 < p["gain"] <= 4:
            raise ClickSoundTableError(f"{pid}: gain out of range")
        voices = p.get("voices") or {}
        for level in _LEVEL_VOICE:
            comps = voices.get(level)
            if not isinstance(comps, list) or not comps:
                raise ClickSoundTableError(f"{pid}: no {level} voice")
            for c in comps:
                _check_component(pid, c)
    if table.get("default") not in seen:
        raise ClickSoundTableError("the default click sound is not a preset")
    if not isinstance(table.get("floor"), (int, float)) or not 0 < table["floor"] < 0.01:
        raise ClickSoundTableError("floor out of range")
    return table


def load_table(path: Path = CLICK_SOUNDS_TABLE) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ClickSoundTableError(f"cannot read {path.name}") from e
    return parse_table(text)


TABLE = load_table()
DEFAULT_CLICK_SOUND: str = TABLE["default"]
# The export routes' whitelist: a sound id is only ever looked up here, never
# used to build a path or a command.
CLICK_SOUND_IDS: tuple[str, ...] = tuple(p["id"] for p in TABLE["presets"])
_BY_ID = {p["id"]: p for p in TABLE["presets"]}
FLOOR: float = float(TABLE["floor"])


def _samples(seconds: float, sample_rate: int) -> int:
    """Half rounds up, as in clickSounds.js (round() would round half to even)."""
    return int(math.floor(seconds * sample_rate + 0.5))


def _envelope(t: np.ndarray, attack: float, decay: float, peak: float) -> np.ndarray:
    """The original click's two exponential ramps, floor -> peak -> floor."""
    rising = t <= attack
    env = np.empty(len(t))
    env[rising] = FLOOR * (peak / FLOOR) ** (t[rising] / attack)
    span = decay - attack
    env[~rising] = peak * (FLOOR / peak) ** ((t[~rising] - attack) / span)
    return env


def _phase(c: dict, t: np.ndarray) -> np.ndarray:
    f = float(c["freq"])
    f_end = c.get("freqEnd")
    sweep = float(c.get("sweep") or 0.0)
    if f_end and sweep > 0 and f_end != f:
        f_end = float(f_end)
        k = math.log(f_end / f) / sweep
        gliding = 2 * math.pi * f * (np.exp(k * t) - 1) / k
        after = 2 * math.pi * f * (f_end / f - 1) / k + 2 * math.pi * f_end * (t - sweep)
        return np.where(t < sweep, gliding, after)
    return 2.0 * np.pi * f * t


def _noise(c: dict, m: int, sample_rate: int) -> np.ndarray:
    """xorshift32 white noise in [-1, 1), band-passed (RBJ, 0 dB peak).

    A plain loop on purpose: it has to be the same recurrence as the JS one,
    and a voice is a few thousand samples, rendered once per export."""
    x = int(c["seed"]) & 0xFFFFFFFF or 1
    w0 = 2 * math.pi * float(c["center"]) / sample_rate
    alpha = math.sin(w0) / (2 * float(c["q"]))
    a0 = 1 + alpha
    b0 = alpha / a0
    b2 = -alpha / a0
    a1 = (-2 * math.cos(w0)) / a0
    a2 = (1 - alpha) / a0
    x1 = x2 = y1 = y2 = 0.0
    out = np.empty(m)
    for i in range(m):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        u = (x / 4294967296) * 2 - 1
        y = b0 * u + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, u
        y2, y1 = y1, y
        out[i] = y
    return out


@lru_cache(maxsize=64)
def _voice_cached(sound: str, level: int, sample_rate: int) -> np.ndarray:
    preset = _BY_ID[sound]
    comps = preset["voices"][_LEVEL_VOICE[level] if 0 <= level < 3 else "beat"]
    n = max(_samples(float(c["decay"]), sample_rate) for c in comps)
    out = np.zeros(n)
    for c in comps:
        m = _samples(float(c["decay"]), sample_rate)
        t = np.arange(m) / sample_rate
        src = _noise(c, m, sample_rate) if c["type"] == "noise" else np.sin(_phase(c, t))
        out[:m] += src * _envelope(t, float(c["attack"]), float(c["decay"]), float(c["peak"]))
    gain = float(preset["gain"])
    if gain != 1.0:
        out *= gain
    out.setflags(write=False)
    return out


def render_voice(sound: str, level: int, sample_rate: int) -> np.ndarray:
    """One click voice (float64, read-only) for a preset and a click level.

    An unknown `sound` raises KeyError: callers validate against
    CLICK_SOUND_IDS first, and silently clicking the wrong sound would hide a
    bug."""
    if sound not in _BY_ID:
        raise KeyError(sound)
    return _voice_cached(sound, int(level), int(sample_rate))
