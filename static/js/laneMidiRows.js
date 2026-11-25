// The MIDI chip on the Piano and Bass lanes and the MIDI row it opens
// (Advanced mode only), and the Export menu's Include MIDI. The piano roll,
// the lane's source switch and the download button's choices are in
// laneMidiPlay.js, called from here.
//
// The chip sits in the lane header's Model column (mixer.js .mx-model-slot),
// under the Model chip: the column keeps its width and the row its height, so
// adding it moves nothing (the column is split in two cells, Model above, MIDI
// below; daw.css .mx-model-slot.has-midi), drawn as two notes. A click opens
// the row and, when the lane has a MIDI file, its MIDI view (the piano roll
// over the waveform); the icon is gold while it is on. It shows only when the lane's
// setting in Settings > Transcription is Auto, the song is finished and the
// server has transcription; when the engine cannot run on this computer it
// shows dimmed and its tooltip says why. While a run goes the chip carries the
// percent, a dot once a file is there, a red dot after a failed run.
//
// A click opens the lane's MIDI row under the lane, in both columns, after its
// EQ, effects and model rows when those are open (eq/eqRows.js lays these
// rows out: eqPanelsHeight, syncEqRows and syncEqRowOrder call in here):
//
//   mixer column:    MIDI  (lane name)
//   waveform column: [Transcribe] | progress 42% [Cancel] | [Download .mid] [Re-run] [Remove] | [Retry]
//                    what the last run did, the detail and the tempo of the file
//
// Transcribe sends POST /api/jobs/{id}/midi, which answers at once; the page
// reads the song every second while it runs (its event stream closes at once
// for a finished song). A re-run keeps the earlier file until the new one is
// done, so Download stays while it runs. The rules (what each state offers and
// says, the refusals, the file's name) are in laneMidiView.js.
//
// The rows exist only where the waveform column is the SVG overlay (the Web
// Audio engine path), like the EQ and model rows. The song itself is read on
// every load, in every mode, for the Export menu's switch.

import { getMode } from "./mode.js";
import { STEM_COLORS, STEM_DISPLAY } from "./constants.js";
import { mixerEl } from "./state.js";
import { t, plural, onLanguageChange } from "./i18n.js";
import { openKey, parseOpenStore, withOpenChoice } from "./eq/eqPanelKinds.js";
import {
  EXPORT_MIDI_STORE_KEY, POLL_FAIL_LIMIT, POLL_MS, REMOVE_ARM_MS,
  anyRunning, chipLabel, chipMark, chipState, hasFinishedMidi, hasTranscriptionFields, isStatus,
  laneHasMidi, maxMinutes, midiFilename, midiOf, midiUrl, parseExportMidi, progressText, refusalNote,
  rowView, transcriptionFields, zipWantsMidi,
} from "./laneMidiView.js";
import {
  buildSourceSwitch, checkEngine, paintDownloads, paintSourceSwitch, setPlayContext, soundNote, syncRolls,
} from "./laneMidiPlay.js";

// Two notes on a stem, in the left rail's line style (the Model chip's
// microchip is drawn the same way, laneModelView.js).
const MIDI_ICON =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
  + '<path d="M9 17V5l10-2v12"/><circle cx="6.5" cy="17" r="2.5"/><circle cx="16.5" cy="15" r="2.5"/>'
  + "</svg>";

/** Height of a MIDI row, px, the same in both columns. */
export const MIDI_PANEL_HEIGHT = 84;
const OPEN_STORE_KEY = "stemdeck.midiPanels";
const READ_RETRY_MS = 30000;

let _want = false;
let _jobId = null;
let _lanes = [];
let _store = null;
// The open song's job state (status, title, midi, stem_reextract), the
// Transcription fields of GET /api/settings, and GET /api/transcribe/status.
let _job = null;
let _settings = {};
let _status = null;
let _statusLoad = null;
let _statusFailedAt = 0;
let _settingsLoad = null;
let _settingsFailedAt = 0;
// lane -> the row's elements and its Remove arm.
const _rows = new Map();
// What the row says about a request this page sent: lane -> { kind, text }.
const _notes = new Map();
// The song being read while a run goes: { jobId, timer, fails }.
let _poll = null;
// A request this page is sending (Transcribe, Cancel, Remove): lane.
let _sending = null;
// The open rows the lanes were last laid out for, so a change says so.
let _laidOut = "";

