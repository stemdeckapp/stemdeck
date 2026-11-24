// The Model tab on a lane (Advanced mode) and the row it opens, the pure
// part: which lanes get one, what the tab says, which model the song's stem
// was made with, what the select lists, and what a refusal says. No DOM, so
// tests/js/lane-model-view.test.mjs runs it in node. laneModelRows.js draws.
//
// The select is the Settings > Separation list for that stem (stemModelsView.js,
// the same "(yours)" and Recommended rules), over the GET /api/settings answer.
// It starts at the model THIS song's stem was made with: the job's
// `stem_models[stem]` (app/core/models.py), recorded when the song was
// separated, so a new song shows what Settings chose at that time. A song
// separated before that field existed has every stem from the base model.
//
// Server side (docs/api.md, app/api/jobs.py):
//   POST /api/jobs/{id}/reextract       { stem, model }  202 { state }, runs on
//   POST /api/jobs/{id}/reextract/undo  { stem }         200 { state }
//   POST /api/jobs/{id}/reextract/keep  { stem }         200 { state }, the undo copy goes
//   POST /api/jobs/{id}/cancel                           stops a running one
// A run is followed by reading GET /api/jobs/{id}: the job's event stream
// closes at once for a finished song, so it carries no progress here.
// The job state carries `stem_undo` ({ stem: { model, saved_at } }, which
// stems have an undo copy) and `stem_reextract` (the last run: stem, model,
// action, state running | done | error | cancelled, phase, progress, code,
// used_model, fallback, gain_db). A refusal is { detail, code }.

import { CHOICE_STEMS, hasStemModels, modelName, selectedValue, stemOptions, stemRows } from "./stemModelsView.js";
import { reasonText } from "./myModelsView.js";

/** The model every stem of a song separated before per-stem models came from. */
export const BASE_MODEL = "htdemucs_6s";

/** The lanes with a Model tab: the stems a model is chosen for in Settings. */
export const MODEL_LANES = Object.freeze([...CHOICE_STEMS]);

const MICROCHIP_SVG =
  '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
  + '<rect x="7" y="5" width="10" height="14" rx="1.5"/>'
  + '<path d="M4 8.5h3M4 12h3M4 15.5h3M17 8.5h3M17 12h3M17 15.5h3"/>'
  + "</svg>";

/**
 * What the Model tab shows: a microchip (a body with short pins on two
 * sides), in the left rail's line style. The one place it is drawn: to show
 * the text instead, return "MDL".
 */
export function modelTabFace() {
  return MICROCHIP_SVG;
}

/** Whether a lane gets a Model tab. */
export function laneHasModelTab(lane) {
  return MODEL_LANES.includes(lane);
}

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

/** The model the song's stem was made with, from its job state. */
export function songStemModel(job, stem) {
  const id = isObj(job?.stem_models) ? job.stem_models[stem] : null;
  return typeof id === "string" && id ? id : BASE_MODEL;
}

/** The model Settings uses for this stem on a new song ("" when unknown). */
export function settingsStemModel(settings, stem) {
  if (!hasStemModels(settings)) return "";
  return String(selectedValue(settings, stem) || "");
}

/**
 * Whether the tab carries its dot: the song's model for the stem is not the
 * one Settings uses for a new song. Unknown (no settings answer) is no dot.
 */
export function differsFromSettings(job, settings, stem) {
  const def = settingsStemModel(settings, stem);
  return Boolean(def) && songStemModel(job, stem) !== def;
}

/** The tab's tooltip and accessible name, "Model: HT Demucs 6s". */
export function tabLabel(job, settings, stem, t) {
  return t("laneModel.tab.title", { model: displayModelName(settings, songStemModel(job, stem), t) });
}

/** A model's name as the select shows it: "(yours)" for one the user added. */
export function displayModelName(settings, id, t) {
  const m = settings?.stem_model_catalog?.models?.[id];
  const name = modelName(settings, id);
  return m?.user === true ? t("settings.myModels.yours", { name }) : name;
}

