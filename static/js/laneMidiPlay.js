// Seeing, hearing and downloading the MIDI of a lane (Advanced mode for the
// first two, every mode for the download), on top of the MIDI row of
// laneMidiRows.js, which calls in here.
//
// SEE: while a lane's MIDI row is open and the lane has a MIDI file, its notes
// (GET /api/jobs/{id}/midi/{stem}/notes, read once per file) are drawn over
// the lane's waveform as a piano roll: one SVG in the lane's waveform row,
// its viewBox in seconds and stretched over the row like the waveform's own
// SVG, so the timeline, the zoom (the --zoom width of the canvas), the scroll
// and the playhead line are the waveform's without a render loop of its own.
// The waveform stays under it, dimmed. On the Drums lane each row is a kind
// of drum, named at the row's left (midiNotesView.drumGeometry). A redraw of
// the lanes rewrites the row, and syncMidiRows (called after every redraw)
// puts the roll back.
//
// HEAR: the row's source switch (Stem, MIDI, Both). MIDI renders the notes
// once to a mono buffer (midiSynth.js, midiRender.js) and hands it to the engine as the
// lane's alternate source (addAltSource, on the full-decode and the chunked
// engine alike), so everything the lane follows applies to it. When the
// audio engine did not start for the song, the switch is off and says so.
//
// DOWNLOAD: the lane's download button (mixer.js a.lane-dl) offers Audio,
// MIDI, or Audio and MIDI (one zip) while the lane has a MIDI file; without
// one it is exactly the plain download. Audio is the button's own download;
// the other two go through a link of the same kind, so the desktop app saves
// them the same way (main.js, externalLinks.js). A drag still carries the
// audio (main.js dragstart reads the button's href).

import { mixerEl, audioEngine } from "./state.js";
import { STEM_DISPLAY } from "./constants.js";
import { t } from "./i18n.js";
import { MIDI_LANES, midiFilename, midiOf, midiUrl, safeTitle } from "./laneMidiView.js";
import {
  bothZipName, bothZipUrl, cleanNotes, downloadChoices, engineSource, laneGeometry, needsMidiSound, normalSource,
  rollRects, sourceSwitch,
} from "./midiNotesView.js";
import { canRender, forgetSounds, laneSound } from "./midiSynth.js";

let _jobId = null;
let _job = null;
// lane -> { at, notes } once read; lane -> promise while reading.
const _notes = new Map();
const _reading = new Map();
// jobId:lane -> "stem" | "midi" | "both", for this page.
const _source = new Map();
// lane -> a sentence about the sound (preparing, failed), shown in the row.
const _soundNote = new Map();
// The engine the lanes' sources were last given to, and what each lane has
// in it: lane -> the file time ("at") of the rendering added.
let _engine = null;
const _added = new Map();
// lane -> the roll's SVG, kept across the redraws that rewrite the row; and
// the Drums lane's row names beside it.
const _rolls = new Map();
const _rollLabels = new Map();
let _onChange = () => {};

const sourceKey = (lane) => `${_jobId}:${lane}`;
const display = (lane) => STEM_DISPLAY[lane] || lane;

/** The song and its state (laneMidiRows.takeJob), and what to call when the row needs a repaint. */
export function setPlayContext({ jobId, job, onChange }) {
  if (onChange) _onChange = onChange;
  if ((jobId || null) !== _jobId) {
    _jobId = jobId || null;
    _notes.clear();
    _reading.clear();
    _soundNote.clear();
    _added.clear();
    for (const svg of _rolls.values()) svg.remove();
    _rolls.clear();
    for (const el of _rollLabels.values()) el.remove();
    _rollLabels.clear();
    forgetSounds(_jobId);
  }
  _job = job || null;
  closeMenu();
  paintDownloads();
  if (_jobId) syncSources();
}

// ─── The notes ───

function fileOf(lane) {
  const m = midiOf(_job, lane);
  return m?.has_file ? m : null;
}

/** The lane's notes for its current file, or null (read on demand). */
function notesOf(lane) {
  const m = fileOf(lane);
  if (!m) return null;
  const held = _notes.get(lane);
  if (held && held.at === m.at) return held.notes;
  readNotes(lane, m);
  return null;
}

