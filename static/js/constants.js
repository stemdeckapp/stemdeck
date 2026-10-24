import { t, TRANSLATIONS } from "./i18n.js";

// Fallback list used before /api/config responds. Kept in sync with
// STEM_NAMES in app/core/config.py — the API is the canonical source.
export let STEM_NAMES = ["vocals", "drums", "bass", "guitar", "piano", "other"];
export let TRACK_NAMES = ["original", ...STEM_NAMES];

// Stems from the on-demand lead/backing vocal split (#275, EXTRA_STEM_NAMES
// in app/core/config.py). Not folded into STEM_NAMES/TRACK_NAMES -- most jobs
// never run this split -- but the player/mixer render them as real lanes,
// swapped in for "vocals", via effectiveStemOrder() below.
export let EXTRA_STEM_NAMES = ["lead_vocals", "backing_vocals"];

export async function syncStemNamesFromAPI() {
  try {
    const res = await fetch("/api/config");
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data.stem_names) && data.stem_names.length > 0) {
      STEM_NAMES = data.stem_names;
      TRACK_NAMES = ["original", ...STEM_NAMES];
    }
    if (Array.isArray(data.extra_stem_names) && data.extra_stem_names.length > 0) {
      EXTRA_STEM_NAMES = data.extra_stem_names;
    }
  } catch (e) {
    console.warn("[constants] failed to sync stem names from API:", e);
  }
}

// The lane order for a specific job: STEM_NAMES with "vocals" replaced by
// lead_vocals + backing_vocals when a job's on-demand split (#275) has
// produced both. `presentNames` is the Set of stem names the job actually
// has (typically from job.stems). Order matters -- callers use this both to
// decide what to render and in what sequence (waveform stacking, mixer rows).
export function effectiveStemOrder(presentNames) {
  const splitDone = presentNames.has("lead_vocals") && presentNames.has("backing_vocals");
  const stems = STEM_NAMES.flatMap((n) => (n === "vocals" && splitDone ? EXTRA_STEM_NAMES : [n]));
  // Recording tracks follow the stems, in the user's track order.
  return [...stems, ...TAKE_NAMES.filter((n) => presentNames.has(n))];
}

// Recording tracks are lanes like any stem. The lane name is the track id
// (`k` plus 8 hex digits, the backend's track id), the label is the user's
// own text, and the audio is the track's active take. Set per job by
// static/js/capture/takeLanes.js. TAKE_ID_RE is the take id inside a track.
export const TAKE_ID_RE = /^t[0-9a-f]{8}$/;
export const TRACK_ID_RE = /^k[0-9a-f]{8}$/;
export const TAKE_COLOR = "#5ec8c0";
export let TAKE_NAMES = [];
const _takeLabels = new Map();

/** True for a recording-track lane name. */
export function isTakeName(name) {
  return typeof name === "string" && TRACK_ID_RE.test(name);
}

/** Replace the current job's track lanes: [{id, label}] in track order. */
export function setTakeLanes(takes) {
  for (const id of TAKE_NAMES) delete STEM_COLORS[id];
  _takeLabels.clear();
  TAKE_NAMES = [];
  for (const take of takes || []) {
    if (!isTakeName(take?.id)) continue;
    TAKE_NAMES.push(take.id);
    _takeLabels.set(take.id, take.label || take.id);
    STEM_COLORS[take.id] = TAKE_COLOR;
  }
}

/** Display name of any lane: a stem's translated name, or a take's label. */
export function laneLabel(name) {
  return STEM_DISPLAY[name] || name;
}

// A Proxy, not a plain object, so every lookup resolves through the CURRENT
// language live -- a plain object would freeze these labels in whatever
// language was active the moment this module first loaded. Callers keep their
// existing `STEM_DISPLAY[name] || name` fallback pattern unchanged: an
// unrecognized name (not one of ours) still yields undefined, same as a plain
// object would, rather than the i18n engine's own missing-key fallback
// (which returns the raw dictionary key string -- wrong here).
export const STEM_DISPLAY = new Proxy({}, {
  get(_target, prop) {
    if (typeof prop !== "string") return undefined;
    if (_takeLabels.has(prop)) return _takeLabels.get(prop);
    const key = `stem.${prop}`;
    return key in TRANSLATIONS.en ? t(key) : undefined;
  },
});

// FL Studio-style channel palette: saturated but slightly dusty, designed
// to read well on a dark background.
export const STEM_COLORS = {
  vocals: "#e85f6f",
  drums: "#e89048",
  bass: "#e8b848",
  guitar: "#88d878",
  piano: "#b88fe8",
  other: "#88a8c8",
  original: "#a8b0bd",
  lead_vocals: "#e8748a",
  backing_vocals: "#c98fe0",
};

export const PROGRESS_COLOR = "#3a3a3a";

export const LOOP_DEFAULT_START_FRAC = 0.25;
export const LOOP_DEFAULT_END_FRAC = 0.5;

export const LANE_VOLUME_MAX = 2;