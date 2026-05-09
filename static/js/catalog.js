// catalog.js — library panel: folders, tracks, collapse, drag-and-drop
import { STEM_NAMES } from "./constants.js";
import { wireUpAudio } from "./player.js";
import { bpmChip, keyChip, saveSelectedStems, selectedStems, titleEl } from "./state.js";

const STORAGE_KEY = "stemdeck.folders";
const STORAGE_VERSION = 2; // bump to wipe stale seeded data

let folders = [];
let tracks = {};
let _currentTrackId = null;
let catalogView = "library";
let catalogSearchQuery = "";

// ─── Persistence ───

const TRASH_ID = "trash";
const PROCESSING_STATUSES = new Set(["queued", "downloading", "analyzing", "separating", "processing"]);
const FOLDER_COLORS = ["#d8a84a", "#e85f6f", "#64c86f", "#4f9de8", "#a985f4"];
const DEFAULT_FOLDER_COLOR = FOLDER_COLORS[0];
const TRACK_DRAG_TYPE = "application/x-stemdeck-track";

function normalizeFolderColor(color) {
  return FOLDER_COLORS.includes(color) ? color : DEFAULT_FOLDER_COLOR;
}

function makeFolder({ id = `f-${Date.now()}`, name = "New folder", collapsed = false, items = [] } = {}) {
  return { id, name, collapsed, items, color: DEFAULT_FOLDER_COLOR };
}

function ensureTrash() {
  if (!folders.find((f) => f.id === TRASH_ID)) {
    folders.push({ id: TRASH_ID, name: "Trash", collapsed: true, items: [] });
  }
}

function getTrashFolder() {
  ensureTrash();
  return folders.find((f) => f.id === TRASH_ID);
}

function removeTrackFromFolders(trackId) {
  for (const folder of folders) {
    folder.items = folder.items.filter((id) => id !== trackId);
  }
}

function normalizeSource(value) {
  return String(value || "").trim();
}

function normalizeSearch(value) {
  return String(value || "").trim().toLowerCase();
}

function trackMatchesSearch(track) {
  const q = normalizeSearch(catalogSearchQuery);
  if (!q) return true;
  return [
    track?.title,
    track?.channel,
    track?.sourceUrl,
    ...(track?.stems || []),
  ].some((value) => String(value || "").toLowerCase().includes(q));
}

function findTrackBySource(sourceUrl, exceptId) {
  const source = normalizeSource(sourceUrl);
  if (!source) return null;
  for (const [id, track] of Object.entries(tracks)) {
    if (id === exceptId) continue;
    if (normalizeSource(track.sourceUrl) === source) return id;
  }
  return null;
}

function replaceTrackId(oldId, newId) {
  if (!oldId || !newId || oldId === newId || !tracks[oldId]) return;
  tracks[newId] = { ...tracks[oldId], ...(tracks[newId] || {}), id: newId };
  delete tracks[oldId];
  for (const folder of folders) {
    folder.items = folder.items.map((id) => (id === oldId ? newId : id));
    folder.items = [...new Set(folder.items)];
  }
  if (_currentTrackId === oldId) _currentTrackId = newId;
}

function purgeTrash() {
  const trash = folders.find((f) => f.id === TRASH_ID);
  if (!trash?.items.length) return false;
  const trashIds = new Set(trash.items);
  for (const id of trashIds) delete tracks[id];
  for (const folder of folders) {
    folder.items = folder.items.filter((id) => !trashIds.has(id));
  }
  trash.items = [];
  return true;
}

function loadState() {
  let changed = false;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const data = JSON.parse(raw);
      if ((data.v ?? 1) >= STORAGE_VERSION) {
        folders = data.folders ?? [];
        tracks = data.tracks ?? {};
        // Drop title-less entries left over from before metadata persistence.
        const noTitle = Object.keys(tracks).filter((id) => !tracks[id].title);
        if (noTitle.length) {
          const toRemove = new Set(noTitle);
          noTitle.forEach((id) => delete tracks[id]);
          folders.forEach((f) => { f.items = f.items.filter((id) => !toRemove.has(id)); });
          changed = true;
        }
      }
      // else: stale version → start fresh
    }
  } catch { /* ignore */ }

  ensureTrash();
  for (const folder of folders) {
    if (folder.id !== TRASH_ID) {
      const nextColor = normalizeFolderColor(folder.color);
      if (folder.color !== nextColor) {
        folder.color = nextColor;
        changed = true;
      }
    }
  }
  changed = purgeTrash() || changed;
  if (changed) saveState();
}

