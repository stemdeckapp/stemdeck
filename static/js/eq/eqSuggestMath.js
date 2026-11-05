// Suggest EQ, the pure part: the request URL, the server's answer turned into
// an ordinary EQ descriptor (graphical panels) or knob values (Bass, Guitar,
// Keys), the loudness-matched "current" for an A/B preview, and the reasons
// as text. No DOM, so tests/js/eq-suggest.test.mjs runs it in node.
//
// The server (GET suggest/eq, suggest/master) measures and fits; this only
// translates. A suggestion is never applied by itself: the panel shows it as
// a ghost curve, previews it on request and applies it through the panel's
// undoable edit state.

import { EQ_VERSION, encodeFxParam, normalizeEq } from "../eqMath.js";
import { fmtDb, fmtFreq } from "./eqGraphMath.js";
import {
  KEYS, LEVEL_MAX, LEVEL_MIN, keysDefaults, normalizeKeys, normalizePedal, panelEq, pedalDefaults,
} from "./eqPedalMath.js";

/** Lane id of the master slot (eqEngine.MASTER, repeated to keep this module DOM-free). */
export const MASTER_LANE = "__master";

const PANEL_KINDS = new Set(["graph", "drum", "bass", "guitar", "keys"]);
const MERGE_KINDS = new Set(["graph", "drum"]);

/** Whether a panel kind offers Suggest (the voice placeholder has no EQ). */
export function canSuggest(kind) {
  return PANEL_KINDS.has(kind);
}

/** Whether "Add to my EQ" is offered: graphical panels and the master. */
export function canMerge(kind, lane) {
  return lane === MASTER_LANE || MERGE_KINDS.has(kind);
}

/**
 * The request for a suggestion.
 * Lane: {jobId, lane, kind, target, mode, laneEq}. Master: {jobId, lane:
 * MASTER_LANE, target, mode, mixQuery} where mixQuery is the export's own
 * query (stems, gains, pitches and fx with the lane EQs and the master EQ).
 */
