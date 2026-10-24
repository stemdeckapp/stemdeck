// The Stems control in the mixer header (Practice and Advanced): which stems
// are lanes of their own in this mode and which play inside Original, and the
// voice form. Each mode keeps its own choice (modeStems.js); a change reloads
// the song with its place kept, and is refused while a take is open.
//
// A small popover in the Mode menu's style: one checkbox per stem in the
// song's order, the Voice row (Combined or Lead + Backing, or the one-time
// split when the song has none), a line naming what Original holds, "Use the
// song's stems", and Apply. Keyboard: Enter or Space opens it with the first
// checkbox focused, Tab moves, Escape closes and returns focus.
//
// Below them, the song's versions (#804, versions.js): which one is open (a
// choice opens another, with its own lanes and mix), its name, "Save as new
// version" (the ticked stems and the mix as it is now, under a new name) and
// Delete, which forgets the version's settings and never a stem.

import { t, onLanguageChange } from "./i18n.js";
import { laneLabel, STEM_COLORS } from "./constants.js";
import { mixerState, currentJobId } from "./state.js";
import { onModeChange } from "./mode.js";
import {
  layoutState, changeLayout, requestSplit, onLayoutChange, setLayoutBusyCheck, layoutBlockedReason,
} from "./modeStems.js";
import { BASE_STEMS, sameNames, stemsReasonKey } from "./partChoice.js";
import { isRecording } from "./capture/recordUi.js";
import { isStreamerTakeOpen } from "./streamer/streamer.js";
import { splitFailedText, vocalSplitUnavailable, onVocalSplitAvailability } from "./vocalSplitState.js";
import { activeVersionId, nextVersionNumber, songVersions } from "./versionScope.js";
import {
  createVersion, deleteVersion, openVersion, renameVersion, versionLabel, versionStemsText,
} from "./versions.js";

let btn = null;
let pop = null;
let ui = null;
// The choice being edited, applied with Apply: { lanes: Set, voice }.
let draft = null;
let busy = false;
// Delete asks twice: the first press arms it for a few seconds.
let deleteArmed = 0;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function modeName(mode) {
  return t(`mode.${mode}`);
}

function blockedText(reason) {
  const key = stemsReasonKey(reason);
  return key ? t(key) : "";
}