function saveState() {
  ensureTrash();
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ v: STORAGE_VERSION, folders, tracks }));
  } catch { /* ignore */ }
}

// ─── Track management ───

export function addTrackToLibrary(track) {
  // track: { id, title, channel, thumb, stems, status, sourceUrl }
  const existingId = findTrackBySource(track.sourceUrl, track.id);
  if (existingId) replaceTrackId(existingId, track.id);
  tracks[track.id] = { ...(tracks[track.id] || {}), ...track };
  const alreadyPlaced = folders.some((folder) => folder.items.includes(track.id));
  if (!alreadyPlaced) {
    // Put into first non-trash folder or create an "Unsorted" folder.
    let target = folders.find((folder) => folder.id !== TRASH_ID);
    if (!target) {
      target = makeFolder({ id: `f-${Date.now()}`, name: "Unsorted" });
      folders.unshift(target);
    }
    target.items.unshift(track.id);
  }
  saveState();
  render();
}

export function updateTrackStatus(trackId, status) {
  if (tracks[trackId]) {
    tracks[trackId].status = status;
    saveState();
    const statusDot = document.querySelector(`.cat-item[data-id="${trackId}"] .cat-status`);
    if (statusDot) {
      statusDot.className = `cat-status ${PROCESSING_STATUSES.has(status) ? "processing" : ""}`;
    }
  }
}

function hasTrackAnalysis(track) {
  return Boolean(
    track?.bpm
    || track?.key
    || track?.scale
    || track?.keyConfidence != null
    || track?.lufs != null
    || track?.peakDb != null,
  );
}

function stateMetadataToTrack(state, fallbackTrack) {
  return {
    ...fallbackTrack,
    title: state.title || fallbackTrack.title,
    thumb: state.thumbnail || fallbackTrack.thumb,
    stems: state.selected_stems || fallbackTrack.stems,
    selectedStems: state.selected_stems || fallbackTrack.selectedStems,
    audioStems: state.stems || fallbackTrack.audioStems || [],
    duration: state.duration || fallbackTrack.duration,
    status: state.status || fallbackTrack.status,
    bpm: state.bpm ?? fallbackTrack.bpm,
    key: state.key ?? fallbackTrack.key,
    scale: state.scale ?? fallbackTrack.scale,
    keyConfidence: state.key_confidence ?? fallbackTrack.keyConfidence,
    lufs: state.lufs ?? fallbackTrack.lufs,
    peakDb: state.peak_db ?? fallbackTrack.peakDb,
  };
}

function applyTrackInfoToPanel(track) {
  titleEl.textContent = track.title || "Untitled track";
  bpmChip.textContent = track.bpm ? `${track.bpm} BPM` : "— BPM";
  keyChip.textContent = track.key || "— —";

  const summaryKey = document.getElementById("summary-key");
  const summaryBpm = document.getElementById("summary-bpm");
  const summaryScale = document.getElementById("summary-scale");
  const summaryConfidence = document.getElementById("summary-confidence");
  const summaryConfidenceLabel = document.getElementById("summary-confidence-label");
  const loudnessCard = document.getElementById("loudness-card");
  const summaryLufs = document.getElementById("summary-lufs");
  const summaryPeak = document.getElementById("summary-peak");

  if (summaryKey) summaryKey.textContent = track.key || "—";
  if (summaryBpm) {
    summaryBpm.textContent = "";
    if (track.bpm) {
      const bpmNum = document.createTextNode(`${track.bpm} `);
      const bpmUnit = document.createElement("small");
      bpmUnit.textContent = "BPM";
      summaryBpm.append(bpmNum, bpmUnit);
    } else {
      summaryBpm.innerHTML = "— <small>BPM</small>";
    }
  }
  if (summaryScale) summaryScale.textContent = track.scale || "";
  if (summaryConfidence) {
    summaryConfidence.textContent = "";
    summaryConfidence.style.removeProperty("--confidence-pct");
    summaryConfidence.classList.add("hidden");
    summaryConfidenceLabel?.classList.add("hidden");
    if (track.keyConfidence != null) {
      const confidence = Math.max(0, Math.min(100, Number(track.keyConfidence)));
      const confSpan = document.createElement("span");
      confSpan.textContent = `${confidence}%`;
      summaryConfidence.appendChild(confSpan);
      summaryConfidence.style.setProperty("--confidence-pct", confidence);
      summaryConfidence.classList.remove("hidden");
      summaryConfidenceLabel?.classList.remove("hidden");
    }
  }
  if (loudnessCard) {
    const hasLoudness = track.lufs != null && track.peakDb != null;
    loudnessCard.classList.toggle("hidden", !hasLoudness);
    if (hasLoudness) {
      if (summaryLufs) summaryLufs.textContent = Number(track.lufs).toFixed(1);
      if (summaryPeak) summaryPeak.textContent = Number(track.peakDb).toFixed(1);
    }
  }
}