function readNotes(lane, m) {
  const key = `${lane}|${m.at}`;
  if (_reading.has(key)) return _reading.get(key);
  const jobId = _jobId;
  const p = (async () => {
    try {
      const v = m.at ? `?v=${encodeURIComponent(m.at)}` : "";
      const r = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/midi/${encodeURIComponent(lane)}/notes${v}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const notes = cleanNotes(await r.json());
      if (jobId !== _jobId) return null;
      _notes.set(lane, { at: m.at, notes });
      // The roll waited for them.
      syncRolls(_lastRolls.lanes, _lastRolls.viewOn);
      _onChange();
      syncSources();
      return notes;
    } catch (e) {
      console.warn("[midi] reading the notes failed:", e);
      return null;
    } finally {
      _reading.delete(key);
    }
  })();
  _reading.set(key, p);
  return p;
}

// ─── SEE: the piano roll ───

function songDuration() {
  const d = Number(audioEngine?.getDuration?.());
  if (Number.isFinite(d) && d > 0) return d;
  const j = Number(_job?.duration);
  return Number.isFinite(j) && j > 0 ? j : 0;
}

function waveRowOf(lane) {
  return document.querySelector(`.stem-waveform-layer > .stem-waveform-row[data-stem="${CSS.escape(lane)}"]`);
}

function dropRoll(lane) {
  _rolls.get(lane)?.remove();
  _rolls.delete(lane);
  _rollLabels.get(lane)?.remove();
  _rollLabels.delete(lane);
  waveRowOf(lane)?.classList.remove("midi-view");
}

/**
 * Draw or take away each lane's roll: `viewOn(lane)` says whether its MIDI
 * view is on (its row open). Called after every redraw of the lanes.
 */
let _lastRolls = { lanes: [], viewOn: () => false };

export function syncRolls(lanes, viewOn) {
  _lastRolls = { lanes, viewOn };
  for (const lane of [..._rolls.keys()]) if (!lanes.includes(lane) || !viewOn(lane)) dropRoll(lane);
  for (const lane of lanes) {
    if (!viewOn(lane)) continue;
    const row = waveRowOf(lane);
    const notes = notesOf(lane);
    const duration = songDuration();
    if (!row || !notes || !duration) continue;
    const m = fileOf(lane);
    let svg = _rolls.get(lane);
    const sig = `${m.at}|${duration}|${notes.length}`;
    if (!svg || svg.dataset.sig !== sig) {
      svg?.remove();
      _rollLabels.get(lane)?.remove();
      _rollLabels.delete(lane);
      const g = laneGeometry(lane, notes, duration);
      if (!g) continue;
      svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("class", "midi-roll");
      svg.setAttribute("viewBox", `0 0 ${g.width} ${g.height}`);
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("role", "img");
      svg.dataset.sig = sig;
      svg.dataset.midiLane = lane;
      svg.dataset.lo = String(g.lo);
      svg.dataset.hi = String(g.hi);
      svg.innerHTML = rollRects(g);
      _rolls.set(lane, svg);
      if (g.rows) _rollLabels.set(lane, rollLabels(g));
    }
    svg.setAttribute(
      "aria-label",
      t(lane === "drums" ? "laneMidi.roll.drumsAria" : "laneMidi.roll.aria", { name: display(lane) }),
    );
    if (svg.parentElement !== row) row.append(svg);
    const labels = _rollLabels.get(lane);
    if (labels) {
      for (const [i, el] of [...labels.children].entries()) el.textContent = t(labels.dataset.keys.split(",")[i]);
      if (labels.parentElement !== row) row.append(labels);
    }
    row.classList.add("midi-view");
  }
}

/** The Drums lane's row names, one per row of its roll, each at its row's height. */
function rollLabels(g) {
  const box = document.createElement("div");
  box.className = "midi-roll-labels";
  box.setAttribute("aria-hidden", "true");
  box.dataset.keys = g.rows.map((r) => r.key).join(",");
  for (const r of g.rows) {
    const el = document.createElement("span");
    el.className = "midi-roll-label";
    el.dataset.row = r.row;
    el.style.top = `${(r.y / g.height) * 100}%`;
    el.style.height = `${100 / g.height}%`;
    box.append(el);
  }
  return box;
}

// ─── HEAR: the source switch ───

const engineSupports = () => audioEngine?.supportsAltSource === true && canRender(audioEngine.audioContext);

export function laneSource(lane) {
  return normalSource(_source.get(sourceKey(lane)));
}

