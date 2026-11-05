// Click sounds (#805): the one table both click renderers read.
//
// The live click is synthesised here, in the browser; an export is rendered in
// Python (app/pipeline/click_render.py). A preset that sounded one way while
// monitoring and another way in the exported file would be worse than having
// one sound, so the parameters live in exactly one place: the JSON object
// between the two markers below. app/pipeline/click_sounds.py reads this file
// and parses that object, and both sides run the same synthesis on it (the
// formulas below and their numpy twin in click_sounds.py).
//
// The object between the markers must stay strict JSON: double quotes, no
// comments, no trailing commas. Python parses it with json.loads.
//
// Every preset has three voices, one per click level: "accent" for the
// downbeat, "group" for the start of a beat group inside the bar (#595), and
// "beat" for every other beat. A voice is a list of components summed
// together, each with its own envelope:
//
//   sine   freq Hz, optionally gliding exponentially to freqEnd over sweep s
//   noise  white noise (a seeded xorshift32, so it is the same every time and
//          in both languages) through a band-pass at center Hz with q
//
// The envelope is the one the original click used: an exponential ramp from
// "floor" up to peak over attack seconds, then back down to floor at decay
// seconds, where the component ends. "gain" scales the whole preset and is
// what level-matches the presets to Classic (see tests/test_click_sounds.py
// for the measurement it is held to).
//
// Classic is the click StemDeck always had: these numbers are the old
// constants, and tests/test_click_sounds.py pins its render to a fingerprint
// taken before presets existed, so the default cannot change by accident.
//
// Adding a preset: append an entry with a new id and a "label" key in
// i18n.js; the selector, the export whitelist and the drift fixture pick it up
// from here. A user sample (a later feature) would be an entry with
// "kind": "sample" and a renderVoice branch for it on each side; until then
// both sides refuse any kind other than "synth".
export const CLICK_SOUNDS = /* CLICK_SOUNDS_JSON_BEGIN */ {
  "version": 1,
  "default": "classic",
  "floor": 0.0001,
  "presets": [
    {
      "id": "classic",
      "kind": "synth",
      "label": "click.soundClassic",
      "gain": 1.0,
      "voices": {
        "accent": [{ "type": "sine", "freq": 1500, "attack": 0.001, "decay": 0.035, "peak": 1.0 }],
        "group": [{ "type": "sine", "freq": 1225, "attack": 0.001, "decay": 0.035, "peak": 0.85 }],
        "beat": [{ "type": "sine", "freq": 1000, "attack": 0.001, "decay": 0.035, "peak": 0.7 }]
      }
    },
    {
      "id": "woodblock",
      "kind": "synth",
      "label": "click.soundWoodblock",
      "gain": 0.708,
      "voices": {
        "accent": [
          { "type": "sine", "freq": 1250, "attack": 0.0005, "decay": 0.07, "peak": 1.0 },
          { "type": "sine", "freq": 3400, "attack": 0.0005, "decay": 0.03, "peak": 0.3 },
          { "type": "noise", "center": 4000, "q": 1.0, "seed": 1250, "attack": 0.0003, "decay": 0.006, "peak": 0.6 }
        ],
        "group": [
          { "type": "sine", "freq": 1060, "attack": 0.0005, "decay": 0.07, "peak": 0.85 },
          { "type": "sine", "freq": 2890, "attack": 0.0005, "decay": 0.03, "peak": 0.26 },
          { "type": "noise", "center": 3500, "q": 1.0, "seed": 1060, "attack": 0.0003, "decay": 0.006, "peak": 0.5 }
        ],
        "beat": [
          { "type": "sine", "freq": 900, "attack": 0.0005, "decay": 0.07, "peak": 0.7 },
          { "type": "sine", "freq": 2450, "attack": 0.0005, "decay": 0.03, "peak": 0.21 },
          { "type": "noise", "center": 3000, "q": 1.0, "seed": 900, "attack": 0.0003, "decay": 0.006, "peak": 0.42 }
        ]
      }
    },
    {
      "id": "rimshot",
      "kind": "synth",
      "label": "click.soundRimshot",
      "gain": 0.902,
      "voices": {
        "accent": [
          { "type": "noise", "center": 3000, "q": 0.9, "seed": 3000, "attack": 0.0005, "decay": 0.08, "peak": 0.75 },
          { "type": "sine", "freq": 680, "freqEnd": 500, "sweep": 0.02, "attack": 0.0005, "decay": 0.06, "peak": 0.7 },
          { "type": "sine", "freq": 2100, "attack": 0.0005, "decay": 0.05, "peak": 0.45 }
        ],
        "group": [
          { "type": "noise", "center": 2400, "q": 0.9, "seed": 2400, "attack": 0.0005, "decay": 0.075, "peak": 0.64 },
          { "type": "sine", "freq": 610, "freqEnd": 450, "sweep": 0.02, "attack": 0.0005, "decay": 0.055, "peak": 0.6 },
          { "type": "sine", "freq": 1920, "attack": 0.0005, "decay": 0.045, "peak": 0.38 }
        ],
        "beat": [
          { "type": "noise", "center": 1900, "q": 0.9, "seed": 1900, "attack": 0.0005, "decay": 0.07, "peak": 0.53 },
          { "type": "sine", "freq": 560, "freqEnd": 420, "sweep": 0.02, "attack": 0.0005, "decay": 0.05, "peak": 0.49 },
          { "type": "sine", "freq": 1750, "attack": 0.0005, "decay": 0.04, "peak": 0.32 }
        ]
      }
    },
    {
      "id": "soft",
      "kind": "synth",
      "label": "click.soundSoft",
      "gain": 0.55,
      "voices": {
        "accent": [
          { "type": "sine", "freq": 880, "attack": 0.006, "decay": 0.14, "peak": 1.0 },
          { "type": "sine", "freq": 1760, "attack": 0.006, "decay": 0.08, "peak": 0.12 }
        ],
        "group": [
          { "type": "sine", "freq": 762, "attack": 0.006, "decay": 0.14, "peak": 0.85 },
          { "type": "sine", "freq": 1524, "attack": 0.006, "decay": 0.08, "peak": 0.1 }
        ],
        "beat": [
          { "type": "sine", "freq": 660, "attack": 0.006, "decay": 0.14, "peak": 0.7 },
          { "type": "sine", "freq": 1320, "attack": 0.006, "decay": 0.08, "peak": 0.085 }
        ]
      }
    },
    {
      "id": "cowbell",
      "kind": "synth",
      "label": "click.soundCowbell",
      "gain": 0.603,
      "voices": {
        "accent": [
          { "type": "sine", "freq": 700, "attack": 0.001, "decay": 0.15, "peak": 0.6 },
          { "type": "sine", "freq": 1050, "attack": 0.001, "decay": 0.15, "peak": 0.6 },
          { "type": "sine", "freq": 2100, "attack": 0.001, "decay": 0.06, "peak": 0.2 },
          { "type": "sine", "freq": 3150, "attack": 0.001, "decay": 0.04, "peak": 0.15 }
        ],
        "group": [
          { "type": "sine", "freq": 627, "attack": 0.001, "decay": 0.15, "peak": 0.51 },
          { "type": "sine", "freq": 943, "attack": 0.001, "decay": 0.15, "peak": 0.51 },
          { "type": "sine", "freq": 1881, "attack": 0.001, "decay": 0.06, "peak": 0.17 },
          { "type": "sine", "freq": 2829, "attack": 0.001, "decay": 0.04, "peak": 0.13 }
        ],
        "beat": [
          { "type": "sine", "freq": 562, "attack": 0.001, "decay": 0.15, "peak": 0.42 },
          { "type": "sine", "freq": 845, "attack": 0.001, "decay": 0.15, "peak": 0.42 },
          { "type": "sine", "freq": 1686, "attack": 0.001, "decay": 0.06, "peak": 0.14 },
          { "type": "sine", "freq": 2535, "attack": 0.001, "decay": 0.04, "peak": 0.1 }
        ]
      }
    }
  ]
} /* CLICK_SOUNDS_JSON_END */;

