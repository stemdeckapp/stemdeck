// sections.js — interactive sections bar above the waveform

import { onLanguageChange, t } from "./i18n.js";

const SECTION_COLORS = [
  "#4a7fff",
  "#2ab8e8",
  "#9a4aff",
  "#ff8a20",
  "#00c8a0",
  "#ff4a90",
  "#e8c840",
  "#00d4d4",
];

const MIN_SEC = 0.5; // minimum section duration in seconds
const DEFAULT_WIDTH_FRAC = 0.12; // default new section = 12% of track
const SECTION_KINDS = new Set([
  "intro", "outro", "break", "bridge", "inst", "solo", "verse", "chorus", "part",
]);

let _trackId = null;
let _duration = 0;
let _sections = [];
let _container = null;
let _saveTimer = null;
let _saveChain = Promise.resolve();

// The loop lives in transport.js, which reaches the DOM at import time through
// state.js. Importing it here would drag a document into every Node test that
// loads this module, so the two functions sections needs are handed in instead.
// Same reason loopRegion.js is its own module: the testable part stays
// reachable without a browser.
let _loopBridge = null;

/// Wire sections to the transport's loop. Called once, from the module that
/// already owns both. Unset, the loop features are inert rather than broken.
export function setSectionsLoopBridge(bridge) {
  _loopBridge = bridge;
}

function _armedLoop() {
  const loop = _loopBridge?.getLoop?.();
  if (!loop || !loop.enabled) return null;
  if (!(loop.end > loop.start)) return null;
  return loop;
}

onLanguageChange(() => _render());

// ─── Public API ───────────────────────────────────────────

export function initSections(trackId, sections, duration) {
  _trackId = trackId;
  _duration = Math.max(1, duration || 0);
  _sections = (sections || []).map((s) => ({ ...s }));
  // Clear lives in the header rather than the ribbon, so it has to be correct
  // even when the ribbon is absent and the render below never runs.
  _refreshClearVisibility();
  // The inner track, not the visible area. Everything here is a percentage of
  // this element and the drag maths measures it, so pointing at the zoomed
  // track is what keeps both the drawing and the gesture honest (#573).
  _container = document.getElementById("daw-sections-track");
  if (!_container) return;

  // Wire the static "Add" button in the label area (may already be wired)
  const addBtn = document.getElementById("sectionsAddBtn");
  if (addBtn && !addBtn.dataset.sectionsWired) {
    addBtn.dataset.sectionsWired = "1";
    addBtn.addEventListener("click", () => _addSection());
  }
  _wireClearButton();

  _render();
}

export function destroySections() {
  // Flush any pending debounced save before clearing state so switching tracks
  // never drops unsaved sections. _save() serializes _sections synchronously
  // (JSON.stringify runs before the first await) so it's safe to clear state
  // after calling it.
  if (_saveTimer !== null) {
    clearTimeout(_saveTimer);
    _saveTimer = null;
    _queueSaveSnapshot();
  }
  _hideSaveIndicator();
  _trackId = null;
  _sections = [];
  _duration = 0;
  if (_container) _container.innerHTML = "";
  _container = null;
  _refreshClearVisibility();
}

// ─── Rendering ────────────────────────────────────────────

function _render() {
  // Before the container guard: the button lives in the header, not the
  // ribbon, so its state must stay correct even when the ribbon is absent.
  _refreshClearVisibility();
  if (!_container) return;
  _container.innerHTML = "";

  const sorted = [..._sections].sort((a, b) => a.start - b.start);

  for (const section of sorted) {
    _container.appendChild(_makeSectionEl(section));
  }
}