function build() {
  pop = el("div", "stems-pop");
  pop.id = "laneStemsPop";
  pop.setAttribute("role", "dialog");
  pop.hidden = true;
  const title = el("p", "stems-pop-title");
  title.id = "laneStemsTitle";
  pop.setAttribute("aria-labelledby", title.id);
  const hint = el("p", "stems-pop-hint");
  const list = el("div", "stems-pop-list");
  const rows = new Map();
  for (const name of BASE_STEMS) {
    const row = el("label", "stems-pop-row");
    row.style.setProperty("--stem-c", STEM_COLORS[name] || "var(--muted)");
    const box = el("input");
    box.type = "checkbox";
    box.dataset.stem = name;
    box.addEventListener("change", () => {
      if (!draft) return;
      if (box.checked) draft.lanes.add(name);
      else draft.lanes.delete(name);
      // A message about the last Apply no longer describes the ticks.
      setStatus("");
      paint();
    });
    const dot = el("span", "stems-pop-dot");
    const label = el("span", "stems-pop-name");
    const note = el("span", "stems-pop-missing");
    row.append(box, dot, label, note);
    list.append(row);
    rows.set(name, { row, box, label, note });
  }
  const voice = el("div", "stems-pop-voice");
  voice.setAttribute("role", "radiogroup");
  const voiceLabel = el("span", "stems-pop-voice-label");
  voiceLabel.id = "laneStemsVoiceLabel";
  voice.setAttribute("aria-labelledby", voiceLabel.id);
  const seg = el("div", "stems-pop-seg");
  const voiceBtns = {};
  for (const form of ["combined", "split"]) {
    const b = el("button", "stems-pop-seg-btn");
    b.type = "button";
    b.dataset.form = form;
    b.setAttribute("role", "radio");
    b.addEventListener("click", () => {
      if (!draft) return;
      draft.voice = form;
      paint();
    });
    seg.append(b);
    voiceBtns[form] = b;
  }
  const splitBtn = el("button", "stems-pop-split");
  splitBtn.type = "button";
  splitBtn.addEventListener("click", () => runSplit());
  const splitNote = el("p", "stems-pop-split-note");
  voice.append(voiceLabel, seg, splitBtn, splitNote);
  const holds = el("p", "stems-pop-holds");
  const status = el("p", "stems-pop-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const actions = el("div", "stems-pop-actions");
  const reset = el("button", "stems-pop-reset");
  reset.type = "button";
  reset.addEventListener("click", () => apply({ lanes: null, voice: null }));
  const applyBtn = el("button", "stems-pop-apply");
  applyBtn.type = "button";
  applyBtn.addEventListener("click", () => apply());
  actions.append(reset, applyBtn);
  const versions = buildVersions();
  pop.append(title, hint, list, voice, holds, status, actions, versions.wrap);
  pop.addEventListener("keydown", onKey);
  pop.addEventListener("click", (e) => e.stopPropagation());
  document.body.append(pop);
  ui = { title, hint, rows, voice, voiceLabel, voiceBtns, seg, splitBtn, splitNote, holds, status, reset, applyBtn, v: versions };
}

function buildVersions() {
  const wrap = el("div", "stems-pop-versions");
  const label = el("label", "stems-pop-voice-label");
  label.htmlFor = "laneVersionSelect";
  const select = el("select", "stems-pop-vselect");
  select.id = "laneVersionSelect";
  select.addEventListener("change", () => runVersion(() => openVersion(currentJobId, select.value)));
  const nameRow = el("div", "stems-pop-vrow");
  const name = el("input", "stems-pop-vname");
  name.type = "text";
  name.maxLength = 60;
  name.autocomplete = "off";
  name.addEventListener("input", () => paint());
  name.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      rename.click();
    }
  });
  const rename = el("button", "stems-pop-vrename");
  rename.type = "button";
  rename.addEventListener("click", () => {
    const value = name.value.trim();
    if (!value) return;
    runVersion(async () => {
      const why = await renameVersion(currentJobId, activeVersionId(currentJobId), value);
      if (why) throw new Error(why);
    }, t("versions.renamed"));
  });
  nameRow.append(name, rename);
  const row = el("div", "stems-pop-actions");
  const del = el("button", "stems-pop-vdelete");
  del.type = "button";
  del.addEventListener("click", () => {
    if (!deleteArmed) {
      deleteArmed = window.setTimeout(() => { deleteArmed = 0; paint(); }, 4000);
      paint();
      return;
    }
    window.clearTimeout(deleteArmed);
    deleteArmed = 0;
    runVersion(async () => {
      const why = await deleteVersion(currentJobId, activeVersionId(currentJobId));
      if (why) throw new Error(why);
    }, t("versions.deleted"));
  });
  const save = el("button", "stems-pop-vnew");
  save.type = "button";
  save.addEventListener("click", () => saveAsVersion());
  row.append(del, save);
  wrap.append(label, select, nameRow, row);
  return { wrap, label, select, name, rename, del, save };
}

/** Run a version action with the popover held, then say how it went. */
async function runVersion(fn, doneText = "") {
  const why = layoutBlockedReason();
  if (why) {
    setStatus(blockedText(why));
    paint();
    return;
  }
  busy = true;
  paint();
  try {
    await fn();
    setStatus(doneText);
  } catch (err) {
    console.warn("[stemsControl] the version action failed:", err);
    setStatus(t("versions.failed"));
  }
  busy = false;
  draft = null;
  paint();
}

/** "Save as new version": the ticked stems, the voice and the mix as it is
 * now become a new version of the song, which opens. */
function saveAsVersion() {
  const state = layoutState();
  if (!draft) return;
  const lanes = [...draft.lanes].filter((n) => hasStem(state.present, n));
  if (!lanes.length) {
    setStatus(blockedText("empty"));
    return;
  }
  const name = t("versions.unnamed", { n: nextVersionNumber(songVersions(currentJobId)) });
  const voice = lanes.includes("vocals") ? draft.voice : null;
  runVersion(() => createVersion(currentJobId, { name, lanes, voice, copyMix: true }), t("versions.created", { name }));
}

function paintVersions(state) {
  const v = ui?.v;
  if (!v) return;
  const jobId = state.jobId;
  v.wrap.hidden = !jobId;
  if (!jobId) return;
  const list = songVersions(jobId);
  const active = activeVersionId(jobId);
  v.label.textContent = t("versions.title");
  v.select.setAttribute("aria-label", t("versions.title"));
  const options = list.map((ver, i) => {
    const text = ver.stems ? `${versionLabel(ver, i)} (${versionStemsText(ver.stems)})` : versionLabel(ver, i);
    return [ver.id, text];
  });
  const have = [...v.select.options].map((o) => [o.value, o.textContent]);
  if (JSON.stringify(have) !== JSON.stringify(options)) {
    v.select.replaceChildren(...options.map(([value, text]) => {
      const o = el("option", null, text);
      o.value = value;
      return o;
    }));
  }
  v.select.value = active;
  v.select.disabled = busy || list.length < 2;
  const index = Math.max(0, list.findIndex((ver) => ver.id === active));
  const current = versionLabel(list[index], index);
  if (document.activeElement !== v.name) v.name.value = list[index]?.name || "";
  v.name.placeholder = current;
  v.name.setAttribute("aria-label", t("versions.nameLabel"));
  v.name.disabled = busy;
  v.rename.textContent = t("versions.rename");
  v.rename.disabled = busy || !v.name.value.trim() || v.name.value.trim() === (list[index]?.name || "");
  v.save.textContent = t("versions.saveNew");
  v.save.disabled = busy;
  v.del.textContent = deleteArmed ? t("versions.deleteConfirm") : t("versions.delete");
  v.del.classList.toggle("armed", Boolean(deleteArmed));
  v.del.disabled = busy || list.length < 2;
}