function moveTrackToTrash(trackId) {
  if (!tracks[trackId]) return;
  removeTrackFromFolders(trackId);
  const trash = getTrashFolder();
  if (trash && !trash.items.includes(trackId)) trash.items.unshift(trackId);
  if (_currentTrackId === trackId) _currentTrackId = null;
  saveState();
  render();
}

function setCatalogView(view) {
  catalogView = view === "trash" ? "trash" : "library";
  const app = document.querySelector(".app");
  if (catalogView === "trash") {
    app?.classList.remove("cat-collapsed");
    localStorage.setItem("stemdeck.catalog.collapsed", "0");
  }
  render();
}

function applyStoredStemSelection(track) {
  const stored = track.selectedStems || track.stems || [];
  const next = stored.filter((name) => STEM_NAMES.includes(name));
  if (!next.length) return;
  selectedStems.clear();
  for (const name of next) selectedStems.add(name);
  saveSelectedStems();
  for (const btn of document.querySelectorAll(".stem-choice[data-stem]")) {
    btn.setAttribute("aria-pressed", String(selectedStems.has(btn.dataset.stem)));
  }
}

async function loadTrackIntoStudio(trackId) {
  let track = tracks[trackId];
  if (!track) return;
  const hadStoredAudio = Boolean(track.audioStems?.length);

  if (!track.audioStems?.length || !hasTrackAnalysis(track)) {
    try {
      const res = await fetch(`/api/jobs/${trackId}`);
      if (res.ok) {
        const state = await res.json();
        track = stateMetadataToTrack(state, track);
        tracks[trackId] = track;
        saveState();
      }
    } catch { /* ignore; the stored track may be from a previous server run */ }
  }

  if (!track.audioStems?.length) return;
  if (track.status !== "done" && !hadStoredAudio) return;
  applyStoredStemSelection(track);
  setCurrentTrack(trackId);

  const urlInput = document.getElementById("url");
  if (urlInput && track.sourceUrl) urlInput.value = track.sourceUrl;

  applyTrackInfoToPanel(track);
  wireUpAudio(trackId, track.audioStems, track.duration || 0, track.thumb);
}

export function setCurrentTrack(trackId) {
  _currentTrackId = trackId;
  for (const el of document.querySelectorAll(".cat-item.active")) el.classList.remove("active");
  for (const el of document.querySelectorAll(`.cat-item[data-id="${trackId}"]`)) el.classList.add("active");
  for (const el of document.querySelectorAll(".strip-thumb.active")) el.classList.remove("active");
  for (const el of document.querySelectorAll(`.strip-thumb[data-id="${trackId}"]`)) el.classList.add("active");
}

// ─── Folder operations ───

function createFolder() {
  const folder = makeFolder();
  folders.push(folder);
  saveState();
  render();
  openFolderEditor(folder.id);
}

function deleteFolder(folderId) {
  const idx = folders.findIndex((f) => f.id === folderId);
  if (idx === -1 || folders[idx].id === TRASH_ID) return;
  const [folder] = folders.splice(idx, 1);
  const trash = getTrashFolder();
  for (const trackId of folder.items) {
    if (tracks[trackId] && trash && !trash.items.includes(trackId)) {
      trash.items.unshift(trackId);
    }
  }
  saveState();
  render();
}