function _makeSectionEl(section) {
  const pctStart = (section.start / _duration) * 100;
  const pctWidth = ((section.end - section.start) / _duration) * 100;

  const el = document.createElement("div");
  el.className = section.locked ? "section-block sec-locked" : "section-block";
  el.dataset.id = section.id;
  el.style.cssText = `left:${pctStart.toFixed(4)}%;width:${pctWidth.toFixed(4)}%;--sc:${section.color}`;

  const lockAria = section.locked ? t("sections.unlockAria") : t("sections.lockAria");
  el.innerHTML = `
    <div class="section-handle section-handle-l" data-edge="left"></div>
    <span class="section-label">${_esc(sectionDisplayName(section, _sections))}</span>
    <button class="section-lock" type="button" aria-label="${lockAria}" title="${lockAria}" aria-pressed="${section.locked ? "true" : "false"}" tabindex="-1">${_lockIcon(section.locked)}</button>
    <button class="section-del" type="button" aria-label="${t("sections.deleteAria")}" tabindex="-1">×</button>
    <div class="section-handle section-handle-r" data-edge="right"></div>
  `;

  el.querySelector(".section-lock").addEventListener("click", (e) => {
    e.stopPropagation();
    _toggleLock(section.id);
  });

  el.querySelector(".section-del").addEventListener("click", (e) => {
    e.stopPropagation();
    _deleteSection(section.id);
  });

  // Bound to the block, not to the label inside it. The drag calls
  // setPointerCapture, which retargets the rest of the gesture to the capturing
  // element, so the dblclick that ends a real double-click is delivered to the
  // block and a listener on the child label never runs. Synthetic events do not
  // capture, which is why this looked fine in a console and failed for every
  // actual user.
  el.addEventListener("dblclick", (e) => {
    if (e.target.closest(".section-handle,.section-del,.section-lock")) return;
    e.stopPropagation();
    if (section.locked) return;
    const labelEl = el.querySelector(".section-label");
    if (labelEl) _openRename(section.id, labelEl);
  });

  _wireDrag(el, section);
  for (const h of el.querySelectorAll(".section-handle")) {
    _wireResize(h, el, section);
  }

  return el;
}

export function sectionDisplayName(section, all) {
  const kind = String(section?.kind || "").toLowerCase();
  if (!SECTION_KINDS.has(kind)) return String(section?.name || "");
  const name = t(`sections.kind.${kind}`);
  // The model predicts boundaries and labels with separate heads, so two
  // neighbouring spans can share a kind and still be a real structural change
  // (chorus one and chorus two). Merging them was tried and silently discarded
  // five true boundaries on the reference track, so they are numbered instead:
  // the boundary survives and "Chorus Chorus" stops reading as a bug.
  if (!Array.isArray(all)) return name;
  const ordered = [...all].sort((a2, b2) => a2.start - b2.start);
  const peers = ordered.filter((s) => String(s?.kind || "").toLowerCase() === kind);
  if (peers.length < 2) return name;
  const position = peers.findIndex((s) => s.id === section.id);
  if (position < 0) return name;
  return t("sections.kindNumbered", { kind: name, n: position + 1 });
}

function _esc(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Closed or open padlock, in the same stroked 24x24 shape as the rest of the
// app's icons so it inherits --sc through currentColor. Drawn rather than
// written as an emoji: this ships on three platforms and the emoji padlock is
// a different colour and weight on each of them.
function _lockIcon(locked) {
  const shackle = locked ? "M7 11V7a5 5 0 0 1 10 0v4" : "M7 11V7a5 5 0 0 1 9.9-1";
  return (
    '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor"' +
    ' stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>' +
    `<path d="${shackle}"></path></svg>`
  );
}

// ─── Drag to move ─────────────────────────────────────────

function _wireDrag(el, section) {
  let active = false;
  let startX = 0;
  let origStart = 0;
  let origEnd = 0;
  let changed = false;

  // A press that never moves is a click, and a click loops the section. That is
  // tracked separately from `active` so a locked section can still be looped:
  // lock is about position, and refusing to loop one would be a second rule
  // nobody asked for.
  let pressed = false;

  el.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".section-handle,.section-del,.section-lock")) return;
    pressed = true;
    changed = false;
    // Read the flag here, not at wire time: the block is rebuilt on every
    // render, but a stale closure would still be the kind of bug that only
    // shows after a toggle and before the next redraw.
    if (section.locked) return;
    active = true;
    startX = e.clientX;
    origStart = section.start;
    origEnd = section.end;
    el.setPointerCapture(e.pointerId);
    el.classList.add("sec-dragging");
    e.preventDefault();
  });

  el.addEventListener("pointermove", (e) => {
    if (!active) return;
    const cw = _container.getBoundingClientRect().width;
    if (!cw) return;
    const dt = ((e.clientX - startX) / cw) * _duration;
    const w = section.end - section.start;
    const nextStart = _clampMove(section.id, origStart + dt, w);
    const nextEnd = nextStart + w;
    changed ||= _timesChanged(origStart, origEnd, nextStart, nextEnd);
    section.start = nextStart;
    section.end = nextEnd;
    el.style.left = `${(section.start / _duration) * 100}%`;
  });

  el.addEventListener("pointerup", () => {
    const wasPressed = pressed;
    pressed = false;
    if (active) {
      active = false;
      el.classList.remove("sec-dragging");
      if (changed) {
        _scheduleSave();
        return;
      }
    }
    // Nothing moved, so this was a click: loop over what it covers. MIN_SEC is
    // 0.5 s and MIN_LOOP_SEC is 0.2 s, so any section that exists can be a
    // loop and setLoopRange cannot refuse one.
    if (wasPressed && !changed) _loopBridge?.setLoopRange?.(section.start, section.end);
  });

  el.addEventListener("pointercancel", () => {
    if (active) {
      section.start = origStart;
      section.end = origEnd;
      _render();
    }
    active = false;
    pressed = false;
    el.classList.remove("sec-dragging");
  });
}

