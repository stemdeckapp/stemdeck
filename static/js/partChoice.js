// A mode's stem choice for a song, kept per song and per mode: the choice of
// one mode never changes what another shows or plays.
//
// Record key: stemdeck:stems:<jobId>:<mode>. Two shapes, both readable:
//   v1 { v: 1, remove }                  Streamer's "I play" (the first user)
//   v2 { v: 2, remove, lanes?, voice? }  plus the stem choice of a mode
// `remove` is one of the six Demucs stems, or "" for the whole song.
// `lanes` (Practice and Advanced) is the base stems shown as lanes of their
// own; the others are folded into Original. Absent: the song's own selection.
// `voice` is "combined" (one Vocals lane) or "split" (Lead + Backing once the
// split exists). Absent: today's rule, the pair whenever the split exists.
// Nothing is written until a mode's choice is changed, so a song opens in
// every mode exactly as before. Pure: no DOM, no store, testable in Node.

import { scopeFor } from "./versionScope.js";

export const CHOICE_VERSION = 1;
export const CHOICE_VERSION_LANES = 2;

/** What can be left out: "" (nothing, the whole song) and the parts a
 * musician plays. "other" is not offered: nobody plays "other". */
export const STEM_CHOICES = Object.freeze(["", "drums", "bass", "guitar", "piano", "vocals"]);

/** The six Demucs stems (app/core/config.py STEM_NAMES). */
export const BASE_STEMS = Object.freeze(["vocals", "drums", "bass", "guitar", "piano", "other"]);

const MODE_RE = /^[a-z][a-z0-9-]{0,23}$/;

/** The store key of `jobId`'s choice in `mode`, in the song's open version
 * or in `versionId` (versionScope.js: the default version keeps the key
 * every song had). */
export function choiceKey(jobId, mode, versionId) {
  if (!jobId || !MODE_RE.test(String(mode || ""))) throw new Error("a job and a mode are required");
  return `stemdeck:stems:${versionId === undefined ? scopeFor(jobId) : scopeFor(jobId, versionId)}:${mode}`;
}

export const VOICE_FORMS = Object.freeze(["combined", "split"]);

/** A stored choice made safe: anything unknown is the whole song. `lanes`
 * and `voice` are only present when the record holds a valid one; `lanes`
 * comes back in BASE_STEMS order, each stem once. */
export function parseChoice(raw) {
  let data = raw;
  if (typeof data === "string") {
    try { data = JSON.parse(data); } catch { data = null; }
  }
  const obj = data && typeof data === "object" ? data : null;
  const remove = obj && STEM_CHOICES.includes(obj.remove) ? obj.remove : "";
  const out = { remove };
  if (obj && Array.isArray(obj.lanes)) out.lanes = BASE_STEMS.filter((n) => obj.lanes.includes(n));
  if (obj && VOICE_FORMS.includes(obj.voice)) out.voice = obj.voice;
  return out;
}

/** The record to store: v1 when it holds only `remove` (what Streamer has
 * always written), v2 when it holds a stem choice too. */
export function serializeChoice(choice) {
  const c = parseChoice(choice);
  if (!c.lanes && !c.voice) return { v: CHOICE_VERSION, remove: c.remove };
  return { v: CHOICE_VERSION_LANES, ...c };
}

/** `stored` with `patch` applied; a key set to null is removed. */
export function mergeChoice(stored, patch) {
  const next = { ...parseChoice(stored) };
  for (const [k, v] of Object.entries(patch || {})) {
    if (v == null) delete next[k];
    else next[k] = v;
  }
  return parseChoice(next);
}

// ── Lanes per mode ──

/** The modes with a stem layout of their own (mode strings of mixMode.js). */
export const STEM_MODES = Object.freeze(["practice", "daw", "karaoke", "streamer"]);
/** Modes whose user chooses the lanes (the Stems control). */
export const LANE_MODES = Object.freeze(["practice", "daw"]);
/** Modes whose user chooses the voice form. */
export const VOICE_MODES = Object.freeze(["practice", "daw", "karaoke"]);
/** Karaoke's lanes, always: the voice; every instrument is in Original. */
export const KARAOKE_LANES = Object.freeze(["vocals"]);

/** The song's own lanes: its selection at import (job.selected_stems), in
 * BASE_STEMS order. None recorded reads as all six, as the server does. */
export function songDefaultLanes(selected) {
  const own = BASE_STEMS.filter((n) => (selected || []).includes(n));
  return own.length ? own : [...BASE_STEMS];
}

/**
 * The layout a mode shows for a song: { lanes, voice }. `voice` is null for
 * "today's rule". Practice and Advanced read their record; Karaoke always
 * has the voice as its one lane (its own voice form); Streamer and any other
 * mode show the song's own selection.
 */
export function modeLayout(mode, choice, songSelected) {
  const c = parseChoice(choice);
  const lanes = mode === "karaoke"
    ? [...KARAOKE_LANES]
    : LANE_MODES.includes(mode) && c.lanes ? c.lanes : songDefaultLanes(songSelected);
  const voice = VOICE_MODES.includes(mode) || c.voice ? c.voice || null : null;
  return { lanes, voice };
}

/** True when `layout` is the song's own (nothing chosen differs from it). */
export function isSongDefault(layout, songSelected) {
  return sameNames(new Set(layout.lanes), new Set(songDefaultLanes(songSelected))) && !layout.voice;
}

/**
 * What a layout shows for a song whose stem files are `present` (names with
 * a URL): the dedicated lanes in display order, Voice as Lead + Backing when
 * the split exists and the voice is not "combined"; and the base stems that
 * make up Original (every present base stem that is not a lane).
 * Returns { lanes: [names], original: [base stems], split: bool }.
 */