let folderEditor = null;

function folderColorButtonsHtml(activeColor) {
  return FOLDER_COLORS.map((color, index) => `
    <button
      class="folder-color-dot${color === activeColor ? " active" : ""}"
      type="button"
      data-color="${color}"
      style="--folder-color: ${color};"
      aria-label="Set folder color ${index + 1}"
      aria-pressed="${color === activeColor}"
    ></button>
  `).join("");
}

function closeFolderEditor() {
  folderEditor?.remove();
  folderEditor = null;
}

function openFolderEditor(folderId) {
  const folder = folders.find((f) => f.id === folderId);
  if (!folder || folder.id === TRASH_ID) return;
  closeFolderEditor();

  let selectedColor = normalizeFolderColor(folder.color);
  const overlay = document.createElement("div");
  overlay.className = "folder-editor-backdrop";
  overlay.innerHTML = `
    <form class="folder-editor" role="dialog" aria-modal="true" aria-label="Edit folder">
      <div class="folder-editor-head">
        <span>Edit folder</span>
        <button class="folder-editor-close" type="button" aria-label="Close">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M18 6 6 18M6 6l12 12"></path>
          </svg>
        </button>
      </div>
      <label class="folder-editor-field">
        <span>Name</span>
        <input class="folder-editor-name" type="text" maxlength="48" autocomplete="off" spellcheck="false" />
      </label>
      <div class="folder-editor-field">
        <span>Color</span>
        <div class="folder-editor-colors" role="group" aria-label="Folder color">
          ${folderColorButtonsHtml(selectedColor)}
        </div>
      </div>
      <div class="folder-editor-actions">
        <button class="folder-editor-cancel" type="button">Cancel</button>
        <button class="folder-editor-save" type="submit">Save</button>
      </div>
    </form>
  `;

  const form = overlay.querySelector(".folder-editor");
  const input = overlay.querySelector(".folder-editor-name");
  input.value = folder.name;

  const refreshDots = () => {
    for (const dot of overlay.querySelectorAll(".folder-color-dot")) {
      const active = dot.dataset.color === selectedColor;
      dot.classList.toggle("active", active);
      dot.setAttribute("aria-pressed", String(active));
    }
  };

  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay) closeFolderEditor();
  });
  overlay.querySelector(".folder-editor-close")?.addEventListener("click", closeFolderEditor);
  overlay.querySelector(".folder-editor-cancel")?.addEventListener("click", closeFolderEditor);
  for (const dot of overlay.querySelectorAll(".folder-color-dot")) {
    dot.addEventListener("click", () => {
      selectedColor = normalizeFolderColor(dot.dataset.color);
      refreshDots();
    });
  }
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    folder.name = input.value.trim() || folder.name;
    folder.color = selectedColor;
    saveState();
    closeFolderEditor();
    render();
  });
  overlay.addEventListener("keydown", (e) => {
    if (e.code === "Escape") closeFolderEditor();
  });

  document.body.appendChild(overlay);
  folderEditor = overlay;
  input.focus();
  input.select();
}

// ─── Drag-and-drop ───

let dragId = null;

function isTrackDragEvent(event) {
  return dragId != null || Boolean(event?.dataTransfer?.types?.includes(TRACK_DRAG_TYPE));
}

function getDraggedTrackId(event) {
  return event?.dataTransfer?.getData(TRACK_DRAG_TYPE) || dragId;
}

function startDrag(trackId, itemEl, event) {
  dragId = trackId;
  if (event?.dataTransfer) {
    event.dataTransfer.effectAllowed = "copyMove";
    event.dataTransfer.setData(TRACK_DRAG_TYPE, trackId);
    event.dataTransfer.setData("text/plain", trackId);
  }
  itemEl.classList.add("dragging");
}

function endDrag(itemEl) {
  dragId = null;
  itemEl.classList.remove("dragging");
  for (const el of document.querySelectorAll(".folder.drop-target")) el.classList.remove("drop-target");
  document.querySelector(".rail-trash")?.classList.remove("drop-target");
  document.getElementById("lanes")?.classList.remove("library-drop-target");
}