/**
 * The options of the lane's select: the Settings rows for the stem
 * (stemOptions, without "Add a model..."). The song's own model is listed
 * even when the catalog no longer offers it for the stem (a model removed
 * since), so the select never claims another model made the stem.
 * Returns [] when the settings answer has no per-stem models.
 */
export function laneOptions(settings, job, stem, t) {
  const row = stemRows(settings).find((r) => r.stem === stem);
  let choices = [];
  if (row?.kind === "select") choices = row.choices;
  else if (row?.kind === "fixed" && row.fixed === "only") choices = [row.model];
  const opts = choices.length ? stemOptions(settings, stem, choices, t) : [];
  const current = songStemModel(job, stem);
  if (opts.length && !opts.some((o) => o.value === current)) {
    opts.unshift({ value: current, label: displayModelName(settings, current, t), disabled: true, recommended: false, add: false });
  }
  return opts;
}

/** The select's first value: the song's own model when listed, else the first usable one. */
export function initialChoice(options, job, stem) {
  const current = songStemModel(job, stem);
  if (options.some((o) => o.value === current)) return current;
  return options.find((o) => !o.disabled)?.value ?? "";
}

// ─── A run and its outcome ───

/** How often the page reads the song while a re-extract runs, ms. */
export const POLL_MS = 1000;
/** Reads in a row that may fail before the page stops and says so. */
export const POLL_FAIL_LIMIT = 5;

/** Separation settings changed in Settings: the tabs' dots follow. */
export const STEM_MODELS_CHANGED_EVENT = "stemdeck:stem-models-changed";

/** The refusal and failure codes the server gives, each with its sentence. */
export const REASON_CODES = Object.freeze([
  "not_ready", "stem_not_selectable", "stems_missing", "model_not_allowed", "model_removed",
  "model_unavailable", "no_source", "busy", "no_undo", "failed", "source_changed", "interrupted",
  "not_found",
]);

/** Codes an undo says in its own words: "missing stems" there means the
 *  earlier version cannot be put back, not "extract them first". */
export const UNDO_REASON_CODES = Object.freeze(["stems_missing"]);

/**
 * The i18n key for a refusal or failure code; one this page does not know
 * gets the general one. `action`: "undo" for an undo's own sentences.
 */
export function reasonKey(code, action = "reextract") {
  if (action === "undo" && UNDO_REASON_CODES.includes(code)) return `laneModel.reason.undo.${code}`;
  return REASON_CODES.includes(code) ? `laneModel.reason.${code}` : "laneModel.reason.unknown";
}

/** The song's last run, or null. */
export function lastRun(job) {
  const r = job?.stem_reextract;
  return isObj(r) && typeof r.stem === "string" && typeof r.state === "string" ? r : null;
}

/** Whether a re-extract or an undo of any stem of the song is running. */
export function runRunning(job) {
  return lastRun(job)?.state === "running";
}

/** Whether the stem has an undo copy (the Undo button shows). */
export function hasUndo(job, stem) {
  return isObj(job?.stem_undo) && isObj(job.stem_undo[stem]);
}

/** The model the stem's undo copy was made with, or "". */
export function undoModel(job, stem) {
  const id = hasUndo(job, stem) ? job.stem_undo[stem].model : "";
  return typeof id === "string" ? id : "";
}

/** A running run's progress, a whole percent from 0 to 100. */
export function runPct(run) {
  const p = Number(run?.progress);
  return Number.isFinite(p) ? Math.max(0, Math.min(100, Math.round(p * 100))) : 0;
}

const PHASES = ["waiting", "downloading", "separating", "finishing"];

/**
 * The line under the select, as { kind, text }:
 *   running  this stem's run is going: its phase
 *   other    another stem of the song is being separated again
 *   done | undone | cancelled | error   how this stem's last run ended
 * Null when there is nothing to say (no run, or another stem's run ended).
 * `name` is the lane's display name; `stemName(stem)` names another stem.
 */