export function suggestUrl({ jobId, lane, kind, target = "", mode = "replace", laneEq = null, mixQuery = "" }) {
  const job = encodeURIComponent(jobId);
  if (lane === MASTER_LANE) {
    const q = new URLSearchParams(mixQuery);
    if (target) q.set("target", target);
    q.set("mode", mode);
    return `/api/jobs/${job}/suggest/master?${q}`;
  }
  const q = new URLSearchParams({ lane, layout: kind, mode });
  if (target) q.set("target", target);
  const fx = laneEq ? encodeFxParam({ [lane]: laneEq }) : "";
  if (fx) q.set("fx", fx);
  return `/api/jobs/${job}/suggest/eq?${q}`;
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function abOnly(current) {
  const ab = current?.params?.ui?.ab;
  return ab ? { ab } : undefined;
}

/** The knob values a Bass or Guitar EQ takes for the answer's bands. */
export function pedalControls(kind, answer) {
  const c = { ...pedalDefaults(kind) };
  for (const b of answer.bands || []) {
    if (b.slot === "hpf") c.hpf = b.f;
    else if (b.slot === "ls") c.ls = b.g;
    else if (b.slot === "hs") c.hs = b.g;
    else if (b.slot === "low") { c.lf = b.f; c.lg = b.g; c.lq = b.q; }
    else if (b.slot === "mid") { c.mf = b.f; c.mg = b.g; c.mq = b.q; }
    else if (b.slot === "high") { c.hf = b.f; c.hg = b.g; c.hq = b.q; }
  }
  c.lvl = clamp(Number(answer.out) || 0, LEVEL_MIN, LEVEL_MAX);
  return normalizePedal(kind, c);
}

/** The control values a Keys EQ takes for the answer's bands. */
export function keysControls(answer) {
  const c = { ...keysDefaults() };
  for (const b of answer.bands || []) {
    if (b.slot === "lowcut") c.lowcut = b.f;
    else if (b.slot === "body") c.body = b.g;
    else if (b.slot === "warmth") c.warmth = b.g;
    else if (b.slot === "presence") { c.presence = b.g; c.presF = b.f; }
    else if (b.slot === "air") c.air = b.g;
    else if (b.slot === "tone") c.tone = b.g;
  }
  c.lvl = clamp(Number(answer.out) || 0, KEYS.level.min, KEYS.level.max);
  return normalizeKeys(c);
}

/**
 * The EQ descriptor a suggestion would leave on the lane. `current` is the
 * lane's EQ now; its A/B slot is kept. Applying turns the EQ on (a
 * suggestion is meant to be heard). Merge keeps every current band (on or
 * off) and adds the new ones; the output gain is the server's.
 */
export function descriptorFromAnswer(answer, kind, current, mode = answer?.mode || "replace") {
  const ui = abOnly(current);
  if (kind === "bass" || kind === "guitar") {
    return panelEq(kind, pedalControls(kind, answer), { bypass: false, ui: ui || {} });
  }
  if (kind === "keys") {
    return panelEq("keys", keysControls(answer), { bypass: false, ui: ui || {} });
  }
  const fresh = (answer.bands || []).map((b) => ({ on: true, type: b.type, f: b.f, g: b.g, q: b.q, slope: b.slope }));
  const bands = mode === "merge" ? [...(current?.params?.bands || []), ...fresh] : fresh;
  return normalizeEq({
    id: "eq", v: EQ_VERSION, bypass: false,
    params: { out: Number(answer.out) || 0, bands, ui },
  });
}

/**
 * The lane's current EQ for an A/B against the suggestion at the same
 * loudness: its output gain moved by the estimated difference (the
 * suggestion is loudness-neutral against the raw lane, the current EQ may
 * not be). A bypassed EQ plays as flat with that trim.
 */
export function matchedCurrent(current, answer) {
  const l = answer?.loudness || {};
  const trim = (Number(l.suggested_change) || 0) - (Number(l.current_change) || 0);
  const cur = normalizeEq(current);
  if (cur.bypass) {
    return normalizeEq({ id: "eq", v: EQ_VERSION, bypass: false, params: { out: trim, bands: [] } });
  }
  return normalizeEq({ ...cur, params: { ...cur.params, out: cur.params.out + trim } });
}

/** The answer's target choices as [{id, label}] (label translated). */
export function targetOptions(answer, t) {
  return (answer?.choices || []).map((c) => ({ id: c.id, label: t(c.label_key) }));
}

const DB_VARS = new Set(["g", "dev", "d", "out", "target", "want", "limit", "i", "plr"]);

function fmtNumber(v) {
  const x = Math.abs(v) < 0.05 ? 0 : v;
  return `${x < 0 ? "−" : ""}${Math.abs(x).toFixed(1)}`;
}

/**
 * One reason as text: {key, vars} from the server, numbers formatted here
 * (frequencies as "250 Hz", gains as "−3.0 dB", loudness as plain numbers),
 * a region as its translated name. `dev` is shown without its sign: the
 * key's wording says above or below.
 */
export function formatReason(item, t) {
  const vars = {};
  for (const [k, v] of Object.entries(item?.vars || {})) {
    if (k === "f") vars.f = fmtFreq(v);
    else if (k === "region") vars.region = t(`suggest.region.${v}`);
    else if (k === "dev") vars.dev = fmtNumber(Math.abs(v));
    else if (k === "g" || k === "d" || k === "out") vars[k] = fmtDb(v);
    else if (k === "before" || k === "after") vars[k] = fmtNumber(v);
    else if (DB_VARS.has(k)) vars[k] = fmtNumber(v);
    else vars[k] = v;
  }
  return t(item.key, vars);
}

/** Every reason of an answer, the master preset's last. */
export function reasonLines(answer, t) {
  const lines = (answer?.why || []).map((w) => formatReason(w, t));
  if (answer?.master?.why) lines.push(formatReason(answer.master.why, t));
  return lines;
}

/** The suggested loudness preset in one line ("Loudness: Streaming, −14.0 LUFS"), or "". */
export function presetLine(answer, t) {
  const m = answer?.master;
  if (!m) return "";
  const preset = t(`master.preset.${m.preset}`);
  if (m.target_lufs === null || m.target_lufs === undefined) return t("suggest.presetLineGentle", { preset });
  return t("suggest.presetLine", { preset, target: fmtNumber(m.target_lufs) });
}

/** The master descriptor's params a suggested preset sets (masterMath.js shape). */
export function masterParamsFromAnswer(answer) {
  const m = answer?.master;
  if (!m) return null;
  return {
    preset: m.preset,
    target_lufs: m.target_lufs,
    ceiling_dbtp: m.ceiling_dbtp,
    comp: m.comp,
  };
}