/** The song has this stem: its file, or Vocals as the Lead + Backing pair. */
function hasStem(present, name) {
  const have = new Set(present);
  return have.has(name) || (name === "vocals" && have.has("lead_vocals") && have.has("backing_vocals"));
}

function draftFrom(state) {
  return { lanes: new Set(state.lanes), voice: state.voice || (state.shown.split ? "split" : "combined") };
}

function draftChanged(state) {
  if (!draft) return false;
  const voiceNow = state.voice || (state.shown.split ? "split" : "combined");
  return !sameNames(draft.lanes, new Set(state.lanes)) || draft.voice !== voiceNow;
}

/** Paint the button (shown in Practice and Advanced with a song open) and,
 * when open, the popover. */
function paint() {
  if (!btn) return;
  const state = layoutState();
  const show = Boolean(state.jobId) && state.canChooseLanes;
  btn.hidden = !show;
  const title = t("stems.buttonTitle", { mode: modeName(state.mode) });
  btn.title = title;
  btn.setAttribute("aria-label", title);
  btn.classList.toggle("chosen", state.chosen);
  if (!show && pop && !pop.hidden) close();
  if (!pop || pop.hidden || !ui) return;
  if (!draft) draft = draftFrom(state);
  const present = new Set(state.present);
  const splitDone = present.has("lead_vocals") && present.has("backing_vocals");
  ui.title.textContent = t("stems.title", { mode: modeName(state.mode) });
  ui.hint.textContent = t("stems.hint");
  for (const [name, r] of ui.rows) {
    const has = present.has(name) || (name === "vocals" && splitDone);
    r.label.textContent = laneLabel(name);
    r.box.checked = draft.lanes.has(name);
    r.box.disabled = !has || busy;
    r.note.textContent = has ? "" : t("stems.missing");
    r.row.classList.toggle("missing", !has);
  }
  // Voice: only while Vocals is a lane.
  const voiceOn = draft.lanes.has("vocals") && state.canChooseVoice;
  ui.voice.hidden = !voiceOn;
  ui.voiceLabel.textContent = t("stems.voice");
  const running = state.splitState === "running";
  ui.seg.hidden = !splitDone;
  for (const [form, b] of Object.entries(ui.voiceBtns)) {
    b.textContent = t(form === "split" ? "stems.voiceSplit" : "stems.voiceCombined");
    const on = draft.voice === form;
    b.setAttribute("aria-checked", String(on));
    b.tabIndex = on ? 0 : -1;
    b.disabled = busy;
  }
  // Lead + Backing cannot be made on this install (#832): the button says
  // so before it is pressed, rather than after a split that cannot work.
  const unavailable = vocalSplitUnavailable();
  ui.splitBtn.hidden = splitDone;
  ui.splitBtn.disabled = running || busy || Boolean(unavailable);
  ui.splitBtn.textContent = running ? t("stems.splitRunning") : t("stems.splitRun");
  ui.splitNote.hidden = splitDone;
  // The last split of this song failed: why, in place of the usual note,
  // unless the status line below is already saying it.
  const failedBefore = state.splitState === "error" && state.splitReason && !ui.status.textContent;
  ui.splitNote.textContent = unavailable
    ? t("split.unavailable")
    : failedBefore
      ? splitFailedText(state.splitReason)
      : t("stems.splitNote");
  const holds = BASE_STEMS.filter((n) => !draft.lanes.has(n) && present.has(n));
  ui.holds.textContent = holds.length
    ? t("stems.originalHolds", { names: holds.map(laneLabel).join(", ") })
    : t("stems.originalEmpty");
  ui.reset.textContent = t("stems.useSong");
  ui.reset.hidden = !state.chosen;
  ui.reset.disabled = busy;
  ui.applyBtn.textContent = t("stems.apply");
  ui.applyBtn.disabled = busy || !draftChanged(state);
  paintVersions(state);
}