const enabled = () => _want && !!_jobId && getMode() === "daw";

// ─── Open state ───

function loadStore() {
  if (_store) return _store;
  let raw = null;
  try {
    raw = localStorage.getItem(OPEN_STORE_KEY);
  } catch (e) {
    console.warn("[midi] reading the open MIDI rows failed:", e);
  }
  _store = parseOpenStore(raw);
  return _store;
}

function saveOpen(lane, open) {
  _store = withOpenChoice(loadStore(), openKey(_jobId, lane), open);
  try {
    localStorage.setItem(OPEN_STORE_KEY, JSON.stringify(_store));
  } catch (e) {
    console.warn("[midi] saving the open MIDI rows failed:", e);
  }
}

function chipFor(lane) {
  return chipState({ lane, settings: _settings, status: _status, job: _job });
}

/** Whether the lane's chip shows (its setting, the song, the server). */
function chipShows(lane) {
  return enabled() && _lanes.includes(lane) && chipFor(lane).show;
}

export function isMidiOpen(lane) {
  return chipShows(lane) && loadStore()[openKey(_jobId, lane)] === true;
}

/** Height the open MIDI rows add to the lane stack (px). */
export function midiPanelsHeight() {
  if (!enabled()) return 0;
  return _lanes.filter(isMidiOpen).length * MIDI_PANEL_HEIGHT;
}

/** The song and its lanes (eqRows.setEqContext passes them on). */
export function setMidiContext({ jobId, lanes, enabled: want }) {
  const changed = (jobId || null) !== _jobId;
  if (changed) {
    for (const lane of [..._rows.keys()]) dropRows(lane);
    _notes.clear();
    stopPoll();
    _job = null;
    _sending = null;
    setPlayContext({ jobId: jobId || null, job: null, onChange: paintAll });
  }
  _jobId = jobId || null;
  _lanes = (Array.isArray(lanes) ? lanes : []).filter(laneHasMidi);
  _want = !!want;
  _laidOut = openSignature();
  if (!enabled()) for (const lane of [..._rows.keys()]) dropRows(lane);
  paintExport();
  // Every load of the song brings its state (the Export menu's switch reads
  // it in every mode), and the settings and the engine once the chips can show.
  if (_jobId && changed) readSong(_jobId);
  if (enabled()) {
    loadSettings();
    loadStatus();
  }
}

function setOpen(lane, open) {
  if (!chipShows(lane)) return;
  saveOpen(lane, !!open);
  if (!open) dropRows(lane);
  _laidOut = openSignature();
  document.dispatchEvent(new CustomEvent("stemdeck:eq-rows-changed", { detail: { lane, open: !!open } }));
}

/** Which rows are open, so a change of what shows can lay the lanes out again. */
function openSignature() {
  return enabled() ? _lanes.filter(isMidiOpen).join(",") : "";
}

/** The chips or the rows that show changed (a setting, the song, the engine). */
function afterChange() {
  const sig = openSignature();
  if (sig !== _laidOut) {
    _laidOut = sig;
    for (const lane of [..._rows.keys()]) if (!isMidiOpen(lane)) dropRows(lane);
    document.dispatchEvent(new CustomEvent("stemdeck:eq-rows-changed", { detail: { midi: true } }));
  }
  syncTabs();
  paintAll();
  paintExport();
  paintDownloads();
  if (enabled()) syncRolls(_lanes, (lane) => isMidiOpen(lane) && !!midiOf(_job, lane)?.has_file);
}

// ─── Data ───

const jobPath = (jobId) => `/api/jobs/${encodeURIComponent(jobId)}`;

async function readSong(jobId) {
  let state = null;
  try {
    const r = await fetch(jobPath(jobId), { cache: "no-store" });
    if (r.ok) state = await r.json();
  } catch (e) {
    console.warn("[midi] reading the song failed:", e);
  }
  if (jobId !== _jobId || !state) return;
  takeJob(state);
}

