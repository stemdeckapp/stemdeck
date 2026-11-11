// A live input's effects chain, as data: what an armed recording track's
// input is heard through while "Hear myself" is on (capture/inputFx.js).
// No DOM, no Web Audio, so tests/js/input-fx.test.mjs runs it in node.
//
// The chain is the same descriptor as a lane's (chainModel.js, schema v1),
// played by the same host and pedals (chainHost.js, pedals.js). What a live
// input adds is kept beside it:
//
//   { chain: <chain descriptor>, low: false }   // low: the Low latency switch
//
// stored per recording track in this browser (captureSettings.js), like the
// track's other per-machine recording choices. Hear myself itself is not
// stored: it is off at every page load, because a monitor that comes back on
// by itself next to a microphone and speakers can feed back.

import { normalizeChain, chainIsSet, chainLatencyFrames } from "./chainModel.js";

/** At most this many tracks keep a stored input chain; the oldest go first. */
export const INPUT_FX_MAX_TRACKS = 200;

/**
 * The instrument a track's input is voiced for by default, from the kind of
 * input it records (trackInputs.inputKind): a microphone is a voice, an
 * interface or line input is an instrument, and an instrument is a guitar
 * (the general pedal voicings) until the user picks Bass.
 */
export function inputDefaultInstrument(kind) {
  return kind === "voice" ? "voice" : "guitar";
}

/** A stored record made safe: the chain normalised, Low latency a boolean. */
export function normalizeInputFx(raw, defInst = "guitar") {
  const r = raw && typeof raw === "object" ? raw : {};
  return { chain: normalizeChain(r.chain, defInst), low: r.low === true };
}

/** True when a record is worth keeping: a chain set, or Low latency on. */
export function inputFxIsSet(rec) {
  return !!rec && (chainIsSet(rec.chain) || rec.low === true);
}

/** The stored map of track id -> {chain, low, at}, from its JSON. */
export function parseInputFxStore(raw) {
  try {
    const v = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (!v || typeof v !== "object" || Array.isArray(v)) return {};
    const out = {};
    for (const [id, rec] of Object.entries(v)) {
      if (/^k[0-9a-f]{8}$/.test(id) && rec && typeof rec === "object") out[id] = rec;
    }
    return out;
  } catch {
    return {};
  }
}

/**
 * The store with one track's record set (or removed when nothing is set),
 * stamped `now`, and at most INPUT_FX_MAX_TRACKS records: the least
 * recently changed go first.
 */
export function withInputFx(store, trackId, rec, now = Date.now()) {
  const next = { ...(store || {}) };
  if (inputFxIsSet(rec)) next[trackId] = { chain: rec.chain, low: rec.low === true, at: now };
  else delete next[trackId];
  const ids = Object.keys(next);
  if (ids.length > INPUT_FX_MAX_TRACKS) {
    ids.sort((a, b) => (Number(next[a].at) || 0) - (Number(next[b].at) || 0));
    for (const id of ids.slice(0, ids.length - INPUT_FX_MAX_TRACKS)) delete next[id];
  }
  return next;
}

/**
 * Whether a chain needs a host in the monitor path: any pedal (on or off)
 * or a trim. A chain switched off keeps its host, so switching it on again
 * crossfades instead of rewiring, and its echoes ring out when it goes off.
 */
export function inputHostWanted(desc) {
  return !!desc && (desc.pedals.length > 0 || desc.trim !== 0);
}

/**
 * The monitor routes for the armed tracks: one per distinct set of input
 * channels (two tracks on the same channel would play it twice, 6 dB too
 * loud), the first track's chain on it. `tracks`: [{id, channels, desc}] in
 * arm order. `allowed` is fxEngine.inputChainsAllowed(), `monitorOn` the
 * Hear myself switch. Returns [{key, id, channels, host}]: `host` says
 * whether a chain host is built. Nothing is built while monitoring is off
 * or outside the modes that allow it.
 */
