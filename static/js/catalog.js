// catalog.js — library panel: folders, tracks, collapse, drag-and-drop
import { STEM_NAMES } from "./constants.js";
import { wireUpAudio, updateFooterTrack } from "./player.js";
import { initSections } from "./sections.js";
import { bpmChip, keyChip, saveSelectedStems, selectedStems, titleEl } from "./state.js";
import { showError, importFromUrl } from "./job.js";
import { fmtTime, storeGet, storeSet } from "./utils.js";

// Escape user-supplied strings before inserting into innerHTML.
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const STORAGE_KEY = "stemdeck.folders";
const STORAGE_VERSION = 2; // bump to wipe stale seeded data
const DELETED_JOBS_KEY = "stemdeck.deleted_jobs";

// Curated "Our Friends" partners shown at the bottom of the library. Add an
// entry here to feature another store/band/etc. Logos are bundled under
// static/img/friends/ so they render offline. Links open externally via the
// document-level a[target="_blank"] handler in main.js (Tauri open_url).
const FRIENDS = [
  { name: "Dlima Guitars", url: "https://www.instagram.com/dlimaguitars", logo: "/img/friends/dlima-guitars-ig.jpg", avatar: true },
  { name: "Lisbon Guitar Works", url: "https://dlimaguitars.com", logo: "/img/friends/lisbon-guitar-works.webp" },
  {
    name: "Joao Gaspar",
    role: "Producer/Film Scorer, Touring/Session Musician",
    url: "https://www.instagram.com/jay_glaspar",
    logo: "/img/friends/joao-gaspar.jpg",
    avatar: true,
  },
  {
    name: "Kris Luthier",
    role: "Luthier and Musical Instrument Repair, Lisboa",
    url: "https://www.instagram.com/krisluthier",
    logo: "/img/friends/kris-luthier.jpg",
    avatar: true,
  },
  {
    name: "Thomann",
    role: "Online Music Store",
    url: "https://www.instagram.com/thomann.music",
    logo: "/img/friends/thomann.jpg",
    avatar: true,
  },
  {
    name: "Analog4Lyfe",
    role: "Analog music gear",
    url: "https://www.instagram.com/analog4lyfe",
    logo: "/img/friends/analog4lyfe.jpg",
    avatar: true,
  },
  {
    name: "Empress Effects",
    role: "Effects pedals",
    url: "https://empresseffects.com",
    logo: "/img/friends/empress-effects.png",
  },
];

// Instagram glyph (Simple Icons), shown under tiles that link to Instagram.
const IG_ICON_PATH =
  "M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z";

let folders = [];
let tracks = {};
let _deletedJobIds = new Set();
let _currentTrackId = null;
let _loadTrackToken = 0;
let catalogView = "library";
let catalogSearchQuery = "";

// ─── Persistence ───

const TRASH_ID = "trash";
// The default landing folder for unorganized tracks — protected from deletion.
const UNSORTED_ID = "f-unsorted";
const PROCESSING_STATUSES = new Set(["queued", "downloading", "analyzing", "separating", "processing"]);
const FOLDER_COLORS = ["#d8a84a", "#e85f6f", "#64c86f", "#4f9de8", "#a985f4"];
const DEFAULT_FOLDER_COLOR = FOLDER_COLORS[0];
const TRACK_DRAG_TYPE = "application/x-stemdeck-track";
const FOLDER_DRAG_TYPE = "application/x-stemdeck-folder";

function getDeletedJobIds() {
  return _deletedJobIds;
}

function markJobsDeleted(ids) {
  for (const id of ids) _deletedJobIds.add(id);
  storeSet(DELETED_JOBS_KEY, [..._deletedJobIds]).catch((e) =>
    console.warn("[catalog] failed to persist deleted jobs", e)
  );
}

function normalizeFolderColor(color) {
  return FOLDER_COLORS.includes(color) ? color : DEFAULT_FOLDER_COLOR;
}

function makeFolder({ id = `f-${Date.now()}`, name = "New folder", collapsed = false, items = [], parentId = null } = {}) {
  return { id, name, collapsed, items, color: DEFAULT_FOLDER_COLOR, parentId: parentId ?? null };
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
  const s = String(value || "").trim();
  if (!s) return s;
  // Normalize YouTube URLs to the bare video ID so that youtu.be/xxx,
  // youtube.com/watch?v=xxx, and variants with &t= / ?si= all match.
  const yt = s.match(/(?:youtu\.be\/|[?&]v=)([a-zA-Z0-9_-]{11})/);
  if (yt) return `yt:${yt[1]}`;
  return s;
}

function normalizeSearch(value) {
  return String(value || "").trim().toLowerCase();
}