export function layoutLanes(layout, present) {
  const have = present instanceof Set ? present : new Set(present || []);
  const splitDone = have.has("lead_vocals") && have.has("backing_vocals");
  const want = new Set(layout?.lanes || []);
  const lanes = [];
  let split = false;
  for (const name of BASE_STEMS) {
    if (!want.has(name)) continue;
    if (name === "vocals") {
      const usePair = splitDone && (layout.voice !== "combined" || !have.has("vocals"));
      if (usePair) { lanes.push("lead_vocals", "backing_vocals"); split = true; }
      else if (have.has("vocals")) lanes.push("vocals");
      continue;
    }
    if (have.has(name)) lanes.push(name);
  }
  const original = BASE_STEMS.filter((n) => !want.has(n) && have.has(n));
  return { lanes, original, split };
}

/** The base stems a set of lane names stands for (Lead + Backing is Voice). */
export function baseLanes(names) {
  const set = new Set(names || []);
  if (set.has("lead_vocals") || set.has("backing_vocals")) set.add("vocals");
  return BASE_STEMS.filter((n) => set.has(n));
}

/** The canonical spelling of a rest set (app/pipeline/backing.py rest_name). */
export function restSetName(names) {
  return [...new Set(names)].sort().join("-");
}

/**
 * Where Original's one file comes from for `components` (base stems):
 * { kind: "none" } when it holds nothing; the job's own original.wav when it
 * holds exactly the job's complement (`jobOriginal`: base stems, or null when
 * the job has no original.wav); a stem's own file for one stem; else the
 * rest file the server builds once. Never a file that holds a laned stem.
 */
export function originalSource(jobId, components, jobOriginal, urls = {}) {
  const comps = BASE_STEMS.filter((n) => (components || []).includes(n));
  if (!comps.length) return { kind: "none", url: null };
  if (jobOriginal && urls.original && sameNames(new Set(comps), new Set(jobOriginal))) {
    return { kind: "job", url: urls.original };
  }
  if (comps.length === 1 && urls[comps[0]]) return { kind: "stem", url: urls[comps[0]] };
  return { kind: "rest", url: `/api/jobs/${encodeURIComponent(jobId)}/rest/${restSetName(comps)}.wav` };
}

/** The base stems in the job's own original.wav (collect.py: every stem not
 * selected), or null when the job has none. */
export function jobOriginalSet(songSelected, hasOriginal) {
  if (!hasOriginal) return null;
  const sel = new Set(songSelected || []);
  return BASE_STEMS.filter((n) => !sel.has(n));
}

/** One string per shown layout, to tell whether a switch must reload. */
export function layoutSignature(shown) {
  return `${(shown?.lanes || []).join(",")}|${(shown?.original || []).join(",")}`;
}

/**
 * Before a lead/backing split starts from `mode`: the records to write so
 * only that mode sees the pair. Every other mode with no voice of its own is
 * frozen at one Vocals lane ("combined"); `mode` asks for "split".
 * `records`: { mode: stored record or null }. Returns { mode: new record }.
 */
export function splitFreezePatches(mode, records) {
  const out = {};
  for (const m of STEM_MODES) {
    const stored = records?.[m] ?? null;
    if (m === mode) out[m] = serializeChoice(mergeChoice(stored, { voice: "split" }));
    else if (!parseChoice(stored).voice) out[m] = serializeChoice(mergeChoice(stored, { voice: "combined" }));
  }
  return out;
}

/** The choices a song offers: the whole song always, and each part whose
 * stem file it has (`present`: stem names with a file). */
export function offeredChoices(present) {
  const have = new Set(present || []);
  return STEM_CHOICES.filter((c) => c === "" || have.has(c));
}

/** A choice the song can no longer offer reads as the whole song. */
export function effectiveChoice(choice, present) {
  const remove = parseChoice(choice).remove;
  return offeredChoices(present).includes(remove) ? remove : "";
}

/**
 * The lanes and playback parts a removed stem takes out: Voice is the vocals
 * stem and, after a lead/backing split, both of its halves.
 */
export function excludedNames(remove) {
  if (!remove) return new Set();
  if (remove === "vocals") return new Set(["vocals", "lead_vocals", "backing_vocals"]);
  return new Set([remove]);
}

/** Two sets hold the same names. */
export function sameNames(a, b) {
  if (a.size !== b.size) return false;
  for (const n of a) if (!b.has(n)) return false;
  return true;
}

/**
 * The take timeline's two lanes for a choice: the backing track (every base
 * stem the song has but the removed one) and the part the musician plays.
 * Returns { backing: [stem names], part: stem name or null }.
 */
export function timelineLanes(remove, present) {
  const have = new Set(present || []);
  const out = excludedNames(remove);
  return {
    backing: BASE_STEMS.filter((n) => have.has(n) && !out.has(n)),
    part: remove && have.has(remove) ? remove : null,
  };
}

// Why a stem choice was not applied (modeStems.js changeLayout, requestSplit)
// mapped to the i18n key the Stems popover and the version actions show. "" (applied) has no refusal text; a reason not listed
// here still says nothing changed, never that it did (#793).
const REASON_KEYS = Object.freeze({
  recording: "stems.blockedRecording",
  noSong: "stems.noSong",
  failed: "stems.splitFailed",
  empty: "stems.empty",
  mode: "stems.noLanes",
});

/** The i18n key for a refused stem change, "" when `reason` is "". */
export function stemsReasonKey(reason) {
  if (!reason) return "";
  return REASON_KEYS[reason] || "stems.notChanged";
}
