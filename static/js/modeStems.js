// Stem choice per mode (docs/plans/0.21.0/mode-stem-choice.md).
//
// Each mode keeps its own stem layout for a song: which stems are lanes of
// their own and which are folded into Original (Practice and Advanced choose
// them; Karaoke always has the voice as its one lane), and the voice form
// (one Vocals lane, or Lead + Backing). What one mode chooses is never seen in
// another: the record is stemdeck:stems:<jobId>:<mode> (partChoice.js), the
// same record Streamer keeps its "I play" in.
//
// Nothing is ever separated again: all six Demucs stems are on disk for every
// song, so a layout only changes what is a lane. The one optional stem pair,
// Lead + Backing, comes from the existing idempotent split endpoint, run once
// per song; before it starts, every other mode with no voice of its own is
// frozen at one Vocals lane, so only the mode that asked sees the pair.
//
// A layout is read before the song's lanes are built (catalog.js and job.js
// call prepareLayout before wireUpAudio). A change reloads the song with the
// playhead and loop kept, as Streamer's "I play" does; a switch of mode
// reloads only when the two modes show different lanes.

import { storeGet, storeSet } from "./utils.js";
import { currentJobId } from "./state.js";
import { setLaneLayout, shownLaneLayout } from "./player.js";
import { mixModeInForce, onMixModeSettled } from "./mixMode.js";
import { reloadKeepingPlace } from "./reloadPlace.js";
import { noteSplitOutcome, splitReason } from "./vocalSplitState.js";
import { scopeFor, activeVersionStems, songVersions } from "./versionScope.js";
import { dismissFailuresByJobId } from "./notifications.js";
import {
  BASE_STEMS, choiceKey, parseChoice, serializeChoice, mergeChoice, modeLayout, layoutLanes,
  layoutSignature, songDefaultLanes, splitFreezePatches, STEM_MODES, LANE_MODES, VOICE_MODES,
} from "./partChoice.js";

// The open song as the last prepareLayout saw it: { jobId, songSelected,
// versionStems (the stems of its open version, versionScope.js, or null),
// present (stem names with a file), splitState, splitReason (why the last
// split failed, a code from vocalSplitState.js, or null) }.
let song = { jobId: null, songSelected: [], versionStems: null, present: [], splitState: "none", splitReason: null };
// Every mode's record for the open song in its open version (`scope`,
// versionScope.js), as read or last written.
let records = { scope: null, byMode: {} };
const sameScope = (jobId) => Boolean(jobId) && records.scope === scopeFor(jobId);
// The lanes a mode shows when it has chosen none: the open version's stems
// (#804), which for a song's first version are its selection at import.
const defaultLanes = () => song.versionStems || song.songSelected;
const listeners = new Set();
// Refusals the UI owns (recording, a take open): set by whoever can answer.
let busyCheck = () => "";

/** `fn()` returns a reason the lanes may not change now ("" when they may):
 * a take recording or a Streamer take open. */
export function setLayoutBusyCheck(fn) {
  busyCheck = typeof fn === "function" ? fn : () => "";
}

/** Called whenever the open song's layout or records change. */
export function onLayoutChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

function notify() {
  for (const cb of listeners) {
    try { cb(); } catch (err) { console.warn("[modeStems] listener failed:", err); }
  }
}

async function readRecord(jobId, mode) {
  try {
    return await storeGet(choiceKey(jobId, mode), null);
  } catch (err) {
    console.warn("[modeStems] reading the stem choice failed:", err);
    return null;
  }
}

async function writeRecord(jobId, mode, record) {
  if (sameScope(jobId)) records.byMode[mode] = record;
  await storeSet(choiceKey(jobId, mode), record);
}

/**
 * Read the layout of the mode in force for `jobId` and hand it to the player
 * for the load that follows. `songSelected`: the song's selection at import;
 * `present`: the names of the stems it has a file for; `splitState`: its
 * vocal_split ("none", "running", "done", "error"); `splitReason`: its
 * vocal_split_reason when that is "error" (#832). Resolves the layout.
 */