function trackMatchesSearch(track) {
  const q = normalizeSearch(catalogSearchQuery);
  if (!q) return true;
  if (q.startsWith("#")) {
    const tag = q.slice(1);
    if (!tag) return true;
    return (track?.tags ?? []).some((t) => String(t).toLowerCase().includes(tag));
  }
  return [
    track?.title,
    track?.channel,
    track?.sourceUrl,
    ...(track?.stems || []),
    ...(track?.tags || []),
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

async function loadState() {
  let changed = false;
  try {
    const data = await storeGet(STORAGE_KEY, null);
    if (data) {
      if ((data.v ?? 1) >= STORAGE_VERSION) {
        folders = data.folders ?? [];
        tracks = data.tracks ?? {};
        // Migrate old timestamp-based "Unsorted" folder to reserved ID.
        const oldUnsorted = folders.find((f) => f.id !== TRASH_ID && f.name === "Unsorted" && f.id !== "f-unsorted");
        if (oldUnsorted) { oldUnsorted.id = "f-unsorted"; changed = true; }
        // Ensure all folders have parentId field.
        for (const f of folders) {
          if (!Object.prototype.hasOwnProperty.call(f, "parentId")) { f.parentId = null; changed = true; }
        }
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
  } catch (e) { console.warn("[catalog] failed to load state:", e); }

  try {
    const arr = await storeGet(DELETED_JOBS_KEY, []);
    if (Array.isArray(arr)) _deletedJobIds = new Set(arr);
  } catch (e) { console.warn("[catalog] failed to load deleted jobs", e); }

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
  // Remove trash refs whose track data is missing (orphaned), but don't auto-empty.
  const trashFolder = folders.find((f) => f.id === TRASH_ID);
  if (trashFolder) {
    const before = trashFolder.items.length;
    trashFolder.items = trashFolder.items.filter((id) => tracks[id]);
    if (trashFolder.items.length !== before) changed = true;
  }
  if (changed) saveState();
}

function saveState() {
  ensureTrash();
  storeSet(STORAGE_KEY, { v: STORAGE_VERSION, folders, tracks }).catch((e) =>
    console.warn("[catalog] failed to save state:", e)
  );
}

// ─── Track management ───

export function addTrackToLibrary(track) {
  // track: { id, title, channel, thumb, stems, status, sourceUrl }
  const existingId = findTrackBySource(track.sourceUrl, track.id);
  if (existingId) {
    const trash = getTrashFolder();
    const inTrash = trash?.items.includes(existingId);
    if (inTrash) {
      // Old track was trashed — delete it silently so the new import lands
      // in the library instead of inheriting the trash placement.
      delete tracks[existingId];
      for (const f of folders) f.items = f.items.filter((id) => id !== existingId);
    } else {
      replaceTrackId(existingId, track.id);
    }
  }
  const existing = tracks[track.id] || {};
  tracks[track.id] = {
    ...existing,
    ...track,
    createdAt: existing.createdAt ?? track.createdAt ?? (Date.now() / 1000),
    favorite: existing.favorite ?? false,
  };
  const alreadyPlaced = folders.some((folder) => folder.items.includes(track.id));
  if (!alreadyPlaced) {
    // Put into first non-trash folder or create an "Unsorted" folder.
    let target = folders.find((folder) => folder.id !== TRASH_ID);
    if (!target) {
      target = makeFolder({ id: "f-unsorted", name: "Unsorted" });
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
      const modifier = PROCESSING_STATUSES.has(status) ? " processing" : status === "unavailable" ? " unavailable" : "";
      statusDot.className = `cat-status${modifier}`;
    }
    for (const el of document.querySelectorAll(`.cat-item[data-id="${trackId}"]`)) {
      el.classList.toggle("unavailable", status === "unavailable");
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
    stemPresence: state.stem_presence ?? fallbackTrack.stemPresence,
    dynamicRange: state.dynamic_range ?? fallbackTrack.dynamicRange,
    tempoStability: state.tempo_stability ?? fallbackTrack.tempoStability,
    tags: state.tags ?? fallbackTrack.tags ?? [],
    sections: state.sections ?? fallbackTrack.sections ?? null,
    sourceUrl: state.source_url || fallbackTrack.sourceUrl,
    mixUrl: state.mix_url ?? fallbackTrack.mixUrl ?? null,
    hasVideo: state.has_video ?? fallbackTrack.hasVideo ?? false,
    createdAt: fallbackTrack.createdAt ?? state.created_at,
    favorite: fallbackTrack.favorite ?? false,
  };
}

function fmtExtracted(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("en-US", {
    month: "long", day: "numeric", year: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

function deriveSource(sourceUrl) {
  if (!sourceUrl) return "—";
  if (sourceUrl.startsWith("local:")) return "Local file";
  if (sourceUrl.includes("youtube.com") || sourceUrl.includes("youtu.be")) return "YouTube";
  if (sourceUrl.includes("soundcloud.com")) return "SoundCloud";
  return "Web";
}

function deriveQuality(sourceUrl) {
  if (!sourceUrl) return "—";
  if (sourceUrl.startsWith("local:")) {
    const ext = sourceUrl.split(".").pop()?.toLowerCase();
    if (ext === "wav") return "Lossless (WAV)";
    if (ext === "mp3") return "Compressed (MP3)";
    return "Local file";
  }
  if (sourceUrl.includes("youtube.com") || sourceUrl.includes("youtu.be")) return "High";
  if (sourceUrl.includes("soundcloud.com")) return "Compressed (MP3)";
  return "—";
}

function drLabel(dr) {
  if (dr < 7) return "Compressed";
  if (dr < 10) return "Moderate";
  if (dr < 14) return "High";
  return "Wide";
}

function stabilityLabel(pct) {
  if (pct >= 90) return "Very Stable";
  if (pct >= 70) return "Stable";
  if (pct >= 50) return "Moderate";
  return "Variable";
}

export function applyStemPresenceCards(stemPresence) {
  const cards = document.querySelectorAll(".stem-presence-panel .stem-card");
  cards.forEach((card) => {
    const stem = card.dataset.stem;
    const pct = stemPresence?.[stem];
    const label = card.querySelector(".stem-card-pct");
    if (pct != null && pct > 0) {
      card.classList.remove("inactive");
      if (label) label.textContent = `${pct}%`;
    } else {
      card.classList.add("inactive");
      if (label) label.textContent = pct === 0 ? "0%" : "—";
    }
  });
}

function applyTrackInfoToPanel(track) {
  titleEl.textContent = track.title || "Untitled track";
  bpmChip.textContent = track.bpm ? `${track.bpm} BPM` : "— BPM";
  keyChip.textContent = track.key || "— —";
  updateFooterTrack({
    title: track.title,
    thumbnail: track.thumb,
    key: track.key,
    bpm: track.bpm,
    stemCount: (track.audioStems || track.stems || []).filter((s) => (s.name ?? s) !== "original").length || null,
  });
  applyStemPresenceCards(track.stemPresence);

  const summaryKey = document.getElementById("summary-key");
  const summaryBpm = document.getElementById("summary-bpm");
  const summaryScale = document.getElementById("summary-scale");
  const summaryScaleName = document.getElementById("summary-scale-name");
  const summaryConfidence = document.getElementById("summary-confidence");
  const summaryConfidenceLabel = document.getElementById("summary-confidence-label");
  const summaryLufs = document.getElementById("summary-lufs");
  const summaryPeak = document.getElementById("summary-peak");
  const summaryDuration = document.getElementById("summary-duration");

  if (summaryKey) summaryKey.textContent = track.key || "—";
  if (summaryBpm) summaryBpm.textContent = track.bpm ? String(track.bpm) : "—";
  if (summaryScale) summaryScale.textContent = track.scale || "";
  if (summaryScaleName) summaryScaleName.textContent = track.scale || "—";
  if (summaryLufs) summaryLufs.textContent = track.lufs != null ? Number(track.lufs).toFixed(1) : "—";
  if (summaryPeak) summaryPeak.textContent = track.peakDb != null ? `Peak ${Number(track.peakDb).toFixed(1)} dB` : "";
  if (summaryDuration) summaryDuration.textContent = track.duration ? fmtTime(track.duration) : "—";

  const trackExtracted = document.getElementById("track-extracted");
  const trackSource = document.getElementById("track-source");
  const trackQuality = document.getElementById("track-quality");
  const favBtn = document.getElementById("fav-btn");
  if (trackExtracted) trackExtracted.textContent = fmtExtracted(track.createdAt);
  if (trackSource) trackSource.textContent = deriveSource(track.sourceUrl);
  if (trackQuality) trackQuality.textContent = deriveQuality(track.sourceUrl);
  if (favBtn) {
    favBtn.classList.toggle("active", Boolean(track.favorite));
    favBtn.setAttribute("aria-pressed", String(Boolean(track.favorite)));
    favBtn.onclick = () => {
      if (!_currentTrackId) return;
      const t = tracks[_currentTrackId];
      if (!t) return;
      t.favorite = !t.favorite;
      favBtn.classList.toggle("active", t.favorite);
      favBtn.setAttribute("aria-pressed", String(t.favorite));
      saveState();
    };
  }

  const summaryDr = document.getElementById("summary-dr");
  const summaryDrLabel = document.getElementById("summary-dr-label");
  const summaryStability = document.getElementById("summary-stability");
  const summaryStabilityLabel = document.getElementById("summary-stability-label");
  if (summaryDr) summaryDr.textContent = track.dynamicRange != null ? String(track.dynamicRange) : "—";
  if (summaryDrLabel) summaryDrLabel.textContent = track.dynamicRange != null ? drLabel(track.dynamicRange) : "";
  if (summaryStability) {
    summaryStability.textContent = track.tempoStability != null ? `${track.tempoStability}%` : "—";
    summaryStability.className = "meta-card-value" + (track.tempoStability != null && track.tempoStability >= 80 ? " stability-high" : "");
  }
  if (summaryStabilityLabel) summaryStabilityLabel.textContent = track.tempoStability != null ? stabilityLabel(track.tempoStability) : "";

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
  catalogView = ["trash", "favorites"].includes(view) ? view : "library";
  const app = document.querySelector(".app");
  if (catalogView === "trash" || catalogView === "favorites") {
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
  if (track.status === "unavailable") {
    showError("This track's audio is no longer available. Re-upload to restore it.");
    return;
  }
  const hadStoredAudio = Boolean(track.audioStems?.length);
  const token = ++_loadTrackToken;

  // Start peaks fetch immediately — runs in parallel with job-data fetch so it
  // resolves before wireUpAudio calls Multitrack.create. This prevents peaks.json
  // from competing with stem WAV fetches for Safari's 6-connection-per-origin limit.
  const peaksPromise = fetch(`/api/jobs/${trackId}/stems/peaks.json`)
    .then((r) => (r.ok ? r.json() : {}))
    .catch(() => ({}));

  // Always fetch fresh state so server-side changes (sections, analysis, stems)
  // are reflected — cached localStorage data can be stale.
  try {
    const res = await fetch(`/api/jobs/${trackId}`);
    if (token !== _loadTrackToken) return;
    if (res.ok) {
      const state = await res.json();
      track = stateMetadataToTrack(state, track);
      tracks[trackId] = track;
      saveState();
    } else if (res.status === 404) {
      track = { ...track, status: "unavailable" };
      tracks[trackId] = track;
      saveState();
      updateTrackStatus(trackId, "unavailable");
      showError("This track's audio is no longer available. Re-upload to restore it.");
      return;
    }
  } catch (e) { console.warn("[catalog] server sync failed, using stored track:", e); }

  if (token !== _loadTrackToken) return;
  // A reprocessing track may still carry the previous extraction's stems
  // (hadStoredAudio), but it isn't ready — loading it would replace the live
  // job-progress overlay with stale audio. Leave the progress UI in place.
  if (PROCESSING_STATUSES.has(track.status)) return;
  if (!track.audioStems?.length) return;
  if (track.status !== "done" && !hadStoredAudio) return;
  applyStoredStemSelection(track);
  setCurrentTrack(trackId);

  const urlInput = document.getElementById("url");
  if (urlInput && track.sourceUrl) {
    urlInput.value = track.sourceUrl.startsWith("local:")
      ? track.sourceUrl.slice(6)
      : track.sourceUrl;
  }

  applyTrackInfoToPanel(track);
  wireUpAudio(trackId, track.audioStems, track.duration || 0, track.thumb, track.mixUrl ?? null, track.title || "", peaksPromise, track.hasVideo ?? false);
  initSections(trackId, track.sections, track.duration || 0);
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
  if (folderId === TRASH_ID || folderId === UNSORTED_ID) return;
  // Cascade: delete children first.
  for (const child of folders.filter((f) => f.parentId === folderId)) deleteFolder(child.id);
  const idx = folders.findIndex((f) => f.id === folderId);
  if (idx === -1) return;
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

function reorderFolder(draggedId, targetId, before) {
  if (draggedId === targetId) return;
  const dragged = folders.find((f) => f.id === draggedId);
  const target = folders.find((f) => f.id === targetId);
  if (!dragged || !target) return;
  folders.splice(folders.indexOf(dragged), 1);
  const toIdx = folders.indexOf(target);
  folders.splice(before ? toIdx : toIdx + 1, 0, dragged);
  saveState();
  render();
}

function isFolderDescendant(ancestorId, candidateId) {
  let cur = folders.find((f) => f.id === candidateId);
  while (cur?.parentId) {
    if (cur.parentId === ancestorId) return true;
    cur = folders.find((f) => f.id === cur.parentId);
  }
  return false;
}

function reparentFolder(childId, newParentId) {
  if (childId === newParentId) return;
  if (isFolderDescendant(childId, newParentId)) return; // would create cycle
  const child = folders.find((f) => f.id === childId);
  if (!child) return;
  child.parentId = newParentId;
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

// Folder names accept letters (any language), digits, spaces, and a small safe
// punctuation set — markup/symbols are rejected so names like the XSS probe or
// "±!@£$%^&*" can't be created (#170 follow-up).
const FOLDER_NAME_RE = /^[\p{L}\p{M}\p{N} '’&().,_-]+$/u;
const MAX_FOLDER_NAME_LEN = 100;
const isValidFolderName = (s) => FOLDER_NAME_RE.test(s);

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
        <input class="folder-editor-name" type="text" maxlength="100" autocomplete="off" spellcheck="false" />
      </label>
      <div class="folder-editor-field">
        <span>Color</span>
        <div class="folder-editor-colors" role="group" aria-label="Folder color">
          ${folderColorButtonsHtml(selectedColor)}
        </div>
      </div>
      <div class="folder-editor-msg" role="alert" aria-live="polite"></div>
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
  const msgEl = overlay.querySelector(".folder-editor-msg");
  input.addEventListener("input", () => { msgEl.textContent = ""; });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name) {
      msgEl.textContent = "Enter a folder name.";
      input.focus();
      return;
    }
    if (name.length > MAX_FOLDER_NAME_LEN) {
      msgEl.textContent = `Folder name is too long (max ${MAX_FOLDER_NAME_LEN}).`;
      input.focus();
      return;
    }
    if (!isValidFolderName(name)) {
      msgEl.textContent = "Use letters, numbers, spaces, or - _ ' & ( ) . ,";
      input.focus();
      return; // don't save or close until the name is valid
    }
    folder.name = name;
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
let folderDragId = null;

function isTrackDragEvent(event) {
  return dragId != null || Boolean(event?.dataTransfer?.types?.includes(TRACK_DRAG_TYPE));
}

function getDraggedTrackId(event) {
  return event?.dataTransfer?.getData(TRACK_DRAG_TYPE)
    || event?.dataTransfer?.getData("text/plain")
    || dragId;
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

function dropOnFolder(folderId, trackId) {
  const id = trackId ?? dragId;
  if (!id) return;
  // Remove from current folder
  for (const f of folders) {
    const idx = f.items.indexOf(id);
    if (idx !== -1) { f.items.splice(idx, 1); break; }
  }
  // Add to target folder
  const target = folders.find((f) => f.id === folderId);
  if (target && !target.items.includes(id)) target.items.push(id);
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

function restoreTrackFromTrash(trackId) {
  if (!tracks[trackId]) return;
  const trash = getTrashFolder();
  if (!trash?.items.includes(trackId)) return;
  trash.items = trash.items.filter((id) => id !== trackId);
  let target = folders.find((f) => f.id !== TRASH_ID);
  if (!target) {
    target = makeFolder({ id: "f-unsorted", name: "Unsorted" });
    folders.unshift(target);
  }
  if (!target.items.includes(trackId)) target.items.push(trackId);
  saveState();
  render();
}

function wireRailLibraryDrop() {
  const btn = document.querySelector(".rail-library");
  if (!btn || btn.dataset.dropReady === "1") return;
  btn.dataset.dropReady = "1";

  btn.addEventListener("dragover", (e) => {
    if (!isTrackDragEvent(e) || catalogView !== "trash") return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    btn.classList.add("drop-target");
  });
  btn.addEventListener("dragleave", (e) => {
    if (!btn.contains(e.relatedTarget)) btn.classList.remove("drop-target");
  });
  btn.addEventListener("drop", (e) => {
    btn.classList.remove("drop-target");
    if (catalogView !== "trash") return;
    const trackId = getDraggedTrackId(e);
    if (!trackId || !tracks[trackId]) return;
    e.preventDefault();
    restoreTrackFromTrash(trackId);
    setCatalogView("library");
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

// ─── Rendering helpers ───

function getRecentTracks(trashIds, n = 3) {
  return Object.entries(tracks)
    .filter(([id, t]) => !trashIds.has(id) && t.title)
    .sort(([, a], [, b]) => (b.createdAt ?? 0) - (a.createdAt ?? 0))
    .slice(0, n)
    .map(([id]) => id);
}

function getAllTags(trashIds) {
  const counts = {};
  for (const [id, track] of Object.entries(tracks)) {
    if (trashIds.has(id)) continue;
    for (const tag of track.tags ?? []) {
      counts[tag] = (counts[tag] || 0) + 1;
    }
  }
  return Object.entries(counts).sort(([, a], [, b]) => b - a);
}

function makeSectionEl(labelText) {
  const section = document.createElement("div");
  section.className = "lib-section";
  const head = document.createElement("div");
  head.className = "lib-section-head";
  head.textContent = labelText;
  section.appendChild(head);
  return section;
}

function renderRecentItem(trackId) {
  const track = tracks[trackId];
  if (!track) return null;
  const el = document.createElement("div");
  const isUnavailable = track.status === "unavailable";
  el.className = `cat-item${trackId === _currentTrackId ? " active" : ""}${isUnavailable ? " unavailable" : ""}`;
  el.dataset.id = trackId;
  const duration = track.duration ? fmtTime(track.duration) : "";
  const stemCount = track.stems?.length ?? 0;
  const sub = [duration, `${stemCount} stem${stemCount !== 1 ? "s" : ""}`].filter(Boolean).join(" · ");
  el.innerHTML = `
    <div class="cat-thumb">${thumbHtml(track)}</div>
    <div class="cat-meta">
      <div class="cat-title">${esc(track.title ?? "Unknown track")}</div>
      <div class="cat-sub"><span>${esc(sub)}</span></div>
    </div>
    <div class="cat-status${PROCESSING_STATUSES.has(track.status) ? " processing" : isUnavailable ? " unavailable" : ""}"></div>
  `;
  wireTrackDragAndLoad(el, trackId);
  return el;
}

// ─── Rendering ───

function thumbHtml(track) {
  if (track.thumb) return `<img src="${esc(track.thumb)}" alt="" loading="lazy" />`;
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
  const isUnavailable = track.status === "unavailable";
  el.className = `cat-item${trackId === _currentTrackId ? " active" : ""}${isUnavailable ? " unavailable" : ""}`;
  el.dataset.id = trackId;

  const stemCount = track.stems?.length ?? 0;
  el.innerHTML = `
    <div class="cat-thumb">${thumbHtml(track)}</div>
    <div class="cat-meta">
      <div class="cat-title">${esc(track.title ?? "Unknown track")}</div>
      <div class="cat-sub">
        <span>${esc(track.channel ?? "")}</span>
        <span class="dot">·</span>
        <span>${inTrash ? "Removed" : `${stemCount} stem${stemCount !== 1 ? "s" : ""}`}</span>
      </div>
    </div>
    <div class="cat-status${PROCESSING_STATUSES.has(track.status) ? " processing" : isUnavailable ? " unavailable" : ""}"></div>
    ${inTrash ? "" : `<button class="cat-del" type="button" title="Move to Trash">
      <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <polyline points="3 6 5 6 21 6"></polyline>
        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path>
      </svg>
    </button>`}
  `;
  el.querySelector(".cat-del")?.setAttribute("aria-label", `Move ${track.title ?? "track"} to Trash`);

  el.querySelector(".cat-del")?.addEventListener("click", (e) => {
    e.stopPropagation();
    moveTrackToTrash(trackId);
  });

  wireTrackDragAndLoad(el, trackId);

  return el;
}

function renderFolder(folder) {
  const isTrash = folder.id === TRASH_ID;
  const isUnsorted = folder.id === UNSORTED_ID;
  const isSubfolder = Boolean(folder.parentId);
  if (!isTrash) folder.color = normalizeFolderColor(folder.color);

  const el = document.createElement("div");
  el.className = `folder${folder.collapsed ? " collapsed" : ""}${isSubfolder ? " subfolder" : ""}`;
  el.dataset.id = folder.id;

  const head = document.createElement("div");
  head.className = "folder-head";
  if (!isTrash) head.style.setProperty("--folder-color", folder.color);

  const folderIcon = isTrash
    ? `<svg class="f-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>`
    : `<svg class="f-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>`;

  head.innerHTML = `
    ${isTrash ? "" : `<span class="f-grip" title="Drag to reorder">
      <svg viewBox="0 0 24 24" width="10" height="10" fill="currentColor" aria-hidden="true">
        <circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/>
        <circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/>
        <circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/>
      </svg>
    </span>`}
    <svg class="f-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"></polyline></svg>
    ${folderIcon}
    <span class="f-name">${esc(folder.name)}</span>
    <span class="f-count">${folder.items.length}</span>
    ${isTrash ? "" : `
      <button class="f-subfolder" type="button" aria-label="New subfolder" title="New subfolder">
        <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
          <path d="M12 11v6M9 14h6"/>
        </svg>
      </button>
      ${isUnsorted ? "" : `<button class="f-del" type="button" aria-label="Delete folder" title="Delete folder">
        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>
      </button>`}
    `}
  `;

  const body = document.createElement("div");
  body.className = "folder-body";

  const visibleItems = folder.items.filter((id) => trackMatchesSearch(tracks[id]));
  const childFolders = folders.filter((f) => f.parentId === folder.id);

  if (catalogSearchQuery && visibleItems.length === 0 && childFolders.length === 0) {
    return null;
  }

  if (visibleItems.length === 0 && childFolders.length === 0) {
    body.innerHTML = '<span class="folder-empty">Empty folder</span>';
  } else {
    for (const id of visibleItems) {
      const item = renderTrackItem(id);
      if (item) body.appendChild(item);
    }
    for (const child of childFolders) {
      const childEl = renderFolder(child);
      if (childEl) body.appendChild(childEl);
    }
  }

  el.append(head, body);

  let folderClickTimer = null;

  // Toggle folder collapse on single click.
  head.addEventListener("click", (e) => {
    if (e.target.closest(".f-del, .f-subfolder, .f-grip")) return;
    if (e.detail !== 1) return;
    window.clearTimeout(folderClickTimer);
    folderClickTimer = window.setTimeout(() => {
      folder.collapsed = !folder.collapsed;
      el.classList.toggle("collapsed", folder.collapsed);
      saveState();
    }, 180);
  });

  if (!isTrash) {
    head.addEventListener("dblclick", (e) => {
      if (e.target.closest(".f-del, .f-subfolder, .f-grip")) return;
      window.clearTimeout(folderClickTimer);
      e.stopPropagation();
      openFolderEditor(folder.id);
    });
  }

  head.querySelector(".f-del")?.addEventListener("click", (e) => {
    e.stopPropagation();
    deleteFolder(folder.id);
  });

  head.querySelector(".f-subfolder")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const child = makeFolder({ parentId: folder.id });
    folders.push(child);
    folder.collapsed = false;
    el.classList.remove("collapsed");
    saveState();
    render();
    openFolderEditor(child.id);
  });

  // Folder drag handle — reorder folders.
  const grip = head.querySelector(".f-grip");
  if (grip) {
    grip.draggable = true;
    grip.addEventListener("dragstart", (e) => {
      folderDragId = folder.id;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData(FOLDER_DRAG_TYPE, folder.id);
      e.stopPropagation();
      requestAnimationFrame(() => el.classList.add("folder-dragging"));
    });
    grip.addEventListener("dragend", () => {
      folderDragId = null;
      el.classList.remove("folder-dragging");
      for (const f of document.querySelectorAll(".folder.drop-before, .folder.drop-after, .folder.drop-into")) {
        f.classList.remove("drop-before", "drop-after", "drop-into");
      }
    });
  }

  // Dragover: folder reorder/nest indicator OR track drop target.
  el.addEventListener("dragover", (e) => {
    if (folderDragId && folderDragId !== folder.id && !isTrash) {
      if (isFolderDescendant(folderDragId, folder.id)) return; // prevent cycle
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const rect = head.getBoundingClientRect();
      const rel = (e.clientY - rect.top) / rect.height;
      el.classList.toggle("drop-before", rel < 0.25);
      el.classList.toggle("drop-into", rel >= 0.25 && rel < 0.75);
      el.classList.toggle("drop-after", rel >= 0.75);
      return;
    }
    if (!isTrackDragEvent(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    el.classList.add("drop-target");
  });
  el.addEventListener("dragleave", (e) => {
    if (!el.contains(e.relatedTarget)) {
      el.classList.remove("drop-target", "drop-before", "drop-after", "drop-into");
    }
  });
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    if (folderDragId && folderDragId !== folder.id && !isTrash) {
      const rect = head.getBoundingClientRect();
      const rel = (e.clientY - rect.top) / rect.height;
      el.classList.remove("drop-before", "drop-after", "drop-into");
      if (rel < 0.25) reorderFolder(folderDragId, folder.id, true);
      else if (rel >= 0.75) reorderFolder(folderDragId, folder.id, false);
      else reparentFolder(folderDragId, folder.id);
      return;
    }
    el.classList.remove("drop-target");
    dropOnFolder(folder.id, getDraggedTrackId(e));
  });

  return el;
}

function renderStrip(strip, nonTrash) {
  if (!strip) return;
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

function render() {
  const list = document.getElementById("catalogList");
  const strip = document.getElementById("catalogStrip");
  const catalog = document.getElementById("catalogPanel");
  const searchInput = document.getElementById("catalogSearch");
  if (!list) return;

  list.innerHTML = "";
  if (strip) strip.innerHTML = "";

  const trash = getTrashFolder();
  const trashIds = new Set(trash?.items || []);
  const isTrashView = catalogView === "trash";
  const isFavoritesView = catalogView === "favorites";
  const isLibraryView = !isTrashView && !isFavoritesView;

  catalog?.classList.toggle("trash-view", isTrashView);
  catalog?.classList.toggle("favorites-view", isFavoritesView);

  document.querySelector(".rail-library")?.classList.toggle("active", isLibraryView);
  document.querySelector(".rail-library")?.setAttribute("aria-pressed", String(isLibraryView));
  document.querySelector(".rail-favorites")?.classList.toggle("active", isFavoritesView);
  document.querySelector(".rail-favorites")?.setAttribute("aria-pressed", String(isFavoritesView));
  document.querySelector(".rail-trash")?.classList.toggle("active", isTrashView);
  document.querySelector(".rail-trash")?.setAttribute("aria-pressed", String(isTrashView));

  if (searchInput) {
    searchInput.placeholder = isTrashView ? "Search trash…" : isFavoritesView ? "Search favorites…" : "Search library…";
  }

  const nonTrash = folders.filter((f) => f.id !== TRASH_ID && !f.parentId);

  // ── Trash view ──
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

  // ── Favorites view ──
  if (isFavoritesView) {
    const favIds = Object.entries(tracks)
      .filter(([id, t]) => !trashIds.has(id) && t.favorite && trackMatchesSearch(t))
      .sort(([, a], [, b]) => (b.createdAt ?? 0) - (a.createdAt ?? 0))
      .map(([id]) => id);
    if (!favIds.length) {
      list.innerHTML = `<span class="folder-empty trash-empty">${catalogSearchQuery ? "No favorites match your search" : "No favorites yet — click ♥ on a track to save it"}</span>`;
    } else {
      for (const id of favIds) {
        const item = renderRecentItem(id);
        if (item) list.appendChild(item);
      }
    }
    renderStrip(strip, nonTrash);
    return;
  }

  // ── Library view — Recent · Stem Collections · Tags ──

  // Recent section
  const recentIds = getRecentTracks(trashIds).filter((id) => trackMatchesSearch(tracks[id]));
  if (recentIds.length) {
    const section = makeSectionEl("Recent");
    for (const id of recentIds) {
      const item = renderRecentItem(id);
      if (item) section.appendChild(item);
    }
    list.appendChild(section);
  }

  // Stem Collections section
  const collectionsSection = makeSectionEl("Stem Collections");
  const newFolderBtn = document.createElement("button");
  newFolderBtn.id = "newFolderBtn";
  newFolderBtn.className = "new-folder-btn";
  newFolderBtn.type = "button";
  newFolderBtn.setAttribute("aria-label", "New folder");
  newFolderBtn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 11v6 M9 14h6"/></svg>New folder`;
  newFolderBtn.addEventListener("click", createFolder);
  collectionsSection.querySelector(".lib-section-head").appendChild(newFolderBtn);
  let hasCollections = false;
  for (const folder of nonTrash) {
    const el = renderFolder(folder);
    if (!el) continue;
    collectionsSection.appendChild(el);
    hasCollections = true;
  }
  if (hasCollections) list.appendChild(collectionsSection);

  // Empty state when search yields nothing
  if (catalogSearchQuery && !recentIds.length && !hasCollections) {
    list.innerHTML = '<span class="folder-empty trash-empty">No tracks match your search</span>';
    return;
  }

  // Tags section
  const tags = getAllTags(trashIds);
  if (tags.length) {
    const section = makeSectionEl("Tags");
    const row = document.createElement("div");
    row.className = "lib-tags-row";
    const activeTag = catalogSearchQuery.startsWith("#") ? catalogSearchQuery.slice(1) : null;
    for (const [tag, count] of tags) {
      const chip = document.createElement("button");
      chip.className = `lib-tag-chip${activeTag === tag ? " active" : ""}`;
      chip.type = "button";
      chip.dataset.tag = tag;
      chip.textContent = tag;
      const countSpan = document.createElement("span");
      countSpan.className = "lib-tag-count";
      countSpan.textContent = String(count);
      chip.appendChild(countSpan);
      chip.addEventListener("click", () => {
        const input = document.getElementById("catalogSearch");
        if (catalogSearchQuery === `#${tag}`) {
          catalogSearchQuery = "";
          if (input) input.value = "";
        } else {
          catalogSearchQuery = `#${tag}`;
          if (input) input.value = `#${tag}`;
        }
        render();
      });
      row.appendChild(chip);
    }
    section.appendChild(row);
    list.appendChild(section);
  }

  renderStrip(strip, nonTrash);
}

// ─── Catalog panel collapse ───

function wireCatalogToggle() {
  const toggle = document.getElementById("catalogToggle");
  const collapseBtn = document.getElementById("sidebarCollapseBtn");
  const app = document.querySelector(".app");
  if (!app) return;

  const collapsed = localStorage.getItem("stemdeck.catalog.collapsed") === "1";
  if (collapsed) {
    app.classList.add("cat-collapsed");
    collapseBtn?.setAttribute("aria-expanded", "false");
  }

  function setSidebarCollapsed(isCollapsed) {
    app.classList.toggle("cat-collapsed", isCollapsed);
    collapseBtn?.setAttribute("aria-expanded", String(!isCollapsed));
    localStorage.setItem("stemdeck.catalog.collapsed", isCollapsed ? "1" : "0");
  }

  collapseBtn?.addEventListener("click", () => {
    setSidebarCollapsed(!app.classList.contains("cat-collapsed"));
  });

  if (toggle) {
    toggle.addEventListener("click", (e) => {
      // Only expand from within the sidebar body.
      if (!app.classList.contains("cat-collapsed")) return;
      setSidebarCollapsed(false);
      toggle.querySelector("input")?.focus();
    });
    toggle.addEventListener("keydown", (e) => {
      if (e.code === "Enter" || e.code === "Space") { e.preventDefault(); toggle.click(); }
    });
  }
}

function wireCatalogRailViews() {
  document.querySelector(".rail-library")?.addEventListener("click", () => setCatalogView("library"));
  document.querySelector(".rail-favorites")?.addEventListener("click", () => setCatalogView("favorites"));
  document.querySelector(".rail-trash")?.addEventListener("click", () => setCatalogView("trash"));
  document.getElementById("clearBinBtn")?.addEventListener("click", () => {
    const trash = getTrashFolder();
    const toDelete = [...(trash?.items || [])];
    markJobsDeleted(toDelete); // persist before purge so reload can't re-import
    purgeTrash();
    saveState();
    render();
    for (const id of toDelete) {
      fetch(`/api/jobs/${id}`, { method: "DELETE" }).catch(() => {});
    }
  });
}

function wireCatalogSearch() {
  const input = document.getElementById("catalogSearch");
  if (!input || input.dataset.searchReady === "1") return;
  input.dataset.searchReady = "1";

  const suggest = document.getElementById("tagSuggest");

  function hideSuggest() {
    if (suggest) suggest.innerHTML = "";
  }

  function showTagSuggestions(prefix) {
    if (!suggest) return;
    suggest.innerHTML = "";
    if (!prefix) { hideSuggest(); return; }
    const trashIds = new Set(folders.find((f) => f.id === TRASH_ID)?.items || []);
    const all = getAllTags(trashIds);
    const matches = all.filter(([t]) => t.toLowerCase().includes(prefix));
    if (!matches.length) { hideSuggest(); return; }
    for (const [tag] of matches.slice(0, 8)) {
      const li = document.createElement("li");
      li.className = "tag-suggest-item";
      li.setAttribute("role", "option");
      li.textContent = `#${tag}`;
      li.addEventListener("mousedown", (e) => {
        e.preventDefault();
        input.value = `#${tag}`;
        catalogSearchQuery = `#${tag}`;
        hideSuggest();
        render();
      });
      suggest.appendChild(li);
    }
  }

  input.addEventListener("input", () => {
    catalogSearchQuery = normalizeSearch(input.value);
    render();
    const val = input.value;
    if (val.startsWith("#")) {
      showTagSuggestions(val.slice(1).toLowerCase());
    } else {
      hideSuggest();
    }
  });

  input.addEventListener("blur", () => setTimeout(hideSuggest, 150));
  input.addEventListener("keydown", (e) => {
    if (!suggest?.children.length) return;
    if (e.key === "Escape") { hideSuggest(); return; }
    const items = [...suggest.querySelectorAll(".tag-suggest-item")];
    const active = suggest.querySelector(".tag-suggest-item.focused");
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = active ? (items[items.indexOf(active) + 1] || items[0]) : items[0];
      active?.classList.remove("focused");
      next.classList.add("focused");
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const prev = active ? (items[items.indexOf(active) - 1] || items[items.length - 1]) : items[items.length - 1];
      active?.classList.remove("focused");
      prev.classList.add("focused");
    } else if (e.key === "Enter" && active) {
      e.preventDefault();
      active.dispatchEvent(new MouseEvent("mousedown"));
    }
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
const REPO_URL = "https://github.com/stemdeckapp/stemdeck";
const RELEASES_URL = "https://github.com/stemdeckapp/stemdeck/releases";
const RELEASES_API = "https://api.github.com/repos/stemdeckapp/stemdeck/releases/latest";
const DISMISSED_UPDATE_KEY = "stemdeck.dismissed_update";

function normalizeVersion(value) {
  return String(value || "").trim().replace(/^v/i, "") || FALLBACK_VERSION;
}

// Fold a version into one canonical form so the GitHub release tag
// ("0.7.0-alpha.9") and the backend's PEP440 package version ("0.7.0a9", from
// hatch-vcs via /api/health) compare equal. Without this the update banner
// shows on every release because the two strings never match literally.
function canonicalVersion(value) {
  return normalizeVersion(value)
    .toLowerCase()
    .replace(/[-_]/g, "")            // 0.7.0-alpha.9 -> 0.7.0alpha.9
    .replace(/alpha/g, "a")
    .replace(/beta/g, "b")
    .replace(/preview|pre/g, "rc")
    .replace(/(a|b|rc)\.?(\d)/g, "$1$2"); // alpha.9/a.9 -> a9
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
  } catch (e) { console.warn("[catalog] version fetch failed:", e); }
}

async function checkForUpdate() {
  try {
    const res = await fetch(RELEASES_API, { headers: { Accept: "application/vnd.github+json" } });
    if (!res.ok) return;
    const data = await res.json();
    const latest = normalizeVersion(data.tag_name);
    // Compare canonically so a PEP440 current version (0.7.0a9) matches the
    // release tag form (0.7.0-alpha.9) and we don't nag an already-current app.
    if (!latest || canonicalVersion(latest) === canonicalVersion(currentVersion)) return;
    // Dev/source builds report a git-derived version (e.g. 0.7.0a5.dev3+g…) that
    // is *ahead* of the last release — don't nag them with an "update" banner.
    if (/\bdev\b|\+/.test(currentVersion)) return;

    let dismissed = null;
    try { dismissed = localStorage.getItem(DISMISSED_UPDATE_KEY); } catch (e) { console.warn(e); }
    if (dismissed === latest) return;

    const card = document.getElementById("notifReleaseCard");
    const desc = document.getElementById("notifReleaseDesc");
    const badge = document.getElementById("notifBadge");
    const empty = document.getElementById("notifEmpty");
    const dismissBtn = document.getElementById("notifReleaseDismiss");

    if (desc) desc.textContent = `v${latest}`;
    card?.classList.remove("hidden");
    badge?.classList.remove("hidden");
    empty?.classList.add("hidden");

    dismissBtn?.addEventListener("click", () => {
      try { localStorage.setItem(DISMISSED_UPDATE_KEY, latest); } catch (e) { console.warn(e); }
      card?.classList.add("hidden");
      badge?.classList.add("hidden");
      empty?.classList.remove("hidden");
    }, { once: true });
  } catch (e) { console.warn("[catalog] update check failed:", e); }
}

function wireAboutDialog() {
  const btn = document.getElementById("aboutBtn");
  const dialog = document.getElementById("aboutDialog");
  const close = document.getElementById("aboutClose");
  const version = document.getElementById("aboutVersion");
  if (!btn || !dialog) return;

  if (version) version.textContent = `v${currentVersion}`;

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

// Supporters dialog: a TV rail button opens a centered modal (like About) with
// the partner tiles. Links open externally via the document-level
// a[target="_blank"] handler in main.js (Tauri open_url on desktop).
function wireSupportersDialog() {
  const btn = document.getElementById("friendsBtn");
  const dialog = document.getElementById("friendsDialog");
  const close = document.getElementById("friendsClose");
  const grid = document.getElementById("friendsDialogGrid");
  if (!btn || !dialog) return;

  if (grid && grid.dataset.ready !== "1") {
    grid.dataset.ready = "1";
    // Masonry: round-robin tiles into fixed columns so a tall tile in one
    // column does not push the next row down. Small per-tile tilt gives the
    // deliberately-uneven "frames on a wall" look.
    const COLS = 3;
    const tilts = ["-2deg", "1.5deg", "-1deg", "2deg", "-1.5deg", "1deg"];
    const cols = [];
    for (let i = 0; i < COLS; i++) {
      const col = document.createElement("div");
      col.className = "lib-friends-col";
      cols.push(col);
      grid.appendChild(col);
    }
    FRIENDS.forEach((f, i) => {
      const a = document.createElement("a");
      a.className = "lib-friend";
      a.href = f.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.title = f.name;
      a.style.setProperty("--tilt", tilts[i % tilts.length]);
      // A monogram avatar (first initial) keeps the tile on-brand when an entry
      // has no image, or its image fails to load (e.g. before the asset is added).
      const makeMonogram = () => {
        const m = document.createElement("span");
        m.className = "lib-friend-monogram";
        m.textContent = (f.name || "?").trim().charAt(0).toUpperCase();
        m.setAttribute("aria-hidden", "true");
        return m;
      };
      if (f.logo) {
        const img = document.createElement("img");
        img.className = f.avatar ? "lib-friend-avatar" : "lib-friend-logo";
        img.src = f.logo;
        img.alt = f.name;
        img.loading = "lazy";
        img.addEventListener("error", () => img.replaceWith(makeMonogram()));
        a.appendChild(img);
      } else {
        a.appendChild(makeMonogram());
      }
      const name = document.createElement("span");
      name.className = "lib-friend-name";
      name.textContent = f.name;
      a.appendChild(name);
      if (f.role) {
        const role = document.createElement("span");
        role.className = "lib-friend-role";
        role.textContent = f.role;
        a.appendChild(role);
      }
      if (/instagram\.com/i.test(f.url || "")) {
        const SVGNS = "http://www.w3.org/2000/svg";
        const ig = document.createElementNS(SVGNS, "svg");
        ig.setAttribute("class", "lib-friend-ig");
        ig.setAttribute("viewBox", "0 0 24 24");
        ig.setAttribute("aria-hidden", "true");
        const p = document.createElementNS(SVGNS, "path");
        p.setAttribute("d", IG_ICON_PATH);
        ig.appendChild(p);
        a.appendChild(ig);
      }
      cols[i % COLS].appendChild(a);
    });
  }

  const open = () => dialog.classList.remove("hidden");
  const hide = () => dialog.classList.add("hidden");
  btn.addEventListener("click", open);
  close?.addEventListener("click", hide);
  dialog.addEventListener("mousedown", (e) => { if (e.target === dialog) hide(); });
  dialog.addEventListener("keydown", (e) => { if (e.code === "Escape") hide(); });
}

async function syncWithServer() {
  try {
    const res = await fetch("/api/jobs", { cache: "no-store" });
    if (!res.ok) return;
    const jobs = await res.json();
    const trashIds = new Set(getTrashFolder()?.items || []);
    const deletedIds = getDeletedJobIds();
    for (const state of jobs) {
      if (tracks[state.job_id]) continue;
      if (trashIds.has(state.job_id)) continue;   // soft-deleted, skip
      if (deletedIds.has(state.job_id)) continue; // hard-deleted, skip
      const track = stateMetadataToTrack(state, { id: state.job_id, status: state.status });
      track.id = state.job_id;
      addTrackToLibrary(track);
    }
  } catch (e) { console.warn("[catalog] failed to load jobs from backend:", e); }
}

// ─── Settings menu + Library editor ───

let libraryEditor = null;
let libraryEditorOnKey = null;

// Human-readable "Location" for a track: the imported filename for local
// uploads, otherwise the source URL.
function libraryLocation(sourceUrl) {
  if (!sourceUrl) return "—";
  if (sourceUrl.startsWith("local:")) return sourceUrl.slice(6) || "Imported file";
  return sourceUrl;
}

// Count library tracks (excluding Trash) whose audio is gone.
function libraryUnavailableCount() {
  const trashIds = new Set(getTrashFolder()?.items || []);
  return Object.entries(tracks)
    .filter(([id, t]) => !trashIds.has(id) && t.status === "unavailable").length;
}

// Update the editor's footer line with the out-of-sync count (red) or an
// all-clear message. Safe no-op when the editor isn't open.
function refreshLibrarySyncSummary() {
  const statusEl = libraryEditor?.querySelector(".library-editor-status");
  if (!statusEl) return;
  const n = libraryUnavailableCount();
  statusEl.classList.toggle("out-of-sync", n > 0);
  statusEl.textContent = n > 0
    ? `${n} ${n === 1 ? "track is" : "tracks are"} out of sync`
    : "All tracks in sync";
}

function closeLibraryEditor() {
  if (libraryEditorOnKey) {
    document.removeEventListener("keydown", libraryEditorOnKey);
    libraryEditorOnKey = null;
  }
  libraryEditor?.remove();
  libraryEditor = null;
}

// Fill the editor's table body from `tracks` (skips Trash). Built via DOM +
// textContent — titles/URLs are untrusted (YouTube/SoundCloud) so never
// interpolate them into innerHTML.
function renderLibraryRows(tbody) {
  tbody.textContent = "";
  const trashIds = new Set(getTrashFolder()?.items || []);
  // Only out-of-sync (audio missing) tracks — this table sits next to Resync.
  const entries = Object.entries(tracks)
    .filter(([id, t]) => !trashIds.has(id) && t.status === "unavailable")
    .sort((a, b) => (b[1].createdAt || 0) - (a[1].createdAt || 0));

  if (!entries.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "library-editor-empty";
    td.textContent = "All tracks are in sync.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  for (const [id, t] of entries) {
    const tr = document.createElement("tr");
    tr.dataset.id = id;
    if (t.status === "unavailable") tr.className = "unavailable";

    const name = document.createElement("td");
    name.className = "le-name";
    name.textContent = t.title || "—";
    name.title = t.title || "";
    if (t.status === "unavailable") {
      const badge = document.createElement("span");
      badge.className = "le-badge";
      badge.textContent = "unavailable";
      name.appendChild(badge);
    }

    const source = document.createElement("td");
    source.className = "le-source";
    source.textContent = deriveSource(t.sourceUrl);

    const loc = document.createElement("td");
    loc.className = "le-loc";
    const locText = libraryLocation(t.sourceUrl);
    loc.textContent = locText;
    loc.title = locText;

    tr.append(name, source, loc);
    tbody.appendChild(tr);
  }
}

// "Make StemDeck available on your network" toggle. The backend always binds
// all interfaces and gates LAN access on a runtime flag (GET/POST /api/settings)
// — so this works live, no restart, identically in the desktop app and the
// self-hosted server. Loopback is always allowed, so the owner can't lock
// themselves out of this control.
function networkSettingsHtml() {
  return `
    <div class="settings-section">
      <div class="settings-row">
        <div class="settings-row-text">
          <div class="settings-row-title">Make StemDeck available on your network</div>
          <div class="settings-row-desc">Let other devices (like your phone) open StemDeck at the address below.</div>
        </div>
        <label class="settings-switch">
          <input type="checkbox" class="net-access-input" />
          <span class="settings-switch-track"><span class="settings-switch-thumb"></span></span>
        </label>
      </div>
      <div class="settings-net hidden">
        <div class="settings-net-label">Access on your local network by any of these addresses:</div>
        <div class="settings-net-list"></div>
      </div>
    </div>
  `;
}

// General settings: max track length (minutes) + MP4 video quality. Read live
// and POSTed on change to /api/settings (same runtime store as the toggle).
async function wireGeneralSettings(overlay) {
  const durInput = overlay.querySelector(".set-max-duration");
  const heightSel = overlay.querySelector(".set-video-height");
  if (!durInput && !heightSel) return;

  const apply = (d) => {
    if (durInput && d.max_duration_sec) durInput.value = String(Math.round(d.max_duration_sec / 60));
    if (heightSel && d.video_max_height) heightSel.value = String(d.video_max_height);
  };

  try {
    const r = await fetch("/api/settings", { cache: "no-store" });
    if (r.ok) apply(await r.json());
  } catch { /* leave blank */ }

  const post = async (patch) => {
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (r.ok) apply(await r.json()); // reflect the server's clamped value
    } catch { /* ignore */ }
  };

  durInput?.addEventListener("change", () => {
    const mins = Math.max(1, Math.min(20, parseInt(durInput.value, 10) || 20));
    post({ max_duration_sec: mins * 60 });
  });
  heightSel?.addEventListener("change", () => {
    post({ video_max_height: parseInt(heightSel.value, 10) });
  });
}

async function wireNetworkSetting(overlay) {
  const input = overlay.querySelector(".net-access-input");
  const netWrap = overlay.querySelector(".settings-net");
  const list = overlay.querySelector(".settings-net-list");
  if (!input) return;

  let enabled = false;
  let addresses = [];
  try {
    const r = await fetch("/api/settings", { cache: "no-store" });
    if (r.ok) {
      const data = await r.json();
      enabled = data.allow_network === true;
      addresses = Array.isArray(data.lan_addresses) ? data.lan_addresses : [];
    }
  } catch { /* leave defaults */ }

  // Build the address list with textContent (URLs are server data, but never
  // interpolate untrusted strings into innerHTML).
  if (list) {
    list.textContent = "";
    if (addresses.length) {
      for (const a of addresses) {
        const code = document.createElement("code");
        code.textContent = a;
        list.appendChild(code);
      }
    } else {
      const span = document.createElement("span");
      span.className = "settings-net-empty";
      span.textContent = "No local network connection detected.";
      list.appendChild(span);
    }
  }

  input.checked = enabled;
  const refresh = () => netWrap?.classList.toggle("hidden", !input.checked);
  refresh();

  input.addEventListener("change", async () => {
    const want = input.checked;
    input.disabled = true;
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ allow_network: want }),
      });
      input.checked = r.ok ? (await r.json()).allow_network === true : !want;
    } catch {
      input.checked = !want; // revert on failure
    } finally {
      input.disabled = false;
      refresh();
    }
  });
}

function openLibraryEditor() {
  closeFolderEditor();
  closeLibraryEditor();

  const overlay = document.createElement("div");
  overlay.className = "library-editor-backdrop";
  overlay.innerHTML = `
    <div class="library-editor" role="dialog" aria-modal="true" aria-label="Settings">
      <div class="library-editor-head">
        <span>Settings</span>
        <button class="library-editor-close" type="button" aria-label="Close">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"></path></svg>
        </button>
      </div>
      <div class="settings-tabs" role="tablist">
        <button class="settings-tab active" type="button" data-tab="general" role="tab">General</button>
        <button class="settings-tab" type="button" data-tab="advanced" role="tab">Advanced</button>
      </div>
      <div class="settings-pane" data-pane="general">
        <div class="settings-section">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title">Max track length</div>
              <div class="settings-row-desc">Longest track accepted for processing.</div>
            </div>
            <div class="settings-num">
              <input type="number" class="set-max-duration" min="1" max="20" step="1" inputmode="numeric" />
              <span class="settings-num-unit">min</span>
            </div>
          </div>
        </div>
        <div class="settings-section">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title">MP4 video quality</div>
              <div class="settings-row-desc">Max resolution for MP4 export and YouTube video.</div>
            </div>
            <select class="settings-select set-video-height">
              <option value="360">360p</option>
              <option value="480">480p</option>
              <option value="720">720p</option>
              <option value="1080">1080p</option>
            </select>
          </div>
        </div>
      </div>
      <div class="settings-pane hidden" data-pane="advanced">
        ${networkSettingsHtml()}
        <div class="settings-subhead">Out of sync tracks</div>
        <div class="library-editor-table-wrap">
          <table class="library-editor-table">
            <thead><tr><th>Name</th><th>Source</th><th>Location</th></tr></thead>
            <tbody class="library-editor-body"></tbody>
          </table>
        </div>
        <div class="library-editor-foot">
          <span class="library-editor-status" aria-live="polite"></span>
          <button class="library-editor-sync" type="button">Resync out of sync tracks</button>
        </div>
      </div>
    </div>
  `;

  renderLibraryRows(overlay.querySelector(".library-editor-body"));

  overlay.querySelectorAll(".settings-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.dataset.tab;
      overlay.querySelectorAll(".settings-tab").forEach((t) => t.classList.toggle("active", t === tab));
      overlay.querySelectorAll(".settings-pane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== name));
    });
  });

  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeLibraryEditor(); });
  // (status summary is filled in after the overlay is in the DOM, below)
  overlay.querySelector(".library-editor-close")?.addEventListener("click", closeLibraryEditor);
  overlay.querySelector(".library-editor-sync")?.addEventListener("click", () => resyncLibrary());
  // Escape closes from anywhere (the overlay isn't focused, so listen on document).
  libraryEditorOnKey = (e) => { if (e.code === "Escape") closeLibraryEditor(); };
  document.addEventListener("keydown", libraryEditorOnKey);

  document.body.appendChild(overlay);
  libraryEditor = overlay;
  refreshLibrarySyncSummary();
  wireGeneralSettings(overlay);
  wireNetworkSetting(overlay);
}

// Poll a job until it reaches a terminal state, so auto-restores run one at a
// time (applyState/connectEvents drive a single active studio job — overlapping
// restores would fight over the studio). Caps at 30 min as a safety net.
async function waitForJobTerminal(jobId) {
  const deadline = Date.now() + 30 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1500));
    try {
      const r = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
      if (r.status === 404) return;
      const s = await r.json();
      if (s.status === "done" || s.status === "error" || s.status === "cancelled") return;
    } catch { /* transient — keep waiting */ }
  }
}

// "Sync again": reconcile the library with the backend, then auto-restore the
// tracks that fell out of sync.
//   forward  — add server jobs missing locally (syncWithServer)
//   reverse  — flag local "done" tracks the server no longer has as
//              "unavailable"; restore ones that reappeared on the server.
//   restore  — re-import every currently-unavailable URL-sourced track
//              (re-download + re-separate). Local-file tracks can't be
//              auto-restored (the original file isn't kept) — they stay flagged.
// Only done↔unavailable are reconciled, so in-progress imports are never
// mis-flagged (they aren't on the server's done-list yet).
async function resyncLibrary() {
  const statusEl = libraryEditor?.querySelector(".library-editor-status");
  const syncBtn = libraryEditor?.querySelector(".library-editor-sync");
  if (statusEl) statusEl.textContent = "Syncing…";
  if (syncBtn) syncBtn.disabled = true;

  try {
    const res = await fetch("/api/jobs", { cache: "no-store" });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const jobs = await res.json();
    const serverIds = new Set(jobs.map((j) => j.job_id));

    await syncWithServer(); // forward: pull in any new server jobs

    const trashIds = new Set(getTrashFolder()?.items || []);
    for (const [id, t] of Object.entries(tracks)) {
      if (trashIds.has(id)) continue;
      if (t.status === "done" && !serverIds.has(id)) t.status = "unavailable";
      else if (t.status === "unavailable" && serverIds.has(id)) t.status = "done";
    }
    saveState();
    render();
    if (libraryEditor) renderLibraryRows(libraryEditor.querySelector(".library-editor-body"));

    // Collect what's still unavailable; auto-restore the ones with a URL source.
    const unavailable = Object.entries(tracks)
      .filter(([id, t]) => !trashIds.has(id) && t.status === "unavailable")
      .map(([, t]) => t);
    const restorable = unavailable.filter((t) => t.sourceUrl && !t.sourceUrl.startsWith("local:"));

    if (restorable.length) {
      // Re-import each from its source. Close the editor so the studio overlay
      // shows progress; restore sequentially (single active studio job).
      closeLibraryEditor();
      for (const t of restorable) {
        const jobId = await importFromUrl(t.sourceUrl, {
          title: t.title,
          stems: t.selectedStems,
        });
        if (jobId) await waitForJobTerminal(jobId);
      }
      return;
    }

    // Nothing auto-restorable left — show the out-of-sync count (local-file
    // tracks can't be re-fetched and stay flagged).
    refreshLibrarySyncSummary();
  } catch (e) {
    console.warn("[catalog] resync failed:", e);
    if (statusEl) statusEl.textContent = "Sync failed — check your connection.";
  } finally {
    if (syncBtn) syncBtn.disabled = false;
  }
}

function wireSettingsMenu() {
  const btn = document.getElementById("settingsBtn");
  if (!btn || btn.dataset.menuReady === "1") return;
  btn.dataset.menuReady = "1";
  // The only setting today is the library, so Settings opens the Edit Library
  // window directly (a centered modal, like the About dialog).
  btn.addEventListener("click", openLibraryEditor);
}

export async function initCatalog() {
  await loadState();
  wireCatalogToggle();
  wireCatalogRailViews();
  wireCatalogSearch();
  wireWidgets();
  wireMainPanelDrop();
  wireRailTrashDrop();
  wireRailLibraryDrop();
  wireLibraryDeleteKeys();
  wireAboutDialog();
  wireSupportersDialog();
  wireSettingsMenu();
  setDisplayedVersion(currentVersion);
  render();


  loadCurrentVersion().finally(checkForUpdate);
  syncWithServer();
}