// Click strength at one beat, as metronome.js and click_render.py number it.
const LEVEL_VOICE = ["beat", "group", "accent"];

export const DEFAULT_CLICK_SOUND = CLICK_SOUNDS.default;

const _byId = new Map(CLICK_SOUNDS.presets.map((p) => [p.id, p]));

/** Every preset id, in the order the selector lists them. */
export const CLICK_SOUND_IDS = CLICK_SOUNDS.presets.map((p) => p.id);

/** A known preset id, or the default for anything else. */
export function normaliseClickSound(id) {
  return typeof id === "string" && _byId.has(id) ? id : DEFAULT_CLICK_SOUND;
}

/** The preset entry for `id` (the default for an unknown one). */
export function clickSoundPreset(id) {
  return _byId.get(normaliseClickSound(id));
}

// Samples in a duration. Half rounds up, in both languages: Python's round()
// rounds half to even and Math.round rounds half up, so neither is used.
function _samples(seconds, sampleRate) {
  return Math.floor(seconds * sampleRate + 0.5);
}

// The original click's envelope: exponential ramps cannot start from zero, so
// both legs run from the floor (the old scheduler's 0.0001).
function _env(t, attack, decay, peak, floor) {
  if (t <= attack) return floor * (peak / floor) ** (t / attack);
  return peak * (floor / peak) ** ((t - attack) / (decay - attack));
}