/** The switch, made once per row (laneMidiRows.buildRows). */
export function buildSourceSwitch(lane) {
  const group = document.createElement("div");
  group.className = "midi-source";
  group.setAttribute("role", "group");
  const label = document.createElement("span");
  label.className = "midi-source-label";
  group.append(label);
  for (const source of ["stem", "midi", "both"]) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "eq-btn model-btn midi-source-btn";
    b.dataset.source = source;
    b.addEventListener("click", () => chooseSource(lane, source));
    group.append(b);
  }
  return group;
}

/** Paint the switch for the lane's state (laneMidiRows.paintRow). */
export function paintSourceSwitch(group, lane) {
  const preparing = _soundNote.get(lane)?.kind === "running";
  const view = sourceSwitch({ hasFile: !!fileOf(lane), engineSupports: engineSupports(), preparing });
  group.hidden = !view.show;
  const name = display(lane);
  const current = laneSource(lane);
  const reason = view.reasonKey ? t(view.reasonKey) : "";
  group.setAttribute("aria-label", t("laneMidi.source.aria", { name }));
  group.title = reason;
  group.querySelector(".midi-source-label").textContent = t("laneMidi.source.label");
  for (const b of group.querySelectorAll(".midi-source-btn")) {
    const s = b.dataset.source;
    b.textContent = t(`laneMidi.source.${s}`);
    b.title = reason || t(`laneMidi.source.${s}Title`, { name });
    b.setAttribute("aria-pressed", String(s === current));
    b.disabled = view.disabled;
  }
}

/** A sentence about the lane's sound for the row's status line, or null. */
export function soundNote(lane) {
  return _soundNote.get(lane) || null;
}

function chooseSource(lane, source) {
  _source.set(sourceKey(lane), normalSource(source));
  _soundNote.delete(lane);
  syncSources();
  _onChange();
}

// One pass at a time: a pass waits on a rendering, and a second pass started
// meanwhile would add the same sound twice.
let _chain = Promise.resolve();

/** Bring every lane's engine source in line with its switch and its file. */
function syncSources() {
  _chain = _chain.then(syncPass).catch((e) => console.warn("[midi] matching the lanes' sources failed:", e));
  return _chain;
}

async function syncPass() {
  const engine = audioEngine;
  if (engine !== _engine) {
    _engine = engine;
    _added.clear();
  }
  if (!engine?.supportsAltSource || !_jobId) return;
  for (const lane of MIDI_LANES) {
    const m = fileOf(lane);
    const source = m ? laneSource(lane) : "stem";
    if (!m) {
      if (_added.has(lane)) {
        engine.setLaneSource(lane, "stem");
        engine.removeAltSource(lane);
        _added.delete(lane);
      }
      continue;
    }
    if (!needsMidiSound(source)) {
      engine.setLaneSource(lane, "stem");
      continue;
    }
    if (_added.get(lane) === m.at) {
      engine.setLaneSource(lane, engineSource(source));
      continue;
    }
    await addSound(engine, lane, m);
  }
}

async function addSound(engine, lane, m) {
  const notes = _notes.get(lane)?.at === m.at ? _notes.get(lane).notes : await readNotes(lane, m);
  if (!notes || engine !== audioEngine) return;
  const jobId = _jobId;
  _soundNote.set(lane, { kind: "running", text: t("laneMidi.source.preparing", { name: display(lane) }) });
  _onChange();
  try {
    // The stems first: the lane's track has to be there to follow.
    await engine.ready;
    const buffer = await laneSound({
      jobId, stem: lane, at: m.at, notes, duration: engine.getDuration?.() || songDuration(),
      context: engine.audioContext,
      // At the stem's loudness, so the switch does not jump in level.
      reference: engine.getBuffers?.().get(lane) || null,
    });
    if (jobId !== _jobId || engine !== audioEngine || engine.isDestroyed?.()) return;
    if (!engine.addAltSource(lane, buffer)) throw new Error("the lane has no track");
    _added.set(lane, m.at);
    engine.setLaneSource(lane, engineSource(laneSource(lane)));
    _soundNote.delete(lane);
  } catch (e) {
    console.warn("[midi] the MIDI sound could not be added:", e);
    _soundNote.set(lane, { kind: "error", text: t("laneMidi.source.failed") });
    _source.set(sourceKey(lane), "stem");
    engine.setLaneSource?.(lane, "stem");
  }
  _onChange();
}

/** The engine may have been replaced (a new load of the song): give it the sources again. */
export function checkEngine() {
  if (audioEngine !== _engine) syncSources();
}

// ─── DOWNLOAD: the lane's download button ───

let _menu = null;