/** Take a job state (a poll, the first read). */
function takeJob(state) {
  if (!state || typeof state !== "object") return;
  const id = state.job_id ?? state.id ?? _jobId;
  if (id !== _jobId) return;
  _job = {
    job_id: _jobId,
    status: state.status ?? null,
    title: typeof state.title === "string" ? state.title : "",
    duration: Number(state.duration) || 0,
    midi: state.midi ?? null,
    stem_reextract: state.stem_reextract ?? null,
  };
  if (anyRunning(_job)) startPoll(_jobId);
  setPlayContext({ jobId: _jobId, job: _job, onChange: paintAll });
  afterChange();
}

function loadSettings() {
  if (_settingsLoad) return _settingsLoad;
  if (hasTranscriptionFields(_settings)) return null;
  if (_settingsFailedAt && Date.now() - _settingsFailedAt < READ_RETRY_MS) return null;
  _settingsLoad = (async () => {
    try {
      const r = await fetch("/api/settings", { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      _settings = { ..._settings, ...transcriptionFields(await r.json()) };
      _settingsFailedAt = 0;
    } catch (e) {
      console.warn("[midi] reading the transcription settings failed:", e);
      _settingsFailedAt = Date.now();
    } finally {
      _settingsLoad = null;
    }
    afterChange();
  })();
  return _settingsLoad;
}

function loadStatus() {
  if (_statusLoad || isStatus(_status)) return _statusLoad;
  if (_statusFailedAt && Date.now() - _statusFailedAt < READ_RETRY_MS) return null;
  _statusLoad = (async () => {
    try {
      const r = await fetch("/api/transcribe/status", { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      _status = isStatus(data) ? data : null;
      _statusFailedAt = _status ? 0 : Date.now();
    } catch (e) {
      // An older server has no transcription: the chips stay away.
      console.warn("[midi] reading whether transcription can run failed:", e);
      _statusFailedAt = Date.now();
    } finally {
      _statusLoad = null;
    }
    afterChange();
  })();
  return _statusLoad;
}

async function send(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    console.warn("[midi] the request did not reach the server:", e);
    return { ok: false, status: 0, data: null };
  }
  const data = await res.json().catch(() => null);
  return { ok: res.ok, status: res.status, data };
}

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
    console.warn("[midi] reading the song while it is transcribed failed:", e);
  }
  if (_poll !== p) return;
  if (!state) {
    p.fails += 1;
    if (p.fails >= POLL_FAIL_LIMIT) {
      stopPoll();
      for (const lane of _rows.keys()) _notes.set(lane, { kind: "error", text: t("laneMidi.lost") });
      paintAll();
      return;
    }
    p.timer = setTimeout(pollOnce, POLL_MS);
    return;
  }
  // Stopped polling first: takeJob starts it again while a run goes on.
  _poll = null;
  takeJob(state);
}

// ─── Actions ───

function displayName(lane) {
  return STEM_DISPLAY[lane] || lane;
}

function noteFor(lane, res) {
  return refusalNote(res.status, res.data, { name: displayName(lane), maxMin: maxMinutes(_status) }, t);
}

/** Transcribe, Re-run and Retry: the same request, with the Settings detail. */
async function transcribe(lane) {
  const jobId = _jobId;
  if (!jobId || _sending) return;
  _sending = lane;
  _notes.delete(lane);
  disarmRemove(lane);
  paintRow(lane);
  const res = await send("POST", `${jobPath(jobId)}/midi`, { stem: lane });
  _sending = null;
  if (jobId !== _jobId) return;
  if (res.ok) {
    // Running from now: the earlier file (a re-run) stays until the new one
    // is done. The answer came before the run's first step, so the song is
    // read from here.
    const prev = _job?.midi && typeof _job.midi === "object" ? _job.midi[lane] : null;
    _job = { ...(_job || { job_id: jobId }), midi: { ...(_job?.midi || {}), [lane]: { ...(prev || {}), state: "running", pct: 0, error_code: null } } };
    startPoll(jobId, 0);
    afterChange();
  } else {
    _notes.set(lane, noteFor(lane, res));
  }
  paintRow(lane);
}

async function cancel(lane) {
  const jobId = _jobId;
  if (!jobId || _sending) return;
  _sending = lane;
  paintRow(lane);
  const res = await send("POST", `${jobPath(jobId)}/midi/${encodeURIComponent(lane)}/cancel`);
  _sending = null;
  if (jobId !== _jobId) return;
  if (!res.ok) {
    console.warn("[midi] the cancel was refused:", res.status, res.data);
    _notes.set(lane, noteFor(lane, res));
  }
  // The run's state says "cancelled" on the next read.
  startPoll(jobId, 0);
  paintRow(lane);
}

/**
 * Remove: the lane's .mid file is deleted. Two presses, as the track's Delete
 * tool: the first arms it and says to press again, the second within
 * REMOVE_ARM_MS sends it.
 */
async function remove(lane) {
  const r = _rows.get(lane);
  const jobId = _jobId;
  if (!r || !jobId || _sending) return;
  if (!r.removeArmed) {
    r.removeArmed = true;
    clearTimeout(r.removeTimer);
    r.removeTimer = setTimeout(() => {
      r.removeArmed = false;
      paintRow(lane);
    }, REMOVE_ARM_MS);
    paintRow(lane);
    return;
  }
  disarmRemove(lane);
  _sending = lane;
  paintRow(lane);
  const res = await send("DELETE", `${jobPath(jobId)}/midi/${encodeURIComponent(lane)}`);
  _sending = null;
  if (jobId !== _jobId) return;
  if (res.ok) {
    _notes.set(lane, { kind: "done", text: t("laneMidi.removed") });
    await readSong(jobId);
  } else {
    _notes.set(lane, noteFor(lane, res));
  }
  paintRow(lane);
}

function disarmRemove(lane) {
  const r = _rows.get(lane);
  if (!r) return;
  clearTimeout(r.removeTimer);
  r.removeArmed = false;
}

// ─── Elements ───

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function button(cls, onClick) {
  const b = el("button", `eq-btn model-btn ${cls}`);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

function laneMixRow(lane) {
  return mixerEl?.querySelector(`.lane-header[data-stem="${CSS.escape(lane)}"]`) || null;
}
function laneWaveRow(lane) {
  return document.querySelector(`.stem-waveform-layer .stem-waveform-row[data-stem="${CSS.escape(lane)}"]`) || null;
}
function laneSlot(lane) {
  return laneMixRow(lane)?.querySelector(":scope > .mx-model-slot") || null;
}

// ─── Chips ───

function ensureTab(lane) {
  const slot = laneSlot(lane);
  if (!slot) return;
  let tab = slot.querySelector(":scope > .midi-tab");
  if (!tab) {
    // The Model chip's face (daw.css .mx-model-slot > .model-tab).
    tab = el("button", "eq-tab midi-tab");
    tab.type = "button";
    tab.dataset.midiLane = lane;
    const face = el("span", "midi-tab-face");
    face.setAttribute("aria-hidden", "true");
    face.innerHTML = MIDI_ICON;
    const mark = el("span", "midi-tab-mark");
    mark.setAttribute("aria-hidden", "true");
    tab.append(face, mark);
    tab.addEventListener("click", (e) => {
      e.stopPropagation();
      // The lane's own state, not the attribute: player.js sets and clears
      // aria-disabled on every control of a lane header it redraws.
      if (chipFor(lane).off) return;
      setOpen(lane, !isMidiOpen(lane));
    });
    // Under the Model chip: after it in the DOM when it is there (the CSS
    // order keeps it below when the Model chip comes later).
    slot.append(tab);
  }
  slot.classList.add("has-midi");
  paintTab(tab, lane);
}

function paintTab(tab, lane) {
  if (!tab) return;
  const chip = chipFor(lane);
  const m = midiOf(_job, lane);
  const open = isMidiOpen(lane);
  const mark = chip.off ? "" : chipMark(m);
  tab.classList.toggle("open", open);
  tab.classList.toggle("off", chip.off);
  tab.dataset.mark = mark;
  tab.classList.toggle("viewing", open && !!midiOf(_job, lane)?.has_file);
  tab.querySelector(".midi-tab-mark").textContent = mark === "run" ? progressText(m) : "";
  if (chip.off) tab.setAttribute("aria-disabled", "true");
  else tab.removeAttribute("aria-disabled");
  tab.setAttribute("aria-expanded", String(open));
  if (open) tab.setAttribute("aria-controls", `midi-panel-${lane}`);
  else tab.removeAttribute("aria-controls");
  const label = chipLabel(m, { name: displayName(lane), off: chip.off, reasonKey: chip.reasonKey }, t);
  tab.setAttribute("aria-label", label);
  tab.title = label;
}

function dropTab(tab) {
  tab.parentElement?.classList.remove("has-midi");
  tab.remove();
}

function syncTabs() {
  const keep = enabled() ? _lanes.filter(chipShows) : [];
  for (const tab of document.querySelectorAll(".mx-model-slot > .midi-tab")) {
    if (!keep.includes(tab.dataset.midiLane)) dropTab(tab);
  }
  for (const lane of keep) ensureTab(lane);
}

// ─── Rows ───

function dropRows(lane) {
  const r = _rows.get(lane);
  if (!r) return;
  clearTimeout(r.removeTimer);
  r.waveRow.remove();
  r.mixRow.remove();
  _rows.delete(lane);
}

function buildRows(lane) {
  const h = MIDI_PANEL_HEIGHT;
  const color = STEM_COLORS[lane] || "#a0a0a0";
  const name = displayName(lane);

  const waveRow = el("div", "stem-waveform-row eq-panel-lane midi-panel-lane");
  waveRow.dataset.midiLane = lane;
  waveRow.id = `midi-panel-${lane}`;
  waveRow.setAttribute("role", "region");
  waveRow.style.setProperty("--stem-color", color);
  waveRow.style.flex = `0 0 ${h}px`;
  waveRow.style.height = `${h}px`;
  const body = el("div", "eq-panel-body model-panel-body midi-panel-body");
  // The row's clicks and keys are its own: the waves column would start a
  // seek or a loop drag on pointerdown.
  body.addEventListener("pointerdown", (e) => e.stopPropagation());
  body.addEventListener("click", (e) => e.stopPropagation());
  body.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });

  const line = el("div", "model-panel-line midi-panel-line");
  const transcribeBtn = button("midi-transcribe", () => transcribe(lane));
  const rerunBtn = button("midi-rerun", () => transcribe(lane));
  const retryBtn = button("midi-retry", () => transcribe(lane));
  const run = el("div", "model-run midi-run");
  const progress = el("progress", "model-progress midi-progress");
  progress.max = 100;
  const pct = el("span", "model-pct midi-pct");
  const cancelBtn = button("midi-cancel", () => cancel(lane));
  run.append(progress, pct, cancelBtn);
  // The file: the app's stem download link (main.js saves an a.lane-dl
  // through the desktop shell, a browser downloads it by its name).
  const download = el("a", "eq-btn model-btn lane-dl midi-download");
  const removeBtn = button("midi-remove", () => remove(lane));
  // Stem, MIDI or Both: what the lane plays (laneMidiPlay.js).
  const source = buildSourceSwitch(lane);
  line.append(transcribeBtn, rerunBtn, retryBtn, run, download, removeBtn, source);
  const status = el("p", "model-status midi-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const hint = el("p", "model-hint midi-hint");
  body.append(line, status, hint);
  waveRow.append(body);

  const mixRow = el("div", "lane-header mx-row eq-panel-row midi-panel-row");
  mixRow.dataset.midiLane = lane;
  mixRow.style.height = `${h}px`;
  mixRow.style.minHeight = `${h}px`;
  mixRow.style.setProperty("--stem-color", color);
  const side = el("div", "eq-side model-side");
  const head = el("div", "eq-side-head");
  const titles = el("div", "eq-side-titles");
  const titleEl = el("span", "eq-side-title model-side-title");
  titleEl.style.color = color;
  const laneEl = el("span", "eq-side-lane", name);
  titles.append(titleEl, laneEl);
  head.append(titles);
  side.append(head);
  mixRow.append(side);

  return {
    waveRow, mixRow, transcribe: transcribeBtn, rerun: rerunBtn, retry: retryBtn, run, progress, pct,
    cancel: cancelBtn, download, remove: removeBtn, source, status, hint, titleEl, laneEl, removeArmed: false, removeTimer: 0,
  };
}

function paintButton(btn, spec, text, title) {
  btn.hidden = !spec.show;
  btn.textContent = text;
  btn.title = title;
  if (btn.tagName === "BUTTON") btn.disabled = spec.disabled;
}

function paintRow(lane) {
  const r = _rows.get(lane);
  if (!r) return;
  const name = displayName(lane);
  const m = midiOf(_job, lane);
  const otherRunning = _lanes.some((l) => l !== lane && midiOf(_job, l)?.state === "running")
    || _job?.stem_reextract?.state === "running";
  const v = rowView(m, {
    name, sending: _sending !== null, otherRunning, removeArmed: r.removeArmed, maxMin: maxMinutes(_status),
  }, t, plural);
  if (!v.buttons.remove.show) r.removeArmed = false;

  r.titleEl.textContent = t("laneMidi.panel.title");
  r.laneEl.textContent = name;
  r.laneEl.title = name;
  r.waveRow.setAttribute("aria-label", t("laneMidi.panel.regionAria", { name }));

  paintButton(r.transcribe, v.buttons.transcribe, t("laneMidi.transcribe"), t("laneMidi.transcribeTitle", { name }));
  paintButton(r.rerun, v.buttons.rerun, t("laneMidi.rerun"), t("laneMidi.rerunTitle", { name }));
  paintButton(r.retry, v.buttons.retry, t("laneMidi.retry"), t("laneMidi.retryTitle", { name }));
  paintButton(r.cancel, v.buttons.cancel, t("laneMidi.cancel"), t("laneMidi.cancelTitle", { name }));
  r.run.hidden = !v.run;
  r.progress.value = v.pct;
  r.progress.setAttribute("aria-label", t("laneMidi.progressAria", { name }));
  r.pct.textContent = v.run ? progressText(m) : "";

  paintButton(r.download, v.buttons.download, t("laneMidi.download"), t("laneMidi.downloadTitle", { name }));
  if (v.buttons.download.show) {
    r.download.href = midiUrl(_jobId, lane, m);
    r.download.download = midiFilename(_job?.title, lane);
  } else {
    r.download.removeAttribute("href");
    r.download.removeAttribute("download");
  }

  const armed = r.removeArmed && v.buttons.remove.show;
  paintButton(r.remove, v.buttons.remove, t(armed ? "laneMidi.removeAgain" : "laneMidi.remove"),
    armed ? t("laneMidi.removeAgain") : t("laneMidi.removeTitle", { name }));
  r.remove.classList.toggle("armed", armed);

  paintSourceSwitch(r.source, lane);

  // The status line: a request's own answer first, then the sound being
  // prepared or refused, then the lane's state.
  const note = _notes.get(lane);
  const status = (note && !(m?.state === "running" && note.kind !== "error") ? note : null) || soundNote(lane) || v.status;
  r.status.textContent = status.text;
  r.status.title = status.text;
  r.status.dataset.kind = status.kind;
  r.hint.textContent = v.hint;
  r.hint.title = v.hint;
}

function paintAll() {
  for (const tab of document.querySelectorAll(".mx-model-slot > .midi-tab[data-midi-lane]")) paintTab(tab, tab.dataset.midiLane);
  for (const lane of _rows.keys()) paintRow(lane);
}

/** Put the chips and the open rows in place (eqRows.syncEqRows calls it). */
export function syncMidiRows() {
  // The download buttons were redrawn with the lanes: their choice comes back.
  paintDownloads();
  checkEngine();
  if (!enabled()) {
    for (const lane of [..._rows.keys()]) dropRows(lane);
    syncTabs();
    syncRolls([], () => false);
    return;
  }
  loadSettings();
  loadStatus();
  syncTabs();
  for (const lane of [..._rows.keys()]) if (!isMidiOpen(lane)) dropRows(lane);
  for (const lane of _lanes) {
    if (!isMidiOpen(lane)) continue;
    const waveLane = laneWaveRow(lane);
    const mixLane = laneMixRow(lane);
    if (!waveLane || !mixLane) continue;
    let r = _rows.get(lane);
    if (!r) {
      r = buildRows(lane);
      _rows.set(lane, r);
    }
    // After the lane's EQ, effects and model rows when they are open, else
    // right after the lane.
    const waveAfter = lastPanelAfter(waveLane, lane);
    const mixAfter = lastPanelAfter(mixLane, lane);
    if (waveAfter.nextElementSibling !== r.waveRow) waveAfter.after(r.waveRow);
    if (mixAfter.nextElementSibling !== r.mixRow) mixAfter.after(r.mixRow);
    paintRow(lane);
  }
  _laidOut = openSignature();
  syncMidiRowOrder();
  // The MIDI view: the roll over the waveform of each lane whose row is open
  // and which has a file.
  syncRolls(_lanes, (lane) => isMidiOpen(lane) && !!midiOf(_job, lane)?.has_file);
}

/** The last of the lane's own EQ, effects and model rows right after it, else the lane. */
function lastPanelAfter(laneEl, lane) {
  let at = laneEl;
  const esc = CSS.escape(lane);
  const own = `.eq-panel-lane[data-eq-lane="${esc}"], .eq-panel-lane[data-fx-lane="${esc}"], .eq-panel-lane[data-model-lane="${esc}"],`
    + ` .eq-panel-row[data-eq-lane="${esc}"], .eq-panel-row[data-fx-lane="${esc}"], .eq-panel-row[data-model-lane="${esc}"]`;
  for (;;) {
    const next = at.nextElementSibling;
    if (next?.matches?.(own)) at = next;
    else return at;
  }
}

/** Each row's CSS order: the lane's + 1, as the EQ row (DOM order breaks the tie). */
export function syncMidiRowOrder() {
  for (const [lane, r] of _rows) {
    const wo = Number(laneWaveRow(lane)?.style.order);
    const m = laneMixRow(lane);
    const mo = m?.style.order === "" ? NaN : Number(m?.style.order);
    r.waveRow.style.order = Number.isFinite(wo) ? String(wo + 1) : "";
    r.mixRow.style.order = Number.isFinite(mo) ? String(mo + 1) : "";
  }
}

// ─── Export: Include MIDI ───

let _exportWired = false;

function exportEls() {
  return { wrap: document.getElementById("t-export-midi-wrap"), box: document.getElementById("t-export-midi") };
}

function wireExport() {
  if (_exportWired) return;
  const { box } = exportEls();
  if (!box) return;
  _exportWired = true;
  let raw = null;
  try {
    raw = localStorage.getItem(EXPORT_MIDI_STORE_KEY);
  } catch (e) {
    console.warn("[midi] reading Include MIDI failed:", e);
  }
  box.checked = parseExportMidi(raw);
  box.addEventListener("change", () => {
    try {
      localStorage.setItem(EXPORT_MIDI_STORE_KEY, box.checked ? "1" : "0");
    } catch (e) {
      console.warn("[midi] saving Include MIDI failed:", e);
    }
  });
}

/** The switch shows only while the open song has a MIDI file. */
function paintExport() {
  wireExport();
  const { wrap } = exportEls();
  if (wrap) wrap.hidden = !(_jobId && hasFinishedMidi(_job));
}

/** Whether the stems zip of the open song asks for its MIDI files (player.js). */
export function exportMidiWanted(jobId) {
  const { box } = exportEls();
  return !!jobId && jobId === _jobId && zipWantsMidi(box?.checked === true, _job);
}

// ─── Events ───

onLanguageChange(paintAll);

// A change in Settings > Transcription, or any settings answer the dialog
// applied: the chips follow at once. Captured: the settings overlay
// dispatches on itself, and the event does not bubble.
document.addEventListener("stemdeck:settings-applied", (e) => {
  if (!hasTranscriptionFields(e.detail)) return;
  _settings = { ..._settings, ...transcriptionFields(e.detail) };
  afterChange();
}, true);
