// Versions of a song (#804): make, rename, delete and open them, and the
// Extract button on a song that is already separated.
//
// A version is a named choice of stems with its own saved mix, over the one
// set of stem files the song has (app/core/versions.py, versionScope.js).
// The server keeps the list and which version opens with the song; the mix
// and layout of each mode are the page's own records, under the version's
// scope. Nothing here ever deletes or copies a stem file.
//
// Announces "stemdeck:versions-changed" ({ jobId }) whenever a song's list or
// its open version changes, for the library rows and the Stems popover.

import { currentJobId } from "./state.js";
import { storeGet, storeSet } from "./utils.js";
import { readSavedMix, getMixMode, mixKey } from "./mixer.js";
import { laneLabel } from "./constants.js";
import { t } from "./i18n.js";
import {
  BASE_STEMS, choiceKey, LANE_MODES, mergeChoice, STEM_MODES, serializeChoice, stemsReasonKey,
} from "./partChoice.js";
import {
  activeVersionId, nextVersionNumber, setSongVersions, songVersions, versionWithStems,
} from "./versionScope.js";
import { freezeVoiceInVersions, layoutBlockedReason, layoutState, runVocalSplit } from "./modeStems.js";
import { reloadKeepingPlace } from "./reloadPlace.js";
import { splitFailedText } from "./vocalSplitState.js";
import { openTrackInStudio } from "./catalog.js";

function announce(jobId) {
  document.dispatchEvent(new CustomEvent("stemdeck:versions-changed", { detail: { jobId } }));
}

/** Take what the server says of a song's versions (a job state, or a
 * versions endpoint's answer). */
export function applyServerVersions(jobId, versions, active) {
  if (!jobId || !Array.isArray(versions)) return;
  setSongVersions(jobId, versions, active);
  announce(jobId);
}

async function api(path, init) {
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(typeof data?.detail === "string" ? data.detail : res.statusText);
    err.status = res.status;
    throw err;
  }
  return data;
}

const jsonInit = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body ?? {}),
});

const base = (jobId) => `/api/jobs/${encodeURIComponent(jobId)}/versions`;

/** What a version is called: its name, else "Version N" by its place. */
export function versionLabel(version, index) {
  return version?.name || t("versions.unnamed", { n: index + 1 });
}

/** The stems a version shows, as a short line ("Vocals, Drums, Bass"). */
export function versionStemsText(stems) {
  const list = BASE_STEMS.filter((n) => (stems || []).includes(n));
  if (!list.length || list.length === BASE_STEMS.length) return t("versions.allStems");
  return list.map(laneLabel).join(", ");
}

/**
 * Make a version of `jobId` showing `lanes` and open it. `voice` ("split"
 * or "combined") is written for the modes that choose lanes; `copyMix`
 * starts it from the mix of the version open now, in every mode, else from
 * the defaults. Resolves the new version.
 */
export async function createVersion(jobId, { name = null, lanes, voice = null, copyMix = false } = {}) {
  const from = activeVersionId(jobId);
  const stems = BASE_STEMS.filter((n) => (lanes || []).includes(n));
  const mixes = {};
  if (copyMix) {
    for (const mode of STEM_MODES) mixes[mode] = await readSavedMix(jobId, mode, from);
  }
  const data = await api(base(jobId), jsonInit("POST", { name, stems, select: false }));
  const vid = data.version.id;
  // Its records are written before it is chosen, so it opens with them.
  for (const [mode, mix] of Object.entries(mixes)) {
    if (mix) await storeSet(mixKey(jobId, mode, vid), mix);
  }
  if (voice) {
    for (const mode of LANE_MODES) await storeSet(choiceKey(jobId, mode, vid), serializeChoice({ voice }));
  }
  applyServerVersions(jobId, data.versions, data.active_version);
  await openVersion(jobId, vid);
  return data.version;
}

/** Rename a version. Resolves "" when done, else the server's reason. */
export async function renameVersion(jobId, versionId, name) {
  try {
    const data = await api(`${base(jobId)}/${encodeURIComponent(versionId)}`, jsonInit("PATCH", { name }));
    applyServerVersions(jobId, data.versions, data.active_version);
    return "";
  } catch (err) {
    console.warn("[versions] rename failed:", err);
    return err.message || "failed";
  }
}

/**
 * Delete a version: its saved mix and layout go, the song's stems stay (every
 * version shares them). The last version is never deleted. When it was the
 * open one, the song opens the version the server opens now.
 */