function laneDl(lane) {
  return mixerEl?.querySelector(`.lane-header[data-stem="${CSS.escape(lane)}"] a.lane-dl`) || null;
}

/** Mark the buttons that offer a choice (aria-haspopup), for the Guide and assistive tech. */
export function paintDownloads() {
  for (const lane of MIDI_LANES) {
    const a = laneDl(lane);
    if (!a) continue;
    if (downloadChoices({ hasMidi: !!fileOf(lane) }).length) {
      a.setAttribute("aria-haspopup", "menu");
      a.dataset.midiChoice = lane;
    } else {
      a.removeAttribute("aria-haspopup");
      delete a.dataset.midiChoice;
    }
  }
}

function closeMenu({ focus = false } = {}) {
  if (!_menu) return;
  const { el, anchor } = _menu;
  _menu = null;
  el.remove();
  anchor.setAttribute("aria-expanded", "false");
  document.removeEventListener("pointerdown", onOutside, true);
  document.removeEventListener("keydown", onMenuKey, true);
  if (focus) anchor.focus();
}

function onOutside(e) {
  if (_menu && !_menu.el.contains(e.target) && e.target !== _menu.anchor) closeMenu();
}

function onMenuKey(e) {
  if (!_menu) return;
  const items = [..._menu.el.querySelectorAll(".lane-dl-opt")];
  const at = items.indexOf(document.activeElement);
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    closeMenu({ focus: true });
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const step = e.key === "ArrowDown" ? 1 : -1;
    items[(at + step + items.length) % items.length]?.focus();
  } else if (e.key === "Tab") {
    closeMenu();
  }
}

function openMenu(anchor, lane) {
  closeMenu();
  const el = document.createElement("div");
  el.className = "lane-dl-menu";
  el.setAttribute("role", "menu");
  el.setAttribute("aria-label", t("laneMidi.dl.aria", { name: display(lane) }));
  for (const choice of downloadChoices({ hasMidi: true })) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "lane-dl-opt";
    b.setAttribute("role", "menuitem");
    b.dataset.choice = choice.id;
    b.textContent = t(choice.key);
    b.title = t(`${choice.key}Title`, { name: display(lane) });
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      closeMenu({ focus: true });
      download(anchor, lane, choice.id);
    });
    el.append(b);
  }
  document.body.append(el);
  const r = anchor.getBoundingClientRect();
  const w = el.offsetWidth;
  el.style.left = `${Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w))}px`;
  const below = r.bottom + 4;
  el.style.top = below + el.offsetHeight > window.innerHeight - 8 ? `${Math.max(8, r.top - el.offsetHeight - 4)}px` : `${below}px`;
  anchor.setAttribute("aria-expanded", "true");
  _menu = { el, anchor, lane };
  document.addEventListener("pointerdown", onOutside, true);
  document.addEventListener("keydown", onMenuKey, true);
  el.querySelector(".lane-dl-opt")?.focus();
}

/** A link of the stem download's kind (a.lane-dl), clicked once and gone. */
function clickDownload(href, filename) {
  const a = document.createElement("a");
  a.className = "lane-dl";
  a.href = href;
  a.download = filename;
  a.hidden = true;
  document.body.append(a);
  a.click();
  a.remove();
}

function download(anchor, lane, choice) {
  const m = fileOf(lane);
  if (choice === "audio" || !m) {
    // The button's own download, as without a MIDI file.
    anchor.dataset.midiPass = "1";
    anchor.click();
    delete anchor.dataset.midiPass;
    return;
  }
  if (choice === "midi") {
    clickDownload(midiUrl(_jobId, lane, m), midiFilename(_job?.title, lane));
    return;
  }
  clickDownload(bothZipUrl(_jobId, lane, anchor.getAttribute("href")), bothZipName(safeTitle(_job?.title), lane));
}

// The button's click, before main.js's document handler sees it.
mixerEl?.addEventListener("click", (e) => {
  const anchor = e.target instanceof Element ? e.target.closest("a.lane-dl[data-midi-choice]") : null;
  if (!anchor || anchor.dataset.midiPass === "1") return;
  const lane = anchor.dataset.midiChoice;
  if (!fileOf(lane) || anchor.getAttribute("href") === "#" || anchor.classList.contains("disabled")) return;
  e.preventDefault();
  e.stopPropagation();
  if (_menu?.anchor === anchor) closeMenu({ focus: true });
  else openMenu(anchor, lane);
});
