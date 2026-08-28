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
let _sectionsSource = null;
let _container = null;
let _saveTimer = null;
let _saveChain = Promise.resolve();

onLanguageChange(() => _render());

// ─── Public API ───────────────────────────────────────────

export function initSections(trackId, sections, duration, sectionsSource = null) {
  _trackId = trackId;
  _duration = Math.max(1, duration || 0);
  _sections = (sections || []).map((s) => ({ ...s }));
  _sectionsSource = sectionsSource;
  _container = document.getElementById("daw-sections");
  _updateSuggestedBadge();
  if (!_container) return;

  // Wire the static "Add" button in the label area (may already be wired)
  const addBtn = document.getElementById("sectionsAddBtn");
  if (addBtn && !addBtn.dataset.sectionsWired) {
    addBtn.dataset.sectionsWired = "1";
    addBtn.addEventListener("click", () => _addSection());
  }

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
  _sectionsSource = null;
  _duration = 0;
  if (_container) _container.innerHTML = "";
  _container = null;
  _updateSuggestedBadge();
}

// ─── Rendering ────────────────────────────────────────────

function _render() {
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
  el.className = "section-block";
  el.dataset.id = section.id;
  el.style.cssText = `left:${pctStart.toFixed(4)}%;width:${pctWidth.toFixed(4)}%;--sc:${section.color}`;

  el.innerHTML = `
    <div class="section-handle section-handle-l" data-edge="left"></div>
    <span class="section-label">${_esc(sectionDisplayName(section, _sections))}</span>
    <button class="section-del" type="button" aria-label="${t("sections.deleteAria")}" tabindex="-1">×</button>
    <div class="section-handle section-handle-r" data-edge="right"></div>
  `;

  el.querySelector(".section-del").addEventListener("click", (e) => {
    e.stopPropagation();
    _deleteSection(section.id);
  });

  el.querySelector(".section-label").addEventListener("dblclick", (e) => {
    e.stopPropagation();
    _openRename(section.id, el.querySelector(".section-label"));
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

function _updateSuggestedBadge() {
  const badge = document.getElementById("sectionsSuggested");
  if (badge) badge.classList.toggle("hidden", _sectionsSource !== "automatic" || !_sections.length);
}

function _esc(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ─── Drag to move ─────────────────────────────────────────

function _wireDrag(el, section) {
  let active = false;
  let startX = 0;
  let origStart = 0;
  let origEnd = 0;
  let changed = false;

  el.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".section-handle,.section-del")) return;
    active = true;
    startX = e.clientX;
    origStart = section.start;
    origEnd = section.end;
    changed = false;
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
    if (!active) return;
    active = false;
    el.classList.remove("sec-dragging");
    if (changed) _scheduleSave();
  });

  el.addEventListener("pointercancel", () => {
    if (active) {
      section.start = origStart;
      section.end = origEnd;
      _render();
    }
    active = false;
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

function _addSection() {
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
  if (start < 0) return;
  const end = Math.min(start + defW, _duration);
  if (end - start < MIN_SEC) return;

  // Verify no overlap
  if (_sections.some((s) => start < s.end && end > s.start)) return;

  const color = _nextColor();
  const section = { id: _nextId(), name: t("sections.defaultName"), start, end, color };
  _sections.push(section);
  _render();
  _scheduleSave();

  // Open rename immediately
  const el = _container?.querySelector(`[data-id="${section.id}"]`);
  if (el) _openRename(section.id, el.querySelector(".section-label"));
}

function _deleteSection(id) {
  _sections = _sections.filter((s) => s.id !== id);
  _render();
  _scheduleSave();
}

function _openRename(id, labelEl) {
  if (!labelEl) return;
  const section = _sections.find((s) => s.id === id);
  if (!section) return;

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
      _sectionsSource = "manual";
      _updateSuggestedBadge();
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
