// The Model tab on a lane and the model row it opens (Advanced mode only).
//
// Each stem a model is chosen for in Settings (vocals, drums, bass, guitar,
// piano) gets a Model tab, a microchip, in a column of its own in the lane's
// header, between the key and the rail (mixer.js leaves the slot on every
// lane, empty where there is no model to choose; Advanced mode only). Not a
// fifth cell of the rail: five cells in a 72 px row are 14 px each, under the
// 17 px a rail cell needs. A dot on it says the song's model for the stem is
// not the one Settings uses for new songs. A click opens the lane's model row
// under the lane, in both columns, after its EQ and effects rows when those
// are open, and on its own: opening or closing the EQ row leaves it as it is
// (eq/eqRows.js lays these rows out: eqPanelsHeight, syncEqRows and
// syncEqRowOrder call in here, after the effects rows):
//
//   mixer column:    Model  (lane name)
//   waveform column: [model select]  [Re-extract]  [Undo] [Keep]  | progress [Cancel]
//                    which model made the stem, and what the last run did
//
// Re-extract separates this one stem again from the song's original track
// with the chosen model (POST .../reextract, which answers at once); the page
// reads the song every second while it runs, and when it is done the song
// reloads with its place and its mix kept (the volume, mute, solo, EQ and
// effects of every lane are in the saved mix, flushed first), so the new
// stem's audio and waveform replace the old ones. The song is read, never its
// event stream: that closes at once for a finished song. The server gives a
// replaced stem a new URL, so nothing old is played from the browser's cache.
// The rules (what the select lists, what a run's line says) are in
// laneModelView.js.
//
// The rows exist only where the waveform column is the SVG overlay (the Web
// Audio engine path), like the EQ rows: eqRows.setEqContext passes it on.

import { getMode } from "./mode.js";
import { STEM_COLORS, STEM_DISPLAY } from "./constants.js";
import { mixerEl, currentJobId } from "./state.js";
import { t, onLanguageChange } from "./i18n.js";
import { codeMessage } from "./errorReason.js";
import { isOnlineSourcesRefusal, setOnlineSourcesState } from "./onlineSources.js";
import { stemsReasonKey } from "./partChoice.js";
import { openKey, parseOpenStore, withOpenChoice } from "./eq/eqPanelKinds.js";
import {
  BLOCK_RUNNING, KEEP_ARM_MS, POLL_FAIL_LIMIT, POLL_MS, STEM_MODELS_CHANGED_EVENT, splitWasReset,
  differsFromSettings, displayModelName, hasUndo, initialChoice, laneHasModelTab, laneOptions,
  lastRun, modelTabFace, reasonKey, reextractBlock, refusalCode, runLine, runPct, runRunning,
  settingsStemModel, songStemModel, stickyRefusal, tabLabel, undoModel,
} from "./laneModelView.js";

// catalog.js, mixer.js, modeStems.js and reloadPlace.js are loaded when used,
// not imported here: eqRows.js imports this file and player.js imports
// eqRows.js, and a static import would pull those modules into player.js's
// own import order.
const lazy = {
  catalog: () => import("./catalog.js"),
  mixer: () => import("./mixer.js"),
  modeStems: () => import("./modeStems.js"),
  reload: () => import("./reloadPlace.js"),
};

/** Height of a model row, px, the same in both columns. */
export const MODEL_PANEL_HEIGHT = 104;
const OPEN_STORE_KEY = "stemdeck.modelPanels";

let _want = false;
let _jobId = null;
let _lanes = [];
let _store = null;
// The open song's job state, the fields this file reads (stem_models,
// stem_undo, stem_reextract), and the GET /api/settings answer.
let _job = null;
let _settings = null;
let _settingsLoad = null;
// When the last read of the model list failed: the redraws do not ask again
// for a while (opening a row does).
let _settingsFailedAt = 0;
const SETTINGS_RETRY_MS = 30000;
// lane -> { waveRow, mixRow, select, reextract, undo, cancel, progress, status, hint, choice }
const _rows = new Map();
// What the row says about a request this page sent: lane -> { kind, text }.
const _notes = new Map();
// A refusal that is about the song (no source): jobId -> i18n key.
const _songBlocks = new Map();
// The song being read while a run goes: { jobId, timer, fails, watching }.
let _poll = null;
// A request this page is sending (Re-extract, Undo, Keep, Cancel): lane.
let _sending = null;
// The song's vocal_split while its run was seen running, and the lanes whose
// run set the Lead + Backing pair aside (a Vocals re-extract).
let _splitBefore = null;
const _splitReset = new Set();