// ─── Resize handles ───────────────────────────────────────

function _wireResize(handle, el, section) {
  const edge = handle.dataset.edge;
  let active = false;
  let startX = 0;
  let origTime = 0;
  let origStart = 0;
  let origEnd = 0;
  let changed = false;

  handle.addEventListener("pointerdown", (e) => {
    if (section.locked) return;
    active = true;
    startX = e.clientX;
    origTime = edge === "left" ? section.start : section.end;
    origStart = section.start;
    origEnd = section.end;
    changed = false;
    handle.setPointerCapture(e.pointerId);
    el.classList.add("sec-resizing");
    e.preventDefault();
    e.stopPropagation();
  });

  handle.addEventListener("pointermove", (e) => {
    if (!active) return;
    const cw = _container.getBoundingClientRect().width;
    if (!cw) return;
    const dt = ((e.clientX - startX) / cw) * _duration;
    const desired = origTime + dt;

    if (edge === "left") {
      const lbound = _leftNeighborEnd(section.id);
      const max = section.end - MIN_SEC;
      section.start = Math.max(lbound, Math.min(max, desired));
    } else {
      const rbound = _rightNeighborStart(section.id);
      const min = section.start + MIN_SEC;
      section.end = Math.min(rbound, Math.max(min, desired));
    }

    const ps = (section.start / _duration) * 100;
    const pw = ((section.end - section.start) / _duration) * 100;
    changed ||= _timesChanged(origStart, origEnd, section.start, section.end);
    el.style.left = `${ps}%`;
    el.style.width = `${pw}%`;
  });

  handle.addEventListener("pointerup", () => {
    if (!active) return;
    active = false;
    el.classList.remove("sec-resizing");
    if (changed) _scheduleSave();
  });

  handle.addEventListener("pointercancel", () => {
    if (active) {
      section.start = origStart;
      section.end = origEnd;
      _render();
    }
    active = false;
    el.classList.remove("sec-resizing");
  });
}

// ─── Collision helpers ────────────────────────────────────

function _clampMove(id, desiredStart, width) {
  let start = Math.max(0, Math.min(_duration - width, desiredStart));
  const end = () => start + width;
  const others = _sections.filter((s) => s.id !== id);

  for (const o of others) {
    if (start < o.end && end() > o.start) {
      // Snap to whichever edge is closer to desired
      const snapRight = o.end;
      const snapLeft = o.start - width;
      const dr = Math.abs(desiredStart - snapRight);
      const dl = Math.abs(desiredStart - snapLeft);
      start = dl < dr ? Math.max(0, snapLeft) : Math.min(_duration - width, snapRight);
    }
  }
  return start;
}

function _leftNeighborEnd(id) {
  const s = _sections.find((x) => x.id === id);
  let bound = 0;
  for (const o of _sections) {
    if (o.id === id) continue;
    if (o.end <= s.end) bound = Math.max(bound, o.end);
  }
  return bound;
}

function _rightNeighborStart(id) {
  const s = _sections.find((x) => x.id === id);
  let bound = _duration;
  for (const o of _sections) {
    if (o.id === id) continue;
    if (o.start >= s.start) bound = Math.min(bound, o.start);
  }
  return bound;
}

// ─── CRUD ─────────────────────────────────────────────────

// Where a new section goes.
//
// With a loop armed, Add means "make this selection a section", which is the
// direct route from hearing a part to naming it (#573, and #474 asked for the
// same thing). Without one it falls back to the first gap that fits.
//
// Returns null when a loop is armed but cannot become a section, so the caller
// can say why rather than silently building one somewhere else. That silent
// fallback is the whole complaint: you select a region, press Add, and a
// section appears at the start of the track instead.
function _newSectionBounds() {
  const loop = _armedLoop();
  if (loop && loop.end - loop.start >= MIN_SEC) {
    const start = Math.max(0, loop.start);
    const end = Math.min(_duration, loop.end);
    if (end - start < MIN_SEC) return null;
    // Sections cannot overlap, so a loop drawn across one cannot become a
    // section without moving something the user did not ask to move.
    if (_sections.some((s) => start < s.end && end > s.start)) return null;
    return { start, end };
  }

  const defW = _duration * DEFAULT_WIDTH_FRAC;
  const sorted = [..._sections].sort((a, b) => a.start - b.start);

  // Find first gap ≥ defW
  let start = 0;
  for (const s of sorted) {
    if (s.start - start >= defW) break;
    start = Math.max(start, s.end);
  }

  // Clamp and verify room
  start = Math.min(start, _duration - MIN_SEC);
  if (start < 0) return null;
  const end = Math.min(start + defW, _duration);
  if (end - start < MIN_SEC) return null;

  // Verify no overlap
  if (_sections.some((s) => start < s.end && end > s.start)) return null;
  return { start, end };
}