function dropOnFolder(folderId) {
  if (!dragId) return;
  // Remove from current folder
  for (const f of folders) {
    const idx = f.items.indexOf(dragId);
    if (idx !== -1) { f.items.splice(idx, 1); break; }
  }
  // Add to target folder
  const target = folders.find((f) => f.id === folderId);
  if (target && !target.items.includes(dragId)) target.items.push(dragId);
  saveState();
  render();
}

function wireTrackDragAndLoad(el, trackId) {
  el.draggable = true;
  el.addEventListener("dragstart", (e) => {
    startDrag(trackId, el, e);
  });
  el.addEventListener("dragend", () => endDrag(el));
  el.addEventListener("click", (e) => {
    if (e.target.closest(".cat-del")) return;
    setCurrentTrack(trackId);
  });
  el.addEventListener("dblclick", (e) => {
    if (e.target.closest(".cat-del")) return;
    loadTrackIntoStudio(trackId);
  });
}

function wireMainPanelDrop() {
  const lanes = document.getElementById("lanes");
  if (!lanes || lanes.dataset.libraryDropReady === "1") return;
  lanes.dataset.libraryDropReady = "1";

  lanes.addEventListener("dragover", (e) => {
    if (!isTrackDragEvent(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    lanes.classList.add("library-drop-target");
  });
  lanes.addEventListener("dragleave", (e) => {
    if (!lanes.contains(e.relatedTarget)) lanes.classList.remove("library-drop-target");
  });
  lanes.addEventListener("drop", (e) => {
    const trackId = getDraggedTrackId(e);
    if (!trackId || !tracks[trackId]) return;
    e.preventDefault();
    lanes.classList.remove("library-drop-target");
    loadTrackIntoStudio(trackId);
  });
}

function wireRailTrashDrop() {
  const trash = document.querySelector(".rail-trash");
  if (!trash || trash.dataset.dropReady === "1") return;
  trash.dataset.dropReady = "1";

  trash.addEventListener("dragover", (e) => {
    if (!isTrackDragEvent(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    trash.classList.add("drop-target");
  });
  trash.addEventListener("dragleave", (e) => {
    if (!trash.contains(e.relatedTarget)) trash.classList.remove("drop-target");
  });
  trash.addEventListener("drop", (e) => {
    const trackId = getDraggedTrackId(e);
    if (!trackId || !tracks[trackId]) return;
    e.preventDefault();
    trash.classList.remove("drop-target");
    moveTrackToTrash(trackId);
  });
}

function isTextEditingTarget(target) {
  return Boolean(target?.closest?.("input, textarea, select, [contenteditable='true'], .folder-editor"));
}

function wireLibraryDeleteKeys() {
  if (document.body.dataset.libraryDeleteReady === "1") return;
  document.body.dataset.libraryDeleteReady = "1";

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Delete" && e.key !== "Backspace") return;
    if (isTextEditingTarget(e.target)) return;
    if (!_currentTrackId || !tracks[_currentTrackId]) return;
    e.preventDefault();
    moveTrackToTrash(_currentTrackId);
  });
}

// ─── Rendering ───

function thumbHtml(track) {
  if (track.thumb) return `<img src="${track.thumb}" alt="" loading="lazy" />`;
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>`;
}

function folderThumbHtml(isTrash = false) {
  if (isTrash) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>';
  }
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>';
}

function makeStripItem({ className = "", id, title, html, color, trackId }) {
  const item = document.createElement("div");
  item.className = className ? `strip-thumb ${className}` : "strip-thumb";
  item.dataset.id = id;
  item.title = title;
  item.innerHTML = html;
  if (color) item.style.setProperty("--folder-color", color);
  if (trackId) wireTrackDragAndLoad(item, trackId);
  return item;
}

function renderTrackItem(trackId, { inTrash = false } = {}) {
  const track = tracks[trackId];
  if (!track) return null;

  const el = document.createElement("div");
  el.className = `cat-item${trackId === _currentTrackId ? " active" : ""}`;
  el.dataset.id = trackId;

  const stemCount = track.stems?.length ?? 0;
  el.innerHTML = `
    <div class="cat-thumb">${thumbHtml(track)}</div>
    <div class="cat-meta">
      <div class="cat-title">${track.title ?? "Unknown track"}</div>
      <div class="cat-sub">
        <span>${track.channel ?? ""}</span>
        <span class="dot">·</span>
        <span>${inTrash ? "Removed" : `${stemCount} stem${stemCount !== 1 ? "s" : ""}`}</span>
      </div>
    </div>
    <div class="cat-status${PROCESSING_STATUSES.has(track.status) ? " processing" : ""}"></div>
    ${inTrash ? "" : `<button class="cat-del" type="button" aria-label="Move ${track.title ?? "track"} to Trash" title="Move to Trash">
      <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <polyline points="3 6 5 6 21 6"></polyline>
        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path>
      </svg>
    </button>`}
  `;

  el.querySelector(".cat-del")?.addEventListener("click", (e) => {
    e.stopPropagation();
    moveTrackToTrash(trackId);
  });

  wireTrackDragAndLoad(el, trackId);

  return el;
}

function renderFolder(folder) {
  const isTrash = folder.id === TRASH_ID;
  if (!isTrash) folder.color = normalizeFolderColor(folder.color);

  const el = document.createElement("div");
  el.className = `folder${folder.collapsed ? " collapsed" : ""}`;
  el.dataset.id = folder.id;

  const head = document.createElement("div");
  head.className = "folder-head";
  if (!isTrash) head.style.setProperty("--folder-color", folder.color);
  const folderIcon = isTrash
    ? `<svg class="f-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>`
    : `<svg class="f-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>`;
  head.innerHTML = `
    <svg class="f-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"></polyline></svg>
    ${folderIcon}
    <span class="f-name">${folder.name}</span>
    <span class="f-count">${folder.items.length}</span>
    ${isTrash ? "" : `<button class="f-del" type="button" aria-label="Delete folder" title="Delete folder">
      <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>
    </button>`}
  `;

  const body = document.createElement("div");
  body.className = "folder-body";

  const visibleItems = folder.items.filter((id) => trackMatchesSearch(tracks[id]));

  if (catalogSearchQuery && visibleItems.length === 0) {
    return null;
  }

  if (visibleItems.length === 0) {
    body.innerHTML = '<span class="folder-empty">Empty folder</span>';
  } else {
    for (const id of visibleItems) {
      const item = renderTrackItem(id);
      if (item) body.appendChild(item);
    }
  }

  el.append(head, body);

  let folderClickTimer = null;

  // Toggle folder collapse on a single click. Double-click is reserved for edit.
  head.addEventListener("click", (e) => {
    if (e.target.closest(".f-del")) return;
    if (e.detail !== 1) return;
    window.clearTimeout(folderClickTimer);
    folderClickTimer = window.setTimeout(() => {
      folder.collapsed = !folder.collapsed;
      el.classList.toggle("collapsed", folder.collapsed);
      saveState();
    }, 180);
  });

  // Edit folder name + color on double-click (not for trash).
  if (!isTrash) {
    head.addEventListener("dblclick", (e) => {
      if (e.target.closest(".f-del")) return;
      window.clearTimeout(folderClickTimer);
      e.stopPropagation();
      openFolderEditor(folder.id);
    });
  }

  // Delete
  head.querySelector(".f-del")?.addEventListener("click", (e) => {
    e.stopPropagation();
    deleteFolder(folder.id);
  });

  // Drag-over for drop target
  el.addEventListener("dragover", (e) => {
    if (!isTrackDragEvent(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    el.classList.add("drop-target");
  });
  el.addEventListener("dragleave", (e) => {
    if (!el.contains(e.relatedTarget)) el.classList.remove("drop-target");
  });
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    el.classList.remove("drop-target");
    dropOnFolder(folder.id);
  });

  return el;
}

function render() {
  const list = document.getElementById("catalogList");
  const strip = document.getElementById("catalogStrip");
  const count = document.getElementById("catCount");
  const catalog = document.getElementById("catalogPanel");
  const searchInput = document.getElementById("catalogSearch");
  if (!list) return;

  list.innerHTML = "";
  if (strip) strip.innerHTML = "";

  const trash = getTrashFolder();
  const trashIds = new Set(trash?.items || []);
  const totalTracks = Object.keys(tracks).filter((id) => !trashIds.has(id)).length;
  const isTrashView = catalogView === "trash";

  catalog?.classList.toggle("trash-view", isTrashView);
  document.querySelector(".rail-library")?.classList.toggle("active", !isTrashView);
  document.querySelector(".rail-library")?.setAttribute("aria-pressed", String(!isTrashView));
  document.querySelector(".rail-trash")?.classList.toggle("active", isTrashView);
  document.querySelector(".rail-trash")?.setAttribute("aria-pressed", String(isTrashView));
  if (count) {
    const n = isTrashView ? trashIds.size : totalTracks;
    count.textContent = isTrashView
      ? `${n} deleted`
      : `${n} track${n !== 1 ? "s" : ""}`;
  }
  if (searchInput) {
    searchInput.placeholder = isTrashView
      ? "Search trash…"
      : "Search library…";
  }

  const nonTrash = folders.filter((f) => f.id !== TRASH_ID);
  if (isTrashView) {
    const visibleTrashItems = (trash?.items || []).filter((id) => trackMatchesSearch(tracks[id]));
    if (!trash?.items.length) {
      list.innerHTML = '<span class="folder-empty trash-empty">Trash is empty</span>';
    } else if (visibleTrashItems.length === 0) {
      list.innerHTML = '<span class="folder-empty trash-empty">No deleted tracks match your search</span>';
    } else {
      for (const id of visibleTrashItems) {
        const item = renderTrackItem(id, { inTrash: true });
        if (item) list.appendChild(item);
      }
    }
    return;
  }

  let renderedFolders = 0;
  for (const folder of nonTrash) {
    const el = renderFolder(folder);
    if (!el) continue;
    list.appendChild(el);
    renderedFolders += 1;
  }
  if (catalogSearchQuery && renderedFolders === 0) {
    list.innerHTML = '<span class="folder-empty trash-empty">No tracks match your search</span>';
  }

  // Collapsed strip: top-level structure only. Show unfiled songs,
  // folders only. Trash lives in the side rail.
  if (strip) {
    const folderTrackIds = new Set(folders.flatMap((folder) => folder.items));
    for (const [trackId, track] of Object.entries(tracks)) {
      if (folderTrackIds.has(trackId)) continue;
      strip.appendChild(makeStripItem({
        className: trackId === _currentTrackId ? "active" : "",
        id: trackId,
        title: track.title,
        html: thumbHtml(track),
        trackId,
      }));
    }
    for (const folder of nonTrash) {
      const folderColor = normalizeFolderColor(folder.color);
      strip.appendChild(makeStripItem({
        className: "folder-thumb",
        id: folder.id,
        title: `${folder.name} (${folder.items.length})`,
        html: folderThumbHtml(false),
        color: folderColor,
      }));
    }
  }
}

// ─── Catalog panel collapse ───

function wireCatalogToggle() {
  const toggle = document.getElementById("catalogToggle");
  if (!toggle) return;
  const app = document.querySelector(".app");
  if (!app) return;

  const collapsed = localStorage.getItem("stemdeck.catalog.collapsed") === "1";
  if (collapsed) app.classList.add("cat-collapsed");

  toggle.addEventListener("click", () => {
    const isNowCollapsed = app.classList.toggle("cat-collapsed");
    localStorage.setItem("stemdeck.catalog.collapsed", isNowCollapsed ? "1" : "0");
  });
  toggle.addEventListener("keydown", (e) => {
    if (e.code === "Enter" || e.code === "Space") { e.preventDefault(); toggle.click(); }
  });
}

function wireCatalogRailViews() {
  document.querySelector(".rail-library")?.addEventListener("click", () => setCatalogView("library"));
  document.querySelector(".rail-trash")?.addEventListener("click", () => setCatalogView("trash"));
}

function wireCatalogSearch() {
  const input = document.getElementById("catalogSearch");
  if (!input || input.dataset.searchReady === "1") return;
  input.dataset.searchReady = "1";
  input.addEventListener("input", () => {
    catalogSearchQuery = normalizeSearch(input.value);
    render();
  });
}

// ─── Collapsible widgets ───

function wireWidgets() {
  for (const head of document.querySelectorAll(".widget-head")) {
    const widget = head.closest(".widget");
    if (!widget) continue;
    const key = `stemdeck.widget.${widget.dataset.widget}`;
    if (localStorage.getItem(key) === "collapsed") {
      widget.classList.add("collapsed");
      head.setAttribute("aria-expanded", "false");
    }
    head.addEventListener("click", () => {
      const isCollapsed = widget.classList.toggle("collapsed");
      head.setAttribute("aria-expanded", String(!isCollapsed));
      localStorage.setItem(key, isCollapsed ? "collapsed" : "open");
    });
    head.addEventListener("keydown", (e) => {
      if (e.code === "Enter" || e.code === "Space") { e.preventDefault(); head.click(); }
    });
  }
}

// ─── Init ───

const FALLBACK_VERSION = "0.1.0";
let currentVersion = FALLBACK_VERSION;
const REPO_URL = "https://github.com/thcp/stemdeck";
const RELEASES_URL = "https://github.com/thcp/stemdeck/releases";
const RELEASES_API = "https://api.github.com/repos/thcp/stemdeck/releases/latest";

function normalizeVersion(value) {
  return String(value || "").trim().replace(/^v/i, "") || FALLBACK_VERSION;
}

function setDisplayedVersion(version) {
  const brand = document.getElementById("brandVersion");
  const about = document.getElementById("aboutVersion");
  currentVersion = normalizeVersion(version);
  if (brand) brand.textContent = `v${currentVersion}`;
  if (about) about.textContent = `v${currentVersion}`;
}

async function loadCurrentVersion() {
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    setDisplayedVersion(data.version);
  } catch { /* backend unavailable during bootstrap -- keep fallback */ }
}

async function checkForUpdate() {
  const el = document.getElementById("brandVersion");
  if (!el) return;
  el.classList.remove("has-update");
  try {
    const res = await fetch(RELEASES_API, { headers: { Accept: "application/vnd.github+json" } });
    if (!res.ok) return;
    const data = await res.json();
    const latest = normalizeVersion(data.tag_name);
    if (!latest || latest === currentVersion) return;
    el.classList.add("has-update");
    el.innerHTML = `<a href="${RELEASES_URL}/tag/${data.tag_name}" target="_blank" rel="noopener noreferrer">new release available</a>`;
  } catch { /* network unavailable — silently skip */ }
}

function wireAboutDialog() {
  const btn = document.getElementById("aboutBtn");
  const dialog = document.getElementById("aboutDialog");
  const close = document.getElementById("aboutClose");
  const version = document.getElementById("aboutVersion");
  const link = dialog?.querySelector(".about-link");
  if (!btn || !dialog) return;

  if (version) version.textContent = `v${currentVersion}`;
  if (link) link.setAttribute("href", REPO_URL);

  const open = () => dialog.classList.remove("hidden");
  const hide = () => dialog.classList.add("hidden");

  btn.addEventListener("click", open);
  close?.addEventListener("click", hide);
  dialog.addEventListener("mousedown", (e) => {
    if (e.target === dialog) hide();
  });
  dialog.addEventListener("keydown", (e) => {
    if (e.code === "Escape") hide();
  });
}

async function syncWithServer() {
  try {
    const res = await fetch("/api/jobs", { cache: "no-store" });
    if (!res.ok) return;
    const jobs = await res.json();
    for (const state of jobs) {
      if (tracks[state.job_id]) continue;
      const track = stateMetadataToTrack(state, { id: state.job_id, status: state.status });
      track.id = state.job_id;
      addTrackToLibrary(track);
    }
  } catch { /* backend unavailable — skip silently */ }
}

export function initCatalog() {
  loadState();
  wireCatalogToggle();
  wireCatalogRailViews();
  wireCatalogSearch();
  wireWidgets();
  wireMainPanelDrop();
  wireRailTrashDrop();
  wireLibraryDeleteKeys();
  wireAboutDialog();
  setDisplayedVersion(currentVersion);
  render();

  document.getElementById("newFolderBtn")?.addEventListener("click", createFolder);
  loadCurrentVersion().finally(checkForUpdate);
  syncWithServer();
}