const enabled = () => _want && !!_jobId && getMode() === "daw";

// ─── Open state ───

function loadStore() {
  if (_store) return _store;
  let raw = null;
  try {
    raw = localStorage.getItem(OPEN_STORE_KEY);
  } catch (e) {
    console.warn("[model] reading the open model rows failed:", e);
  }
  _store = parseOpenStore(raw);
  return _store;
}

function saveOpen(lane, open) {
  _store = withOpenChoice(loadStore(), openKey(_jobId, lane), open);
  try {
    localStorage.setItem(OPEN_STORE_KEY, JSON.stringify(_store));
  } catch (e) {
    console.warn("[model] saving the open model rows failed:", e);
  }
}

export function isModelOpen(lane) {
  return enabled() && _lanes.includes(lane) && loadStore()[openKey(_jobId, lane)] === true;
}

/** Height the open model rows add to the lane stack (px). */
export function modelPanelsHeight() {
  if (!enabled()) return 0;
  return _lanes.filter(isModelOpen).length * MODEL_PANEL_HEIGHT;
}

/** The song and its lanes (eqRows.setEqContext passes them on). */
export function setModelContext({ jobId, lanes, enabled: want }) {
  const changed = (jobId || null) !== _jobId;
  if (changed) {
    for (const lane of [..._rows.keys()]) dropRows(lane);
    _notes.clear();
    _splitReset.clear();
    _splitBefore = null;
    stopPoll();
    _job = null;
  }
  _jobId = jobId || null;
  _lanes = (Array.isArray(lanes) ? lanes : []).filter(laneHasModelTab);
  _want = !!want;
  if (!enabled()) for (const lane of [..._rows.keys()]) dropRows(lane);
  // Every load of the song (a reload after a run included) brings its state.
  if (_jobId) readSong(_jobId);
}

function setOpen(lane, open) {
  if (!enabled() || !_lanes.includes(lane)) return;
  saveOpen(lane, !!open);
  if (!open) dropRows(lane);
  else {
    // What the row shows is read again: the model list, and the song (its
    // undo copies, a run another window started).
    refreshSettings();
    if (!_poll) startPoll(_jobId, 0);
  }
  document.dispatchEvent(new CustomEvent("stemdeck:eq-rows-changed", { detail: { lane, open: !!open } }));
}

// ─── Data ───

/** The song's state as catalog.js last read it (no request), then a run
 *  is followed. The model list is read once the tabs can show. */
async function readSong(jobId) {
  try {
    const { getCurrentTrackModels } = await lazy.catalog();
    const info = getCurrentTrackModels();
    if (jobId !== _jobId || !info || info.job_id !== jobId) return;
    takeJob(info);
  } catch (e) {
    console.warn("[model] reading the song's models failed:", e);
  }
  if (enabled()) loadSettings();
}

/** Take a job state (a poll, a request's answer, the catalog's copy). */
function takeJob(state) {
  if (!state || state.job_id !== _jobId) return;
  const wasRunning = runRunning(_job);
  _job = {
    job_id: state.job_id,
    stem_models: state.stem_models ?? null,
    stem_undo: state.stem_undo ?? null,
    stem_reextract: state.stem_reextract ?? null,
    vocal_split: state.vocal_split ?? null,
  };
  const run = lastRun(_job);
  if (runRunning(_job)) {
    _splitBefore = _job.vocal_split;
    startPoll(_jobId);
  } else if (wasRunning) {
    if (splitWasReset(run, _splitBefore, _job)) _splitReset.add(run.stem);
    _splitBefore = null;
  }
  // No source to separate from is about the song, not one try.
  if (run?.state === "error" && stickyRefusal(run.code)) _songBlocks.set(_jobId, reasonKey(run.code));
  paintAll();
  return wasRunning && !runRunning(_job);
}

function loadSettings() {
  if (_settings || _settingsLoad) return _settingsLoad;
  if (_settingsFailedAt && Date.now() - _settingsFailedAt < SETTINGS_RETRY_MS) return null;
  return refreshSettings();
}