function _addSection() {
  const bounds = _newSectionBounds();
  if (!bounds) {
    // Only worth explaining when a loop was the thing that failed. A full
    // track with no gap left is visible on its own.
    if (_armedLoop()) _showNotice(t("sections.loopOverlaps"));
    return;
  }
  const { start, end } = bounds;

  const color = _nextColor();
  const section = { id: _nextId(), name: t("sections.defaultName"), start, end, color };
  _sections.push(section);
  _render();
  _scheduleSave();

  // Open rename immediately
  const el = _container?.querySelector(`[data-id="${section.id}"]`);
  if (el) {
    _scrollIntoView(section);
    _openRename(section.id, el.querySelector(".section-label"));
  }
}

/**
 * Bring a section into view by scrolling the timeline, not the ribbon.
 *
 * Add always places the new section in the first gap from t=0, which while
 * zoomed and scrolled is usually off to the left, and the rename input opens on
 * it focused. The ribbon is `overflow: clip` and deliberately cannot scroll, so
 * without this the user would be typing into a field they cannot see.
 *
 * Scrolling the wave is the right lever anyway: the ribbon is positioned from
 * the wave's scrollLeft, so moving the wave moves the ribbon with it and keeps
 * the two in step. Read from the DOM rather than imported, because sections.js
 * deliberately depends on nothing but i18n (see syncRulerScroll in
 * transport.js for what importing across that line breaks).
 */
function _scrollIntoView(section) {
  const wave = document.getElementById("wave-scroll");
  if (!wave || !_duration || wave.scrollWidth <= wave.clientWidth) return;
  const mid = ((section.start + section.end) / 2 / _duration) * wave.scrollWidth;
  const target = mid - wave.clientWidth / 2;
  wave.scrollLeft = Math.max(0, Math.min(target, wave.scrollWidth - wave.clientWidth));
}

// Removing every marker at once cannot be undone, and an automatic set costs
// a whole re-import to regenerate, so the first click only arms the button.
// The app has no modal-confirm idiom, so this is the lightest guard that still
// makes a mis-click harmless.
const CLEAR_ARM_MS = 4000;
let _clearArmTimer = null;

function _disarmClear() {
  clearTimeout(_clearArmTimer);
  _clearArmTimer = null;
  const btn = document.getElementById("sectionsClearBtn");
  if (!btn) return;
  delete btn.dataset.armed;
  const label = btn.querySelector(".sections-clear-label");
  if (label) label.textContent = t("sections.clear");
}

function _wireClearButton() {
  const btn = document.getElementById("sectionsClearBtn");
  if (!btn || btn.dataset.sectionsWired) return;
  btn.dataset.sectionsWired = "1";
  btn.addEventListener("click", () => {
    if (btn.dataset.armed === "1") {
      _disarmClear();
      clearAllSections();
      return;
    }
    btn.dataset.armed = "1";
    const label = btn.querySelector(".sections-clear-label");
    if (label) label.textContent = t("sections.clearConfirm");
    clearTimeout(_clearArmTimer);
    _clearArmTimer = setTimeout(_disarmClear, CLEAR_ARM_MS);
  });
}

function _refreshClearVisibility() {
  const btn = document.getElementById("sectionsClearBtn");
  if (!btn) return;
  btn.classList.toggle("hidden", _sections.length === 0);
  if (_sections.length === 0) _disarmClear();
}

export function clearAllSections() {
  if (!_sections.length) return;
  _sections = [];
  // The set is now the user's own empty one, not a model suggestion, so the
  // experimental badge must go with it.
  _render();
  _scheduleSave();
}

function _deleteSection(id) {
  _sections = _sections.filter((s) => s.id !== id);
  _render();
  _scheduleSave();
}