export function planInputRoutes({ allowed, monitorOn, tracks }) {
  const out = [];
  const seen = new Set();
  for (const tr of tracks || []) {
    const chans = (tr.channels || []).slice(0, 2);
    const key = chans.join(",");
    if (!chans.length || seen.has(key)) continue;
    seen.add(key);
    out.push({ key: tr.id, id: tr.id, channels: chans, host: !!allowed && !!monitorOn && inputHostWanted(tr.desc) });
  }
  return out;
}

/**
 * How late a player hears themselves through the monitor: the round trip
 * (the loopback calibration when there is one, else the browser's
 * outputLatency plus baseLatency plus the input's reported latency) plus
 * what the chain adds. All in milliseconds; `measured` says which round
 * trip it is.
 */
export function monitorLatency({ roundTripSec, calibrated = false, chainFrames = 0, rate = 48000 }) {
  const rt = Number.isFinite(roundTripSec) && roundTripSec >= 0 ? roundTripSec * 1000 : null;
  const fxMs = rate > 0 && chainFrames > 0 ? (chainFrames / rate) * 1000 : 0;
  return { roundTripMs: rt, fxMs, totalMs: rt === null ? null : rt + fxMs, measured: !!calibrated };
}

/** The chain's added latency on a live input, in frames (Low latency counted). */
export function inputChainFrames(rec, facts) {
  if (!rec || !inputHostWanted(rec.chain)) return 0;
  return chainLatencyFrames(rec.chain, facts, { lowLatency: rec.low === true });
}

// Names an output device has when it is headphones, in the words Windows,
// macOS and Linux use (and a few languages of the device names).
const HEADPHONES = /head ?phones?|headset|ear ?phones?|ear ?buds?|airpods|\bbuds\b|in-ear|kopfh|auriculares|casque|[ée]couteurs|fones? de ouvido|auscultadores|s[łl]uchawki|ヘッドホン|ヘッドフォン|イヤホン|耳机|耳機|헤드폰|헤드셋/i;
// Audio interfaces and mixers. Windows names their outputs "Speakers (...)"
// whatever is plugged into them (headphones on the interface's own jack are
// the usual case), so their name says nothing about speakers.
const INTERFACE = /focusrite|scarlett|clarett|vocaster|universal audio|\buad\b|apollo|\bvolt\b|motu|presonus|audiobox|steinberg|\bur\d|behringer|\bumc\d|u-phoria|mg-?xu|\bag0?\d\b|yamaha|audient|\bevo\b|\bssl\b|\brme\b|babyface|fireface|tascam|roland|rubix|line ?6|helix|neural dsp|quad cortex|fractal|axe-?fx|kemper|irig|arturia|minifuse|komplete audio|m-audio|m-track|mackie|apogee|antelope|zoom\b|rodecaster|goxlr|tc-helicon|\besi\b|maono/i;
// What plays into a room: built-in and desktop speakers, a TV or monitor
// screen over HDMI or DisplayPort.
const SPEAKERS = /speakers?|lautsprecher|altavoces|haut-parleurs?|alto-falantes?|g[łl]o[śs]niki|hdmi|displayport|display audio|\btv\b|スピーカー|扬声器|揚聲器|스피커/i;

/**
 * What an output device probably is, from its name: "headphones",
 * "interface" (its name says nothing either way), "speakers", or
 * "unknown" (no name: the browser gives none before permission).
 */
export function outputKind(label) {
  const s = String(label || "").replace(/^(default|communications)\s*-\s+/i, "").trim();
  if (!s) return "unknown";
  if (HEADPHONES.test(s)) return "headphones";
  if (INTERFACE.test(s)) return "interface";
  if (SPEAKERS.test(s)) return "speakers";
  return "unknown";
}

/**
 * The output the page plays to: the system default (the page never picks
 * another, there is no setSinkId), from enumerateDevices' audiooutput list.
 */
export function defaultOutputLabel(devices) {
  const outs = (devices || []).filter((d) => d && (!d.kind || d.kind === "audiooutput"));
  const def = outs.find((d) => d.deviceId === "default") || outs[0];
  return def?.label || "";
}