function setStatus(text) {
  if (ui) ui.status.textContent = text || "";
}

async function apply(patch) {
  const before = layoutState();
  // The voice is written only when it was changed: an untouched voice keeps
  // following today's rule (the pair whenever the split exists).
  const voiceNow = before.voice || (before.shown.split ? "split" : "combined");
  const next = patch || { lanes: [...draft.lanes], ...(draft.voice !== voiceNow ? { voice: draft.voice } : {}) };
  // A stem the song does not have stays ticked out of sight (its box is
  // disabled), so an empty list is not the only empty choice: with no stem
  // of this song ticked, say so and save nothing (#793).
  if (next.lanes && !next.lanes.some((n) => hasStem(before.present, n))) {
    setStatus(blockedText("empty"));
    return;
  }
  busy = true;
  paint();
  const why = await changeLayout(next);
  busy = false;
  if (why) {
    setStatus(blockedText(why));
    paint();
    return;
  }
  const after = layoutState();
  draft = draftFrom(after);
  // Doc 4.2: a stem moved into a muted Original is silent; say so, never unmute.
  const moved = after.shown.original.filter((n) => !before.shown.original.includes(n));
  const msg = moved.length && mixerState.original?.muted
    ? t("stems.originalMuted", { names: moved.map(laneLabel).join(", ") })
    : t("stems.applied", { mode: modeName(after.mode) });
  setStatus(msg);
  paint();
}

async function runSplit() {
  setStatus("");
  if (draft) draft.voice = "split";
  paint();
  const why = await requestSplit();
  if (why === "failed") setStatus(splitFailedText(layoutState().splitReason));
  else if (why) setStatus(blockedText(why));
  draft = null;
  paint();
}

function open() {
  if (!pop) build();
  draft = null;
  window.clearTimeout(deleteArmed);
  deleteArmed = 0;
  setStatus("");
  pop.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  paint();
  place();
  pop.querySelector("input:not(:disabled)")?.focus();
}

function place() {
  if (!pop || pop.hidden) return;
  const r = btn.getBoundingClientRect();
  const w = pop.offsetWidth;
  const left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
  // Hung below the button and scrolled inside when it is taller than the
  // room there (the versions part made it tall), so it never covers the
  // button that closes it. Moved up only when there is almost no room below.
  const below = Math.floor(window.innerHeight - r.bottom - 14);
  pop.style.maxHeight = below >= 240 ? `${below}px` : "";
  const top = below >= 240 ? r.bottom + 6 : Math.min(r.bottom + 6, window.innerHeight - pop.offsetHeight - 8);
  pop.style.left = `${Math.round(left)}px`;
  pop.style.top = `${Math.round(Math.max(8, top))}px`;
}

function close({ focusButton = false } = {}) {
  if (!pop || pop.hidden) return;
  pop.hidden = true;
  draft = null;
  btn.setAttribute("aria-expanded", "false");
  if (focusButton) btn.focus();
}

function onKey(e) {
  if (e.key === "Escape") {
    e.preventDefault();
    close({ focusButton: true });
  } else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && e.target?.closest?.(".stems-pop-seg")) {
    e.preventDefault();
    if (draft) draft.voice = draft.voice === "split" ? "combined" : "split";
    paint();
    ui.voiceBtns[draft.voice]?.focus();
  }
  // Keys in the popover are the popover's: Space must not start playback.
  e.stopPropagation();
}

export function initStemsControl() {
  btn = document.getElementById("laneStemsBtn");
  if (!btn) return;
  setLayoutBusyCheck(() => (isRecording() || isStreamerTakeOpen() ? "recording" : ""));
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (pop && !pop.hidden) close();
    else open();
  });
  btn.addEventListener("keydown", (e) => {
    if (e.code === "Space" || e.key === "Enter") e.stopPropagation();
  });
  document.addEventListener("click", () => close());
  window.addEventListener("resize", () => place());
  onLayoutChange(() => paint());
  onModeChange(() => paint());
  onLanguageChange(() => paint());
  onVocalSplitAvailability(() => paint());
  document.addEventListener("stemdeck:karaoke-changed", () => paint());
  document.addEventListener("stemdeck:streamer-changed", () => paint());
  // The layout is read before the song's job id is current: paint again once
  // the song's mix is in.
  document.addEventListener("stemdeck:mix-loaded", () => paint());
  // Another version opened, or the list changed: the ticks start from it.
  document.addEventListener("stemdeck:versions-changed", () => {
    if (!busy) draft = null;
    paint();
  });
  paint();
}

/** For tests and scripts: the current job's state as the control shows it. */
export function stemsControlState() {
  return { open: Boolean(pop && !pop.hidden), job: currentJobId, ...layoutState() };
}