// Lock covers editing: drag, resize and rename all refuse while it is set, and
// the padlock turns red so a locked section is obvious at rest (#573).
//
// Looping over a locked section still works. That reads the section without
// changing it, and it is the gesture most likely to be wanted on one pinned
// precisely because it matters.
//
// Delete is still allowed: the cross is an explicit press on one named target,
// not something a pointer does on the way to somewhere else.
function _toggleLock(id) {
  const section = _sections.find((s) => s.id === id);
  if (!section) return;
  section.locked = !section.locked;
  _render();
  _scheduleSave();
}

function _openRename(id, labelEl) {
  if (!labelEl) return;
  const section = _sections.find((s) => s.id === id);
  if (!section) return;
  // Also checked here, not only at the gesture. _addSection opens a rename on
  // the block it just made, and a future caller has no reason to know that a
  // locked section must not get an editable field.
  if (section.locked) return;

  const input = document.createElement("input");
  input.className = "section-rename-input";
  input.type = "text";
  const originalName = sectionDisplayName(section, _sections);
  input.value = originalName;
  input.style.setProperty("--sc", section.color);
  labelEl.replaceWith(input);
  input.focus();
  input.select();

  const commit = () => {
    const n = input.value.trim();
    if (n && n !== originalName) {
      section.name = n;
      delete section.kind;
      _render();
      _scheduleSave();
      return;
    }
    _render();
  };
  input.addEventListener("blur", commit, { once: true });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") {
      e.preventDefault();
      input.removeEventListener("blur", commit);
      _render();
    }
  });
}

// ─── Persistence ──────────────────────────────────────────

let _savedTimer = null;

function _showSaving() {
  const el = document.getElementById("sectionsSaveIndicator");
  if (!el) return;
  clearTimeout(_savedTimer);
  el.textContent = t("sections.saving");
  el.className = "sections-save-indicator";
}

function _showSaved() {
  const el = document.getElementById("sectionsSaveIndicator");
  if (!el) return;
  el.textContent = t("sections.saved");
  el.className = "sections-save-indicator saved";
  _savedTimer = setTimeout(() => {
    el.className = "sections-save-indicator hidden";
  }, 1800);
}

// A refusal, shown where the save state already appears. That element is
// aria-live="polite", so this reaches a screen reader without stealing focus,
// and it is beside the button that was just pressed rather than in the import
// panel at the other end of the page.
function _showNotice(message) {
  const el = document.getElementById("sectionsSaveIndicator");
  if (!el) return;
  clearTimeout(_savedTimer);
  el.textContent = message;
  el.className = "sections-save-indicator notice";
  _savedTimer = setTimeout(() => {
    el.className = "sections-save-indicator hidden";
  }, 3200);
}

function _hideSaveIndicator() {
  const el = document.getElementById("sectionsSaveIndicator");
  if (el) el.className = "sections-save-indicator hidden";
  clearTimeout(_savedTimer);
}

function _scheduleSave() {
  clearTimeout(_saveTimer);
  _showSaving();
  _saveTimer = setTimeout(() => {
    _saveTimer = null;
    _queueSaveSnapshot();
  }, 600);
}

export function flushSectionsSave() {
  if (_saveTimer !== null) {
    clearTimeout(_saveTimer);
    _saveTimer = null;
  }
  return _queueSaveSnapshot();
}

function _queueSaveSnapshot() {
  if (!_trackId) return _saveChain;
  const id = _trackId;
  const body = JSON.stringify({ sections: _sections });
  _saveChain = _saveChain.then(() => _sendSave(id, body));
  return _saveChain;
}

async function _sendSave(id, body) {
  try {
    const res = await fetch(`/api/jobs/${id}/sections`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => String(res.status));
      console.warn("[sections] save failed:", res.status, detail);
      if (id === _trackId) _hideSaveIndicator();
      return;
    }
    if (id === _trackId) {
      if (body === JSON.stringify({ sections: _sections }) && _saveTimer === null) _showSaved();
    }
  } catch (e) {
    console.warn("[sections] save failed:", e);
    if (id === _trackId) _hideSaveIndicator();
  }
}

function _timesChanged(beforeStart, beforeEnd, afterStart, afterEnd) {
  return Math.abs(beforeStart - afterStart) > 1e-6 || Math.abs(beforeEnd - afterEnd) > 1e-6;
}

// ─── Utilities ────────────────────────────────────────────

function _nextColor() {
  const used = new Set(_sections.map((s) => s.color));
  return SECTION_COLORS.find((c) => !used.has(c)) ?? SECTION_COLORS[_sections.length % SECTION_COLORS.length];
}

function _nextId() {
  return `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}