export async function prepareLayout(jobId, songSelected, present, splitState = "none", splitReason = null) {
  const mode = mixModeInForce();
  if (!sameScope(jobId)) records = { scope: scopeFor(jobId), byMode: {} };
  const stored = await readRecord(jobId, mode);
  records.byMode[mode] = stored;
  song = {
    jobId,
    songSelected: [...(songSelected || [])],
    versionStems: activeVersionStems(jobId),
    present: [...(present || [])],
    splitState: splitState || "none",
    splitReason: splitState === "error" ? splitReason || null : null,
  };
  const layout = modeLayout(mode, stored, defaultLanes());
  setLaneLayout(jobId, { ...layout, songSelected: song.songSelected });
  notify();
  return layout;
}

/** What the Stems control shows for the open song in the mode in force:
 * { jobId, mode, lanes, voice, chosen (a record exists), canChooseLanes,
 * canChooseVoice, present, splitState, splitReason, songDefault }. */
export function layoutState() {
  const mode = mixModeInForce();
  const jobId = song.jobId === currentJobId ? song.jobId : null;
  const stored = sameScope(jobId) ? records.byMode[mode] ?? null : null;
  const layout = modeLayout(mode, stored, defaultLanes());
  const c = parseChoice(stored);
  return {
    jobId,
    mode,
    lanes: layout.lanes,
    voice: layout.voice,
    chosen: Boolean(c.lanes || c.voice),
    canChooseLanes: LANE_MODES.includes(mode),
    canChooseVoice: VOICE_MODES.includes(mode),
    present: [...song.present],
    splitState: song.splitState,
    splitReason: song.splitReason,
    songDefault: songDefaultLanes(defaultLanes()),
    shown: layoutLanes(layout, new Set(song.present)),
  };
}

/** Why the lanes cannot change now ("" when they can). */
export function layoutBlockedReason() {
  if (!song.jobId || song.jobId !== currentJobId) return "noSong";
  return busyCheck() || "";
}

/**
 * Change the open mode's choice: `patch` is { lanes?, voice? } (null removes
 * a key; { lanes: null, voice: null } is "use the song's stems"). Saved for
 * this song in this mode only, then the song reloads with its place kept.
 * Resolves "" when applied, else the reason it was refused.
 */
export async function changeLayout(patch) {
  const why = layoutBlockedReason();
  if (why) return why;
  const jobId = song.jobId;
  const mode = mixModeInForce();
  if (patch && "lanes" in patch && patch.lanes) {
    patch = { ...patch, lanes: BASE_STEMS.filter((n) => patch.lanes.includes(n)) };
    if (!patch.lanes.length) return "empty";
  }
  if (patch && "lanes" in patch && !LANE_MODES.includes(mode)) return "mode";
  const stored = sameScope(jobId) && mode in records.byMode
    ? records.byMode[mode]
    : await readRecord(jobId, mode);
  const next = serializeChoice(mergeChoice(stored, patch));
  await writeRecord(jobId, mode, next);
  if (jobId !== currentJobId) return "noSong";
  applyIfChanged(jobId, mode);
  notify();
  return "";
}

/** Reload the open song when the layout of `mode` shows other lanes than
 * the ones on screen. Returns whether a reload was asked for. */
function applyIfChanged(jobId, mode) {
  const stored = sameScope(jobId) ? records.byMode[mode] ?? null : null;
  const layout = modeLayout(mode, stored, defaultLanes());
  const want = layoutSignature(layoutLanes(layout, new Set(song.present)));
  const shown = shownLaneLayout();
  setLaneLayout(jobId, { ...layout, songSelected: song.songSelected });
  if (shown && shown.jobId === jobId && shown.sig === want) return false;
  reloadKeepingPlace(jobId);
  return true;
}

/**
 * Split the open song's vocals into Lead + Backing for the mode in force.
 * Every other mode without a voice of its own is first frozen at one Vocals
 * lane (written before the split starts), so none of them changes. The split
 * runs once per song on the server; asking again is answered from it.
 * Resolves "" when done, else a reason.
 */