export function runLine(job, settings, stem, name, t, stemName = (s) => s) {
  const run = lastRun(job);
  if (!run) return null;
  const model = (id) => displayModelName(settings, id, t);
  if (run.state === "running") {
    if (run.stem !== stem) return { kind: "other", text: t("laneModel.otherStem", { stem: stemName(run.stem) }) };
    if (run.action === "undo") return { kind: "running", text: t("laneModel.undoing", { name }) };
    const phase = PHASES.includes(run.phase) ? run.phase : "separating";
    return { kind: "running", text: t(`laneModel.phase.${phase}`, { name, model: model(run.model) }) };
  }
  if (run.stem !== stem) return null;
  if (run.state === "cancelled") return { kind: "cancelled", text: t("laneModel.cancelled", { name }) };
  if (run.state === "error") {
    if (run.code === "online_sources_off") return { kind: "error", text: t("onlineSources.off") };
    return { kind: "error", text: t(reasonKey(run.code, run.action)) };
  }
  if (run.state !== "done") return null;
  if (run.action === "undo") return { kind: "undone", text: t("laneModel.undone", { name, model: model(songStemModel(job, stem)) }) };
  const used = typeof run.used_model === "string" && run.used_model ? run.used_model : songStemModel(job, stem);
  let text;
  if (isObj(run.fallback)) {
    const asked = typeof run.fallback.name === "string" && run.fallback.name ? run.fallback.name : model(run.fallback.model);
    text = t("laneModel.doneFallback", { asked, model: model(used), name, reason: reasonText(run.fallback.code, t) });
  } else {
    text = t("laneModel.done", { name, model: model(used) });
  }
  const db = Number(run.gain_db);
  // "turned down by 1.5 dB": the server's change is negative.
  if (Number.isFinite(db) && db < 0) text = `${text} ${t("laneModel.gain", { db: String(Math.round(-db * 10) / 10) })}`;
  return { kind: "done", text };
}

/** reextractBlock's answer while a run of the song is going: the run's own
 *  line (runLine) says what is happening, so there is no reason to add. */
export const BLOCK_RUNNING = "running";

/**
 * Whether Re-extract can be pressed, and why not: "" when it can,
 * BLOCK_RUNNING while a run of the song is going, else the i18n key of the
 * reason shown under it. `blockedKey`: a reason this page found itself (a
 * take recording, a refusal remembered for the song).
 */
export function reextractBlock({ job, options, choice, blockedKey = "" }) {
  if (runRunning(job)) return BLOCK_RUNNING;
  if (blockedKey) return blockedKey;
  if (!options?.length) return "laneModel.noChoices";
  const opt = options.find((o) => o.value === choice);
  if (!opt || opt.disabled) return "laneModel.reason.model_unavailable";
  return "";
}

/** The code of a refused request: the body's own, else read from the status. */
export function refusalCode(status, body) {
  if (isObj(body) && typeof body.code === "string" && body.code) return body.code;
  if (status === 404) return "not_found";
  return "unknown";
}

/**
 * Whether a refusal is about the song rather than this one try, so the row
 * keeps saying it (and keeps Re-extract off) until the song changes: no
 * source to separate from.
 */
export function stickyRefusal(code) {
  return code === "no_source";
}

/**
 * Whether the run that just ended set the Lead + Backing pair aside: a done
 * re-extract of Vocals on a song whose pair existed before it (`splitBefore`,
 * the song's vocal_split when the run was seen running) and is gone now.
 */
export function splitWasReset(run, splitBefore, job) {
  return run?.stem === "vocals" && run.action === "reextract" && run.state === "done"
    && splitBefore === "done" && job?.vocal_split !== "done";
}

/** How long Keep waits for its second press, ms (the track Delete tool's 3 s). */
export const KEEP_ARM_MS = 3000;