// Phase of a sine at time t, in closed form so a glide needs no running sum
// (which would accumulate rounding differently in each language).
function _phase(c, t) {
  const f = c.freq;
  const fEnd = c.freqEnd;
  const sweep = c.sweep || 0;
  if (fEnd && sweep > 0 && fEnd !== f) {
    const k = Math.log(fEnd / f) / sweep;
    if (t < sweep) return (2 * Math.PI * f * (Math.exp(k * t) - 1)) / k;
    return (2 * Math.PI * f * (fEnd / f - 1)) / k + 2 * Math.PI * fEnd * (t - sweep);
  }
  return 2 * Math.PI * f * t;
}

// White noise in [-1, 1) from xorshift32, band-passed (RBJ, 0 dB peak).
function _noise(c, m, sampleRate) {
  let x = (c.seed >>> 0) || 1;
  const out = new Float64Array(m);
  const w0 = (2 * Math.PI * c.center) / sampleRate;
  const alpha = Math.sin(w0) / (2 * c.q);
  const a0 = 1 + alpha;
  const b0 = alpha / a0;
  const b2 = -alpha / a0;
  const a1 = (-2 * Math.cos(w0)) / a0;
  const a2 = (1 - alpha) / a0;
  let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  for (let i = 0; i < m; i++) {
    x = (x ^ (x << 13)) >>> 0;
    x = (x ^ (x >>> 17)) >>> 0;
    x = (x ^ (x << 5)) >>> 0;
    const u = (x / 4294967296) * 2 - 1;
    const y = b0 * u + b2 * x2 - a1 * y1 - a2 * y2;
    x2 = x1; x1 = u;
    y2 = y1; y1 = y;
    out[i] = y;
  }
  return out;
}

/**
 * One click voice as samples: the preset's components for `level`, summed and
 * scaled by the preset's gain. Pure, so node can run it for the drift test.
 * @param {string} id      preset id (unknown ids fall back to the default)
 * @param {number} level   0 beat, 1 group start, 2 downbeat
 * @param {number} sampleRate
 * @returns {Float64Array}
 */
export function renderVoice(id, level, sampleRate) {
  const preset = clickSoundPreset(id);
  if (preset.kind !== "synth") throw new Error(`click sound kind ${preset.kind} is not supported`);
  const comps = preset.voices[LEVEL_VOICE[level] || "beat"];
  const floor = CLICK_SOUNDS.floor;
  let n = 0;
  for (const c of comps) n = Math.max(n, _samples(c.decay, sampleRate));
  const out = new Float64Array(n);
  for (const c of comps) {
    const m = _samples(c.decay, sampleRate);
    const noise = c.type === "noise" ? _noise(c, m, sampleRate) : null;
    for (let i = 0; i < m; i++) {
      const t = i / sampleRate;
      const s = noise ? noise[i] : Math.sin(_phase(c, t));
      out[i] += s * _env(t, c.attack, c.decay, c.peak, floor);
    }
  }
  if (preset.gain !== 1) for (let i = 0; i < n; i++) out[i] *= preset.gain;
  return out;
}