export async function requestSplit() {
  const why = layoutBlockedReason();
  if (why) return why;
  const jobId = song.jobId;
  const mode = mixModeInForce();
  if (!VOICE_MODES.includes(mode)) return "mode";
  const current = {};
  for (const m of STEM_MODES) {
    current[m] = sameScope(jobId) && m in records.byMode ? records.byMode[m] : await readRecord(jobId, m);
  }
  const patches = splitFreezePatches(mode, current);
  for (const [m, rec] of Object.entries(patches)) await writeRecord(jobId, m, rec);
  if (song.splitState === "done") {
    applyIfChanged(jobId, mode);
    notify();
    return "";
  }
  // The song's other versions (#804) see no pair either: every mode of
  // theirs without a voice of its own stays at one Vocals lane.
  const open = scopeFor(jobId);
  await freezeVoiceInVersions(jobId, songVersions(jobId).map((v) => v.id).filter((id) => scopeFor(jobId, id) !== open));
  song = { ...song, splitState: "running", splitReason: null };
  notify();
  // Why it failed, from the server (#832): the popover and Karaoke say it.
  const [ok, reason] = await runVocalSplit(jobId);
  if (song.jobId === jobId) {
    song = { ...song, splitState: ok ? "done" : "error", splitReason: ok ? null : reason };
  }
  notify();
  if (!ok) return "failed";
  // The new stems are on disk: the song reloads from fresh state and shows
  // the pair in this mode only.
  if (jobId === currentJobId) reloadKeepingPlace(jobId);
  return "";
}

/** A split another window started: wait for it (at most 15 minutes).
 * Resolves [done, the reason when it was not]. */
async function waitForSplit(jobId) {
  const until = Date.now() + 15 * 60 * 1000;
  while (Date.now() < until) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
      const state = res.ok ? await res.json() : null;
      if (state?.vocal_split === "done") return [true, null];
      if (state?.vocal_split !== "running") return [false, splitReason(state?.vocal_split_reason)];
    } catch (err) {
      console.warn("[modeStems] reading the split state failed:", err);
    }
  }
  return [false, "timeout"];
}

/**
 * Freeze every mode of the versions `versionIds` of `jobId` that has no
 * voice of its own at one Vocals lane, before a Lead + Backing split starts:
 * a split asked for in one version never changes what another shows.
 */
export async function freezeVoiceInVersions(jobId, versionIds) {
  for (const vid of versionIds || []) {
    const current = {};
    for (const m of STEM_MODES) {
      try {
        current[m] = await storeGet(choiceKey(jobId, m, vid), null);
      } catch (err) {
        console.warn("[modeStems] reading a version's stem choice failed:", err);
        current[m] = null;
      }
    }
    // "" asks for the pair in no mode: all of them are frozen.
    for (const [m, rec] of Object.entries(splitFreezePatches("", current))) {
      await storeSet(choiceKey(jobId, m, vid), rec);
    }
  }
}

/**
 * Run the song's Lead + Backing split (idempotent on the server) without
 * touching any record. Resolves [done, the reason when it was not].
 */
export async function runVocalSplit(jobId) {
  let ok = false;
  let reason = "worker_failed";
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/vocal-split`, { method: "POST" });
    ok = res.ok || res.status === 202;
    if (res.status === 409) [ok, reason] = await waitForSplit(jobId);
    else if (!ok) reason = splitReason((await res.json().catch(() => ({})))?.vocal_split_reason);
  } catch (err) {
    console.warn("[modeStems] the vocal split failed:", err);
  }
  noteSplitOutcome(ok ? null : reason);
  if (ok) dismissFailuresByJobId(jobId, "split");
  return [ok, ok ? null : reason];
}

// ── Reload with the place kept ──

// One helper for every reload of the open song that the user did not ask
// for by choosing a song: here, Karaoke's lanes and Streamer's "I play".
export { reloadKeepingPlace };

// ── Mode switches ──

/** Reload when the mode now in force shows other lanes than the ones on
 * screen. Practice and Advanced with the same lanes never reload. */
async function onModeSettled(mode) {
  const jobId = currentJobId;
  if (!jobId || song.jobId !== jobId) return;
  if (!sameScope(jobId)) records = { scope: scopeFor(jobId), byMode: {} };
  if (!(mode in records.byMode)) records.byMode[mode] = await readRecord(jobId, mode);
  if (jobId !== currentJobId || mode !== mixModeInForce()) return;
  // A take recording keeps the lanes it began with; the next load follows.
  if (busyCheck()) return;
  applyIfChanged(jobId, mode);
  notify();
}

export function initModeStems() {
  onMixModeSettled((mode) => { onModeSettled(mode); });
}