export async function deleteVersion(jobId, versionId) {
  const wasOpen = activeVersionId(jobId) === versionId;
  let data;
  try {
    data = await api(`${base(jobId)}/${encodeURIComponent(versionId)}`, { method: "DELETE" });
  } catch (err) {
    console.warn("[versions] delete failed:", err);
    return err.message || "failed";
  }
  for (const mode of STEM_MODES) {
    try {
      await storeSet(mixKey(jobId, mode, versionId), null);
      await storeSet(choiceKey(jobId, mode, versionId), null);
    } catch (err) {
      console.warn("[versions] clearing a deleted version's records failed:", err);
    }
  }
  applyServerVersions(jobId, data.versions, data.active_version);
  if (wasOpen && jobId === currentJobId) reloadKeepingPlace(jobId);
  return "";
}

/**
 * Open version `versionId` of `jobId`: it becomes the one that opens with the
 * song, and the song loads with its lanes and its mix (the place kept when
 * the song is already open).
 */
export async function openVersion(jobId, versionId) {
  const open = jobId === currentJobId;
  if (open && activeVersionId(jobId) === versionId) return;
  const data = await api(`${base(jobId)}/${encodeURIComponent(versionId)}/select`, { method: "POST" });
  // A change made a moment ago in the version being left is written to its
  // own record first: readSavedMix flushes it.
  if (open) await readSavedMix(jobId, getMixMode());
  applyServerVersions(jobId, data.versions, data.active_version);
  if (open) reloadKeepingPlace(jobId);
  else await openTrackInStudio(jobId);
}

/**
 * The Extract button on an open song that is already separated: nothing is
 * separated over again. The chips become a version of the song: the one that
 * already shows exactly them is opened, or a new one is made. A stem the song
 * has no file for is separated first, with the settings the song was
 * separated with (POST fill-stems), and Lead + Backing is split when asked
 * for and missing. `stems`: the chips; `voice`: "split" or "combined".
 * Resolves { handled, text }: not handled when no song is open or no chip is
 * on; `text` is what the button says happened. `onBusy(text)` is told when
 * something is being separated, which can take minutes. `expectJobId`: the
 * song meant, waited for (at most 15 seconds) when it is still loading.
 */
export async function extractAsVersion(stems, voice, onBusy = () => {}, expectJobId = null) {
  const until = Date.now() + 15000;
  while (expectJobId && layoutState().jobId !== expectJobId && Date.now() < until) {
    await new Promise((r) => setTimeout(r, 100));
  }
  const state = layoutState();
  const jobId = state.jobId;
  if (!jobId || jobId !== currentJobId || (expectJobId && jobId !== expectJobId)) return { handled: false };
  // A take recording or a Streamer take open keeps the lanes it began with.
  const why = layoutBlockedReason();
  if (why) return { handled: true, text: t(stemsReasonKey(why)) };
  const lanes = BASE_STEMS.filter((n) => (stems || []).includes(n));
  if (!lanes.length) return { handled: false };
  const present = new Set(state.present);
  const splitDone = present.has("lead_vocals") && present.has("backing_vocals");
  const wantSplit = lanes.includes("vocals") && voice === "split";
  const versions = songVersions(jobId);
  const same = versionWithStems(versions, lanes);
  if (same && same.id === activeVersionId(jobId) && (!wantSplit || splitDone)) {
    return { handled: true, text: t("versions.alreadyOpen") };
  }
  try {
    const missing = lanes.filter((n) => !present.has(n) && !(n === "vocals" && splitDone));
    if (missing.length) {
      onBusy(t("versions.extractingMissing", { names: missing.map(laneLabel).join(", ") }));
      await api(`/api/jobs/${encodeURIComponent(jobId)}/fill-stems`, jsonInit("POST", { stems: lanes }));
    }
    if (wantSplit && !splitDone) {
      onBusy(t("stems.splitRunning"));
      // Only the version asking for the pair shows it: every mode of the
      // others with no voice of its own stays at one Vocals lane.
      await freezeVoiceInVersions(jobId, versions.map((v) => v.id));
      const [ok, reason] = await runVocalSplit(jobId);
      // Why it did not work (#832), not only that it did not.
      if (!ok) return { handled: true, text: splitFailedText(reason) };
      // The version these chips match asked for the pair: it shows it.
      if (same) {
        for (const mode of LANE_MODES) {
          const key = choiceKey(jobId, mode, same.id);
          await storeSet(key, serializeChoice(mergeChoice(await storeGet(key, null), { voice: "split" })));
        }
      }
    }
    if (same) {
      // Already open, with new stems on disk: it reloads to show them.
      if (same.id === activeVersionId(jobId)) reloadKeepingPlace(jobId);
      else await openVersion(jobId, same.id);
      return { handled: true, text: t("versions.opened", { name: versionLabel(same, versions.indexOf(same)) }) };
    }
    const name = t("versions.unnamed", { n: nextVersionNumber(versions) });
    await createVersion(jobId, { name, lanes, voice: wantSplit ? "split" : null });
    return { handled: true, text: t("versions.created", { name }) };
  } catch (err) {
    console.warn("[versions] extract as a version failed:", err);
    return { handled: true, text: t("versions.extractFailed") };
  }
}