/** Read GET /api/settings again: the model list and the default per stem. */
function refreshSettings() {
  if (_settingsLoad) return _settingsLoad;
  _settingsLoad = (async () => {
    try {
      const r = await fetch("/api/settings", { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      _settings = await r.json();
      _settingsFailedAt = 0;
    } catch (e) {
      console.warn("[model] reading the model list failed:", e);
      _settingsFailedAt = Date.now();
    } finally {
      _settingsLoad = null;
    }
    paintAll();
  })();
  return _settingsLoad;
}

async function api(path, body) {
  let res;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  } catch (e) {
    console.warn("[model] the request did not reach the server:", e);
    return { ok: false, status: 0, data: null };
  }
  const data = await res.json().catch(() => null);
  if (!res.ok && isOnlineSourcesRefusal(data)) setOnlineSourcesState(false);
  return { ok: res.ok, status: res.status, data };
}

const jobPath = (jobId) => `/api/jobs/${encodeURIComponent(jobId)}`;

// ─── Following a run ───

function startPoll(jobId, delay = POLL_MS) {
  if (!jobId || (_poll && _poll.jobId === jobId)) return;
  stopPoll();
  _poll = { jobId, timer: 0, fails: 0 };
  _poll.timer = setTimeout(pollOnce, delay);
}

function stopPoll() {
  if (_poll) clearTimeout(_poll.timer);
  _poll = null;
}

async function pollOnce() {
  const p = _poll;
  if (!p) return;
  let state = null;
  try {
    const r = await fetch(jobPath(p.jobId), { cache: "no-store" });
    if (r.ok) state = await r.json();
  } catch (e) {
    console.warn("[model] reading the song while it is separated failed:", e);
  }
  if (_poll !== p) return;
  if (!state) {
    p.fails += 1;
    if (p.fails >= POLL_FAIL_LIMIT) {
      stopPoll();
      noteAll({ kind: "error", text: t("laneModel.lost") });
      return;
    }
    p.timer = setTimeout(pollOnce, POLL_MS);
    return;
  }
  p.fails = 0;
  // Stopped polling first: takeJob starts it again while the run goes on.
  _poll = null;
  if (takeJob(state)) finishRun(p.jobId);
}

/**
 * A run of the open song ended. A stem that was replaced (done) reloads the
 * song with its place and its mix; a cancelled or failed one changed nothing.
 */
async function finishRun(jobId) {
  const run = lastRun(_job);
  if (!run || run.state !== "done" || jobId !== currentJobId) return;
  await reloadSong(jobId);
}

async function reloadSong(jobId) {
  try {
    const [{ readSavedMix, getMixMode }, { reloadKeepingPlace }] = await Promise.all([lazy.mixer(), lazy.reload()]);
    // A change made a moment ago is written to the song's mix first, so the
    // reload brings back every lane's volume, mute, solo, EQ and effects.
    await readSavedMix(jobId, getMixMode());
    if (jobId !== currentJobId) return;
    reloadKeepingPlace(jobId);
  } catch (e) {
    console.warn("[model] reloading the song after the new stem failed:", e);
  }
}

// ─── Actions ───

/** Why the lanes cannot change now (a take recording), as an i18n key, or "". */
async function pageBlockKey() {
  try {
    const { layoutBlockedReason } = await lazy.modeStems();
    const why = layoutBlockedReason();
    return why ? stemsReasonKey(why) : "";
  } catch (e) {
    console.warn("[model] reading whether the lanes can change failed:", e);
    return "";
  }
}

function refusalNote(status, data, action = "reextract") {
  const known = codeMessage(data);
  if (known) return { kind: "error", text: known, code: data?.code || "" };
  const code = refusalCode(status, data);
  return { kind: "error", text: t(reasonKey(code, action)), code };
}

async function reextract(lane) {
  const r = _rows.get(lane);
  const jobId = _jobId;
  if (!r || !jobId || _sending) return;
  const blocked = await pageBlockKey();
  if (blocked) {
    _notes.set(lane, { kind: "error", text: t(blocked) });
    paintRow(lane);
    return;
  }
  const model = r.choice;
  _sending = lane;
  _notes.delete(lane);
  paintRow(lane);
  const res = await api(`${jobPath(jobId)}/reextract`, { stem: lane, model });
  _sending = null;
  if (jobId !== _jobId) return;
  if (res.ok) {
    takeJob(res.data?.state);
    // The answer came before the run's first step: it is followed from here.
    if (!runRunning(_job)) startPoll(jobId);
    paintRow(lane);
    return;
  }
  const note = refusalNote(res.status, res.data);
  if (stickyRefusal(note.code)) _songBlocks.set(jobId, reasonKey(note.code));
  _notes.set(lane, note);
  paintRow(lane);
}

async function undo(lane) {
  const jobId = _jobId;
  if (!jobId || _sending) return;
  const blocked = await pageBlockKey();
  if (blocked) {
    _notes.set(lane, { kind: "error", text: t(blocked) });
    paintRow(lane);
    return;
  }
  _sending = lane;
  _notes.set(lane, { kind: "running", text: t("laneModel.undoing", { name: displayName(lane) }) });
  paintRow(lane);
  const res = await api(`${jobPath(jobId)}/reextract/undo`, { stem: lane });
  _sending = null;
  if (jobId !== _jobId) return;
  _notes.delete(lane);
  if (res.ok) {
    // The undo brings the Lead + Backing pair back with the old Vocals.
    _splitReset.delete(lane);
    takeJob(res.data?.state);
    // The undo ran to its end before the answer: the stem on disk is the old one.
    if (lastRun(_job)?.state === "done") await reloadSong(jobId);
    else startPoll(jobId);
    return;
  }
  _notes.set(lane, refusalNote(res.status, res.data, "undo"));
  paintRow(lane);
}

/**
 * Keep: the earlier version of the stem (its undo copy) is deleted for good.
 * Two presses, as the track's Delete tool: the first arms it and says to
 * press again, the second within KEEP_ARM_MS sends it.
 */
async function keep(lane) {
  const r = _rows.get(lane);
  const jobId = _jobId;
  if (!r || !jobId || _sending) return;
  if (!r.keepArmed) {
    r.keepArmed = true;
    clearTimeout(r.keepTimer);
    r.keepTimer = setTimeout(() => {
      r.keepArmed = false;
      paintRow(lane);
    }, KEEP_ARM_MS);
    paintRow(lane);
    return;
  }
  clearTimeout(r.keepTimer);
  r.keepArmed = false;
  _sending = lane;
  paintRow(lane);
  const res = await api(`${jobPath(jobId)}/reextract/keep`, { stem: lane });
  _sending = null;
  if (jobId !== _jobId) return;
  if (res.ok) {
    _notes.set(lane, { kind: "done", text: t("laneModel.kept", { name: displayName(lane) }) });
    takeJob(res.data?.state);
    return;
  }
  _notes.set(lane, refusalNote(res.status, res.data, "keep"));
  paintRow(lane);
}

async function cancel(lane) {
  const jobId = _jobId;
  if (!jobId) return;
  const r = _rows.get(lane);
  if (r) r.cancel.disabled = true;
  const res = await api(`${jobPath(jobId)}/cancel`, {});
  if (!res.ok) {
    console.warn("[model] the cancel was refused:", res.status, res.data);
    _notes.set(lane, refusalNote(res.status, res.data));
  }
  // The run's state says "cancelled" on the next read.
  startPoll(jobId);
  paintRow(lane);
}

// ─── Names ───

function displayName(lane) {
  return STEM_DISPLAY[lane] || lane;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// ─── Tabs ───

function laneMixRow(lane) {
  return mixerEl?.querySelector(`.lane-header[data-stem="${CSS.escape(lane)}"]`) || null;
}
function laneWaveRow(lane) {
  return document.querySelector(`.stem-waveform-layer .stem-waveform-row[data-stem="${CSS.escape(lane)}"]`) || null;
}

/** The chip's column in the lane's header (mixer.js renderMixerRow). */
function laneModelSlot(lane) {
  return laneMixRow(lane)?.querySelector(":scope > .mx-model-slot") || null;
}

function ensureTab(lane) {
  const slot = laneModelSlot(lane);
  if (!slot) return;
  let tab = slot.querySelector(":scope > .model-tab");
  if (!tab) {
    // The rail's EQ tab's face (eq.css .eq-tab, daw.css .mx-model-slot).
    tab = el("button", "eq-tab model-tab");
    tab.type = "button";
    tab.dataset.modelLane = lane;
    const face = el("span", "model-tab-icon");
    face.setAttribute("aria-hidden", "true");
    face.innerHTML = modelTabFace();
    const dot = el("span", "model-tab-dot");
    dot.setAttribute("aria-hidden", "true");
    tab.append(face, dot);
    tab.addEventListener("click", (e) => {
      e.stopPropagation();
      setOpen(lane, !isModelOpen(lane));
    });
    slot.append(tab);
  }
  paintTab(tab, lane);
}

function paintTab(tab, lane) {
  if (!tab) return;
  const open = isModelOpen(lane);
  tab.classList.toggle("open", open);
  tab.classList.toggle("differs", differsFromSettings(_job, _settings, lane));
  tab.setAttribute("aria-expanded", String(open));
  if (open) tab.setAttribute("aria-controls", `model-panel-${lane}`);
  else tab.removeAttribute("aria-controls");
  const label = tabLabel(_job, _settings, lane, t);
  tab.setAttribute("aria-label", label);
  tab.title = label;
}

function dropTabs(keep) {
  for (const tab of document.querySelectorAll(".mx-model-slot > .model-tab")) {
    if (!keep || !keep.includes(tab.dataset.modelLane)) tab.remove();
  }
}

// ─── Rows ───

function dropRows(lane) {
  const r = _rows.get(lane);
  if (!r) return;
  r.waveRow.remove();
  r.mixRow.remove();
  _rows.delete(lane);
}

function buildRows(lane) {
  const h = MODEL_PANEL_HEIGHT;
  const color = STEM_COLORS[lane] || "#a0a0a0";
  const name = displayName(lane);

  const waveRow = el("div", "stem-waveform-row eq-panel-lane model-panel-lane");
  waveRow.dataset.modelLane = lane;
  waveRow.id = `model-panel-${lane}`;
  waveRow.setAttribute("role", "region");
  waveRow.setAttribute("aria-label", t("laneModel.panel.regionAria", { name }));
  waveRow.style.setProperty("--stem-color", color);
  waveRow.style.flex = `0 0 ${h}px`;
  waveRow.style.height = `${h}px`;
  const body = el("div", "eq-panel-body model-panel-body");
  // The row's clicks and keys are its own: the waves column would start a
  // seek or a loop drag on pointerdown.
  body.addEventListener("pointerdown", (e) => e.stopPropagation());
  body.addEventListener("click", (e) => e.stopPropagation());
  body.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });

  const line = el("div", "model-panel-line");
  const select = el("select", "model-select");
  select.addEventListener("change", () => {
    const r = _rows.get(lane);
    if (!r) return;
    r.choice = select.value;
    _notes.delete(lane);
    paintRow(lane);
  });
  const reextractBtn = el("button", "eq-btn model-btn model-reextract");
  reextractBtn.type = "button";
  reextractBtn.addEventListener("click", () => reextract(lane));
  const undoBtn = el("button", "eq-btn model-btn model-undo");
  undoBtn.type = "button";
  undoBtn.addEventListener("click", () => undo(lane));
  const keepBtn = el("button", "eq-btn model-btn model-keep");
  keepBtn.type = "button";
  keepBtn.addEventListener("click", () => keep(lane));
  const run = el("div", "model-run");
  const progress = el("progress", "model-progress");
  progress.max = 100;
  const pct = el("span", "model-pct");
  const cancelBtn = el("button", "eq-btn model-btn model-cancel");
  cancelBtn.type = "button";
  cancelBtn.addEventListener("click", () => cancel(lane));
  run.append(progress, pct, cancelBtn);
  line.append(select, reextractBtn, undoBtn, keepBtn, run);
  const status = el("p", "model-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const hint = el("p", "model-hint");
  body.append(line, status, hint);
  waveRow.append(body);

  const mixRow = el("div", "lane-header mx-row eq-panel-row model-panel-row");
  mixRow.dataset.modelLane = lane;
  mixRow.style.height = `${h}px`;
  mixRow.style.minHeight = `${h}px`;
  mixRow.style.setProperty("--stem-color", color);
  const side = el("div", "eq-side model-side");
  const head = el("div", "eq-side-head");
  const titles = el("div", "eq-side-titles");
  const titleEl = el("span", "eq-side-title model-side-title", t("laneModel.panel.title"));
  titleEl.style.color = color;
  const laneEl = el("span", "eq-side-lane", name);
  laneEl.title = name;
  titles.append(titleEl, laneEl);
  head.append(titles);
  side.append(head);
  mixRow.append(side);

  return {
    waveRow, mixRow, select, reextract: reextractBtn, undo: undoBtn, keep: keepBtn, keepArmed: false, keepTimer: 0, cancel: cancelBtn,
    run, progress, pct, status, hint, titleEl, laneEl, choice: null, optionsKey: "",
  };
}

/** Fill the select from the settings answer, keeping the person's choice. */
function paintOptions(lane, r) {
  const options = laneOptions(_settings, _job, lane, t);
  const key = JSON.stringify(options.map((o) => [o.value, o.label, o.disabled]));
  if (r.choice === null || !options.some((o) => o.value === r.choice)) r.choice = initialChoice(options, _job, lane);
  if (key !== r.optionsKey) {
    r.optionsKey = key;
    r.select.replaceChildren(...options.map((o) => {
      const opt = el("option", null, o.label);
      opt.value = o.value;
      opt.disabled = o.disabled;
      return opt;
    }));
  }
  r.select.value = r.choice ?? "";
  return options;
}

function paintRow(lane) {
  const r = _rows.get(lane);
  if (!r) return;
  const name = displayName(lane);
  const options = paintOptions(lane, r);
  const running = runRunning(_job);
  const run = lastRun(_job);
  const mine = running && run?.stem === lane;
  const sending = _sending === lane;
  const block = reextractBlock({ job: _job, options, choice: r.choice, blockedKey: _songBlocks.get(_jobId) || "" });

  r.titleEl.textContent = t("laneModel.panel.title");
  r.laneEl.textContent = name;
  r.laneEl.title = name;
  r.waveRow.setAttribute("aria-label", t("laneModel.panel.regionAria", { name }));
  r.select.setAttribute("aria-label", t("laneModel.select.aria", { name }));
  r.select.disabled = running || sending || !options.length;

  r.reextract.textContent = t("laneModel.reextract");
  r.reextract.title = t("laneModel.reextractTitle", { name });
  r.reextract.disabled = Boolean(block) || sending || !!_sending;
  r.reextract.hidden = mine;

  const undoable = hasUndo(_job, lane);
  r.undo.textContent = t("laneModel.undo");
  r.undo.title = t("laneModel.undoTitle", { name, model: displayModelName(_settings, undoModel(_job, lane), t) });
  r.undo.hidden = !undoable || mine;
  r.undo.disabled = running || !!_sending;

  if (!undoable || running) r.keepArmed = false;
  r.keep.textContent = t(r.keepArmed ? "laneModel.keepAgain" : "laneModel.keep");
  r.keep.title = r.keepArmed ? t("laneModel.keepAgain") : t("laneModel.keepTitle", { name });
  r.keep.setAttribute("aria-label", r.keepArmed ? t("laneModel.keepAgain") : t("laneModel.keepTitle", { name }));
  r.keep.classList.toggle("armed", r.keepArmed);
  r.keep.hidden = !undoable || mine;
  r.keep.disabled = running || !!_sending;

  r.run.hidden = !mine;
  const pct = mine ? runPct(run) : 0;
  r.progress.value = pct;
  r.progress.setAttribute("aria-label", t("laneModel.progressAria", { name }));
  r.pct.textContent = mine ? `${pct}%` : "";
  r.cancel.textContent = t("laneModel.cancel");
  r.cancel.title = t("laneModel.cancelTitle", { name });
  if (!mine) r.cancel.disabled = false;
  // An undo runs to its end in one request: there is nothing to stop.
  if (mine && run?.action === "undo") r.cancel.hidden = true;
  else r.cancel.hidden = false;

  // The status line: a request's own answer first, then the song's last run.
  const note = _notes.get(lane) || runLine(_job, _settings, lane, name, t, displayName);
  // A Vocals re-extract set the Lead + Backing pair aside: one plain line.
  const text = [note?.text, _splitReset.has(lane) ? t("laneModel.splitReset") : ""].filter(Boolean).join(" ");
  r.status.textContent = text;
  r.status.title = text;
  r.status.dataset.kind = note?.kind || "";

  // The hint: which model made the stem, the Settings default when it is
  // another, and why Re-extract is off when it is.
  const made = t("laneModel.madeWith", { name, model: displayModelName(_settings, songStemModel(_job, lane), t) });
  const parts = [made];
  if (differsFromSettings(_job, _settings, lane)) {
    parts.push(t("laneModel.differs", { model: displayModelName(_settings, settingsStemModel(_settings, lane), t) }));
  }
  if (block && block !== BLOCK_RUNNING && !note) parts.push(t(block));
  r.hint.textContent = parts.join(" ");
  r.hint.title = r.hint.textContent;
}

function paintAll() {
  for (const tab of document.querySelectorAll(".mx-model-slot > .model-tab[data-model-lane]")) paintTab(tab, tab.dataset.modelLane);
  for (const lane of _rows.keys()) paintRow(lane);
}

/** One sentence on every open row (the song could not be read). */
function noteAll(note) {
  for (const lane of _rows.keys()) _notes.set(lane, note);
  paintAll();
}

/** Put the tabs and the open rows in place (eqRows.syncEqRows calls it). */
export function syncModelRows() {
  if (!enabled()) {
    for (const lane of [..._rows.keys()]) dropRows(lane);
    dropTabs(null);
    return;
  }
  // The tabs' names and dots need the model list (one request a page).
  loadSettings();
  dropTabs(_lanes);
  for (const lane of _lanes) ensureTab(lane);
  for (const lane of [..._rows.keys()]) if (!isModelOpen(lane)) dropRows(lane);
  for (const lane of _lanes) {
    if (!isModelOpen(lane)) continue;
    const waveLane = laneWaveRow(lane);
    const mixLane = laneMixRow(lane);
    if (!waveLane || !mixLane) continue;
    let r = _rows.get(lane);
    if (!r) {
      r = buildRows(lane);
      _rows.set(lane, r);
    }
    // After the lane's EQ and effects rows when they are open, else right
    // after the lane.
    const waveAfter = lastPanelAfter(waveLane, lane, ".eq-panel-lane");
    const mixAfter = lastPanelAfter(mixLane, lane, ".eq-panel-row");
    if (waveAfter.nextElementSibling !== r.waveRow) waveAfter.after(r.waveRow);
    if (mixAfter.nextElementSibling !== r.mixRow) mixAfter.after(r.mixRow);
    paintRow(lane);
  }
  syncModelRowOrder();
}

/** The last of the lane's own EQ and effects rows right after it, else the lane. */
function lastPanelAfter(laneEl, lane, cls) {
  let at = laneEl;
  const esc = CSS.escape(lane);
  for (;;) {
    const next = at.nextElementSibling;
    if (next?.matches?.(`${cls}[data-eq-lane="${esc}"], ${cls}[data-fx-lane="${esc}"]`)) at = next;
    else return at;
  }
}

/** Each row's CSS order: the lane's + 1, as the EQ row (DOM order breaks the tie). */
export function syncModelRowOrder() {
  for (const [lane, r] of _rows) {
    const wo = Number(laneWaveRow(lane)?.style.order);
    const m = laneMixRow(lane);
    const mo = m?.style.order === "" ? NaN : Number(m?.style.order);
    r.waveRow.style.order = Number.isFinite(wo) ? String(wo + 1) : "";
    r.mixRow.style.order = Number.isFinite(mo) ? String(mo + 1) : "";
  }
}

// ─── Events ───

onLanguageChange(() => {
  for (const r of _rows.values()) r.optionsKey = "";
  paintAll();
});

// Practice and Advanced: eqRows.js's own mode listener puts the tabs and
// rows in place (syncEqRows calls syncModelRows) and lays the lanes out again.

// A model chosen in Settings, or the device or quality changed there: the
// default each dot compares against follows. Captured: the settings overlay
// dispatches its answer on itself, and it does not bubble.
document.addEventListener(STEM_MODELS_CHANGED_EVENT, (e) => {
  if (e.detail && typeof e.detail === "object") {
    _settings = e.detail;
    paintAll();
  } else {
    refreshSettings();
  }
});
document.addEventListener("stemdeck:settings-applied", (e) => {
  if (e.detail?.stem_model_catalog) {
    _settings = { ..._settings, ...e.detail };
    paintAll();
  }
}, true);
