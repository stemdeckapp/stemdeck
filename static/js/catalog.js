// catalog.js — library panel: folders, tracks, collapse, drag-and-drop
import { STEM_NAMES } from "./constants.js";
import { wireUpAudio, updateFooterTrack } from "./player.js";
import { initSections } from "./sections.js";
import { bpmChip, foregroundJobId, keyChip, saveSelectedStems, selectedStems, titleEl } from "./state.js";
import { showError, importFromUrl, detachForegroundJob, runVocalSplitIfWanted } from "./job.js";
import {
  cancelQueuedJob, getQueueSnapshot, isPaused, onJobSettled, onQueueChange,
  queueCount, queueRowStates, reorderQueuedJob, runningLabel, startQueue,
  startQueueStream,
} from "./queue.js";
import { fmtTime, storeGet, storeSet } from "./utils.js";
import { notifyFailure, setReleasePending, dismissFailuresByJobId, dismissFailuresByKind } from "./notifications.js";
// Aliased (not the bare "t") -- this file already uses "t"/"tr" as local
// variable names for track objects and table rows in several scopes.
import { t as i18nT, plural as i18nPlural, LANGUAGES, getLanguage, setLanguage, onLanguageChange, applyTranslations } from "./i18n.js";

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
  {
    name: "Analog4Lyfe",
    role: "All-analog music gear, no digital shortcuts",
    url: "https://www.instagram.com/analog4lyfe",
    logo: "/img/friends/analog4lyfe.jpg",
    avatar: true,
  },
  {
    name: "Beltr",
    role: "Turns the songs you already own into karaoke gold, right on your own machine, no subscription, no cloud, just you and the mic",
    url: "https://beltr.app/",
  },
  {
    name: "Dlima Guitars",
    role: "Custom guitars and basses, built one at a time",
    url: "https://www.instagram.com/dlimaguitars",
    logo: "/img/friends/dlima-guitars-ig.jpg",
    avatar: true,
  },
  {
    name: "Empress Effects",
    role: "Boutique effects pedals for tone chasers who don't settle",
    url: "https://empresseffects.com",
    logo: "/img/friends/empress-effects.png",
  },
  {
    name: "Joao Gaspar",
    role: "Producer and film scorer, also plays as a touring/session musician",
    url: "https://www.instagram.com/jay_glaspar",
    logo: "/img/friends/joao-gaspar.jpg",
    avatar: true,
  },
  {
    name: "Kris Luthier",
    role: "Hand-repairs and restores instruments in Lisbon, one careful fix at a time",
    url: "https://www.instagram.com/krisluthier",
    logo: "/img/friends/kris-luthier.jpg",
    avatar: true,
  },
  {
    name: "Lisbon Guitar Works",
    role: "Guitars built by hand in Lisbon",
    url: "https://dlimaguitars.com",
    logo: "/img/friends/lisbon-guitar-works.webp",
  },
  {
    name: "More Notes Less Talk",
    role: "Instruments and gear with personality, recorded raw to tape. No hype, no gatekeeping.",
    url: "https://www.youtube.com/@morenoteslesstalk",
  },
  {
    name: "Seratone",
    role: "Turns any TV into a studio-grade karaoke stage",
    url: "https://seratone.audio/",
  },
  {
    name: "slashCAM",
    role: "German-language camera and video tech: hands-on tests, industry news, and the post-production details most reviews skip",
    url: "https://www.instagram.com/slashcam.de",
    logo: "/img/friends/slashcam.webp",
  },
  {
    name: "Thomann",
    role: "One of Europe's largest music gear retailers, practically everything a musician could need",
    url: "https://www.instagram.com/thomann.music",
    logo: "/img/friends/thomann.jpg",
    avatar: true,
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

function makeFolder({ id = `f-${Date.now()}`, name = i18nT("library.newFolder"), collapsed = false, items = [], parentId = null } = {}) {
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
  // Catches a job that errored after its track was trashed but before this
  // permanent delete — moveTrackToTrash's own dismiss already fired earlier
  // and can't have caught a failure that didn't exist yet (#401).
  for (const id of trashIds) dismissFailuresByJobId(id);
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
    // The old track is gone either way (deleted or replaced) — any failure
    // notification tied to it, whichever kind, is moot now (#401).
    dismissFailuresByJobId(existingId);
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
      target = makeFolder({ id: "f-unsorted", name: i18nT("folder.unsorted") });
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
    videoStatus: state.video_status ?? fallbackTrack.videoStatus ?? null,
    createdAt: fallbackTrack.createdAt ?? state.created_at,
    favorite: fallbackTrack.favorite ?? false,
  };
}

function fmtExtracted(ts) {
  if (!ts) return "—";
  // Short form ("Aug 14, 11:43 AM") -- this now lives in the footer's single
  // compact meta line (#269 follow-up rebuild), which has no room for the
  // long month name and year the summary panel's date affords.
  return new Date(ts * 1000).toLocaleString("en-US", {
    month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

function deriveSource(sourceUrl) {
  if (!sourceUrl) return "—";
  if (sourceUrl.startsWith("local:")) return i18nT("track.localFile");
  if (sourceUrl.includes("youtube.com") || sourceUrl.includes("youtu.be")) return "YouTube";
  if (sourceUrl.includes("soundcloud.com")) return "SoundCloud";
  return i18nT("track.web");
}

function deriveQuality(sourceUrl) {
  if (!sourceUrl) return "—";
  if (sourceUrl.startsWith("local:")) {
    const ext = sourceUrl.split(".").pop()?.toLowerCase();
    if (ext === "wav") return i18nT("track.losslessWav");
    if (ext === "mp3") return i18nT("track.compressedMp3");
    return i18nT("track.localFile");
  }
  if (sourceUrl.includes("youtube.com") || sourceUrl.includes("youtu.be")) return i18nT("track.qualityHigh");
  if (sourceUrl.includes("soundcloud.com")) return i18nT("track.compressedMp3");
  return "—";
}

// Same buckets as job.js's dr.*/stability.* labels (duplicated here rather
// than imported: this file renders a saved track's stats after reload, job.js
// renders a job actively in progress -- same thresholds, different data path).
function drLabel(dr) {
  if (dr < 7) return i18nT("job.dr.compressed");
  if (dr < 10) return i18nT("job.dr.moderate");
  if (dr < 14) return i18nT("job.dr.high");
  return i18nT("job.dr.wide");
}

function stabilityLabel(pct) {
  if (pct >= 90) return i18nT("job.stability.veryStable");
  if (pct >= 70) return i18nT("job.stability.stable");
  if (pct >= 50) return i18nT("job.stability.moderate");
  return i18nT("job.stability.variable");
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
  titleEl.textContent = track.title || i18nT("track.untitled");
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
  if (summaryPeak) summaryPeak.textContent = track.peakDb != null ? i18nT("job.peakDb", { value: Number(track.peakDb).toFixed(1) }) : "";
  if (summaryDuration) summaryDuration.textContent = track.duration ? fmtTime(track.duration) : "—";

  const trackExtracted = document.getElementById("track-extracted");
  const trackSource = document.getElementById("track-source");
  const trackQuality = document.getElementById("track-quality");
  const favBtn = document.getElementById("fav-btn");
  // Static duration for the footer's compact meta line -- deliberately not
  // #t-time, which live-updates during playback and would duplicate the
  // Position group in the transport row below it.
  const metaDuration = document.getElementById("t-meta-duration");
  if (metaDuration) metaDuration.textContent = track.duration ? fmtTime(track.duration) : "—";
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
  // The user is done with this track — clear any failure tied to it (#401).
  // purgeTrash() does the same on permanent delete, for a job that errors
  // after being trashed but before it's purged.
  dismissFailuresByJobId(trackId);
  saveState();
  render();
}

function setCatalogView(view) {
  catalogView = ["trash", "favorites", "queue"].includes(view) ? view : "library";
  const app = document.querySelector(".app");
  if (catalogView !== "library") {
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

// A track's files went missing (folder deleted or moved outside the app).
// URL-sourced tracks can be rebuilt from the source, so trigger that
// directly rather than making the user hunt for a "resync" button in
// Settings. Local uploads have no source bytes left to rebuild from (#354's
// cleanup deletes the upload once the pipeline is done with it), so the
// only path back is a fresh re-upload.
function reimportUnavailableTrack(trackId, track) {
  updateTrackStatus(trackId, "unavailable");
  if (track.sourceUrl && !track.sourceUrl.startsWith("local:")) {
    importFromUrl(track.sourceUrl, { title: track.title, stems: track.selectedStems });
    return;
  }
  showError(i18nT("track.audioUnavailableError"));
}

async function loadTrackIntoStudio(trackId) {
  let track = tracks[trackId];
  if (!track) return;
  if (track.status === "unavailable") {
    reimportUnavailableTrack(trackId, track);
    return;
  }
  // The user has chosen to look at something else, so the running import gives
  // up the studio. It keeps running and keeps updating its own row; it just
  // stops repainting this view (and, at completion, replacing the audio that
  // is about to load here).
  if (trackId !== foregroundJobId) detachForegroundJob();
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
      if (state.status === "unavailable") {
        reimportUnavailableTrack(trackId, track);
        return;
      }
    } else if (res.status === 404) {
      track = { ...track, status: "unavailable" };
      tracks[trackId] = track;
      saveState();
      updateTrackStatus(trackId, "unavailable");
      showError(i18nT("track.audioUnavailableError"));
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
  wireUpAudio(trackId, track.audioStems, track.duration || 0, track.thumb, track.mixUrl ?? null, track.title || "", peaksPromise, track.hasVideo ?? false, track.videoStatus ?? null);
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

/** Put a whole playlist import in a folder of its own.
 *
 *  Placement happens before addTrackToLibrary, which only assigns a folder to a
 *  track that is not in one yet -- so claiming the ids first is what keeps these
 *  tracks out of Unsorted. Reuses an existing folder of the same name so
 *  re-importing a playlist tops it up instead of creating a duplicate.
 */
export function addPlaylistToLibrary(playlistTitle, jobs) {
  const name = String(playlistTitle || "Playlist").trim().slice(0, 80) || "Playlist";
  let folder = folders.find((f) => f.id !== TRASH_ID && !f.parentId && f.name === name);
  if (!folder) {
    folder = makeFolder({ name });
    folders.unshift(folder);
  }

  for (const job of jobs) {
    if (!folder.items.includes(job.job_id)) folder.items.push(job.job_id);
    addTrackToLibrary({
      id: job.job_id,
      title: job.title || job.source_url || i18nT("job.queuedTrack"),
      channel: i18nT("job.processing"),
      thumb: "",
      stems: [...selectedStems],
      selectedStems: [...selectedStems],
      audioStems: [],
      status: "queued",
      bpm: null,
      key: null,
      scale: null,
      keyConfidence: null,
      lufs: null,
      peakDb: null,
      sourceUrl: job.source_url,
    });
  }
  folder.collapsed = false;
  saveState();
  render();
  return folder.id;
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
    <form class="folder-editor" role="dialog" aria-modal="true" aria-label="Edit folder" data-i18n-aria-label="folderEditor.title">
      <div class="folder-editor-head">
        <span data-i18n="folderEditor.title">Edit folder</span>
        <button class="folder-editor-close" type="button" aria-label="Close" data-i18n-aria-label="folderEditor.closeAria">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M18 6 6 18M6 6l12 12"></path>
          </svg>
        </button>
      </div>
      <label class="folder-editor-field">
        <span data-i18n="folderEditor.nameLabel">Name</span>
        <input class="folder-editor-name" type="text" maxlength="100" autocomplete="off" spellcheck="false" />
      </label>
      <div class="folder-editor-field">
        <span data-i18n="folderEditor.colorLabel">Color</span>
        <div class="folder-editor-colors" role="group" aria-label="Folder color" data-i18n-aria-label="folderEditor.colorGroupAria">
          ${folderColorButtonsHtml(selectedColor)}
        </div>
      </div>
      <div class="folder-editor-msg" role="alert" aria-live="polite"></div>
      <div class="folder-editor-actions">
        <button class="folder-editor-cancel" type="button" data-i18n="folderEditor.cancel">Cancel</button>
        <button class="folder-editor-save" type="submit" data-i18n="folderEditor.save">Save</button>
      </div>
    </form>
  `;
  applyTranslations(overlay);

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
      msgEl.textContent = i18nT("folderEditor.emptyError");
      input.focus();
      return;
    }
    if (name.length > MAX_FOLDER_NAME_LEN) {
      msgEl.textContent = i18nT("folderEditor.tooLong", { max: MAX_FOLDER_NAME_LEN });
      input.focus();
      return;
    }
    if (!isValidFolderName(name)) {
      msgEl.textContent = i18nT("folderEditor.charsError");
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

// A URL-sourced track can be rebuilt by re-running the import; a local
// upload has no source bytes left to rebuild from, only a re-upload gets it
// back (see reimportUnavailableTrack).
function canReimportTrack(track) {
  return Boolean(track.sourceUrl) && !track.sourceUrl.startsWith("local:");
}

function unavailableWarningHtml(track) {
  const label = canReimportTrack(track)
    ? i18nT("track.unavailableReimport")
    : i18nT("track.unavailableReupload");
  return `<span class="cat-unavailable-warning">${esc(label)}</span>`;
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
  const sub = [duration, i18nPlural("footer.stemsCount", stemCount)].filter(Boolean).join(" · ");
  el.innerHTML = `
    <div class="cat-thumb">${thumbHtml(track)}</div>
    <div class="cat-meta">
      <div class="cat-title">${esc(displayTitle(track.title))}</div>
      <div class="cat-sub">${isUnavailable ? unavailableWarningHtml(track) : `<span>${esc(sub)}</span>`}</div>
    </div>
    <div class="cat-status${PROCESSING_STATUSES.has(track.status) ? " processing" : isUnavailable ? " unavailable" : ""}"></div>
  `;
  wireTrackDragAndLoad(el, trackId);
  return el;
}

// ─── Rendering ───

// A queued URL import has no title yet -- nothing has been downloaded, so the
// only thing to show is the URL the user pasted, which renders as a truncated
// unreadable string. Name the source instead until the real title arrives.
const _SOURCE_LABELS = [
  [/(^|\.)youtube\.com$|(^|\.)youtu\.be$|(^|\.)youtube-nocookie\.com$/, "YouTube"],
  [/(^|\.)soundcloud\.com$/, "SoundCloud"],
];

export function displayTitle(title) {
  const text = String(title ?? "").trim();
  if (!/^https?:\/\//i.test(text)) return text || i18nT("track.unknown");
  try {
    const host = new URL(text).hostname.replace(/^www\./, "");
    const match = _SOURCE_LABELS.find(([re]) => re.test(host));
    return `${match ? match[1] : host} link`;
  } catch {
    return text;
  }
}

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
  const subHtml = isUnavailable
    ? unavailableWarningHtml(track)
    : `<span>${esc(track.channel ?? "")}</span>
        <span class="dot">·</span>
        <span>${inTrash ? esc(i18nT("track.removed")) : esc(i18nPlural("footer.stemsCount", stemCount))}</span>`;
  el.innerHTML = `
    <div class="cat-thumb">${thumbHtml(track)}</div>
    <div class="cat-meta">
      <div class="cat-title">${esc(displayTitle(track.title))}</div>
      <div class="cat-sub">${subHtml}</div>
    </div>
    <div class="cat-status${PROCESSING_STATUSES.has(track.status) ? " processing" : isUnavailable ? " unavailable" : ""}"></div>
    ${inTrash ? "" : `<button class="cat-del" type="button" title="${esc(i18nT("track.moveToTrash"))}">
      <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <polyline points="3 6 5 6 21 6"></polyline>
        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path>
      </svg>
    </button>`}
  `;
  el.querySelector(".cat-del")?.setAttribute("aria-label", i18nT("track.moveTitleToTrash", { title: track.title ?? i18nT("track.unknown") }));

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
    ${isTrash ? "" : `<span class="f-grip" title="${esc(i18nT("folder.dragToReorder"))}">
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
      <button class="f-subfolder" type="button" aria-label="${esc(i18nT("folder.newSubfolder"))}" title="${esc(i18nT("folder.newSubfolder"))}">
        <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
          <path d="M12 11v6M9 14h6"/>
        </svg>
      </button>
      ${isUnsorted ? "" : `<button class="f-del" type="button" aria-label="${esc(i18nT("folder.deleteFolder"))}" title="${esc(i18nT("folder.deleteFolder"))}">
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
  const isQueueView = catalogView === "queue";
  const isLibraryView = !isTrashView && !isFavoritesView && !isQueueView;

  catalog?.classList.toggle("trash-view", isTrashView);
  catalog?.classList.toggle("favorites-view", isFavoritesView);
  catalog?.classList.toggle("queue-view", isQueueView);

  document.querySelector(".rail-library")?.classList.toggle("active", isLibraryView);
  document.querySelector(".rail-library")?.setAttribute("aria-pressed", String(isLibraryView));
  document.querySelector(".rail-favorites")?.classList.toggle("active", isFavoritesView);
  document.querySelector(".rail-favorites")?.setAttribute("aria-pressed", String(isFavoritesView));
  document.querySelector(".rail-trash")?.classList.toggle("active", isTrashView);
  document.querySelector(".rail-trash")?.setAttribute("aria-pressed", String(isTrashView));
  document.querySelector(".rail-queue")?.classList.toggle("active", isQueueView);
  document.querySelector(".rail-queue")?.setAttribute("aria-pressed", String(isQueueView));

  if (searchInput) {
    searchInput.placeholder = isTrashView
      ? i18nT("search.placeholderTrash")
      : isFavoritesView
        ? i18nT("search.placeholderFavorites")
        : i18nT("search.placeholderLibrary");
  }

  // ── Queue view ──
  // Rendered from the queue snapshot, not the library: it shows what the
  // backend is actually working on, in the order it will work on it.
  if (isQueueView) {
    renderQueueList(list);
    renderStrip(strip, folders.filter((f) => f.id !== TRASH_ID && !f.parentId));
    updateQueueBadge();
    return;
  }

  const nonTrash = folders.filter((f) => f.id !== TRASH_ID && !f.parentId);

  // ── Trash view ──
  if (isTrashView) {
    const visibleTrashItems = (trash?.items || []).filter((id) => trackMatchesSearch(tracks[id]));
    if (!trash?.items.length) {
      list.innerHTML = `<span class="folder-empty trash-empty">${esc(i18nT("trash.isEmptyState"))}</span>`;
    } else if (visibleTrashItems.length === 0) {
      list.innerHTML = `<span class="folder-empty trash-empty">${esc(i18nT("trash.noSearchMatch"))}</span>`;
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
      list.innerHTML = `<span class="folder-empty trash-empty">${esc(catalogSearchQuery ? i18nT("favorites.noSearchMatch") : i18nT("favorites.empty"))}</span>`;
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
    const section = makeSectionEl(i18nT("library.recent"));
    for (const id of recentIds) {
      const item = renderRecentItem(id);
      if (item) section.appendChild(item);
    }
    list.appendChild(section);
  }

  // Stem Collections section
  const collectionsSection = makeSectionEl(i18nT("library.stemCollections"));
  const newFolderBtn = document.createElement("button");
  newFolderBtn.id = "newFolderBtn";
  newFolderBtn.className = "new-folder-btn";
  newFolderBtn.type = "button";
  newFolderBtn.setAttribute("aria-label", i18nT("library.newFolder"));
  newFolderBtn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 11v6 M9 14h6"/></svg>${esc(i18nT("library.newFolder"))}`;
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
    const section = makeSectionEl(i18nT("library.tags"));
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
  updateQueueBadge();
  applyQueueDecorations();
}

// ─── Import queue decoration ───
//
// Rows are patched in place rather than re-rendered. The queue stream delivers
// a frame several times a second, and a full render() rebuilds the whole
// sidebar and re-runs every drag/click wiring -- at that rate it would fight
// the user for the DOM. render() calls this once at the end so a genuine
// rebuild picks the decoration back up.

function progressBarHtml() {
  return '<div class="cat-progress"><div class="cat-progress-fill"></div></div>';
}

function decorateRow(el, rowState) {
  const sub = el.querySelector(".cat-sub");
  if (!sub) return;

  if (!rowState) {
    // Left the queue (finished, failed or cancelled). render() will have
    // rebuilt the row from the library entry, so just drop the decoration.
    el.classList.remove("in-queue", "queue-waiting", "queue-running");
    el.querySelector(".cat-progress")?.remove();
    if (el.dataset.subRestore) {
      sub.innerHTML = el.dataset.subRestore;
      delete el.dataset.subRestore;
    }
    return;
  }

  const waiting = rowState.state === "waiting";
  el.classList.add("in-queue");
  el.classList.toggle("queue-waiting", waiting);
  el.classList.toggle("queue-running", !waiting);

  // Keep the original sub line so it can come back if this row is still on
  // screen when the job leaves the queue.
  if (!el.dataset.subRestore) el.dataset.subRestore = sub.innerHTML;
  const label = `<span class="cat-queue-label">${esc(rowState.label)}</span>`;
  if (sub.innerHTML !== label) sub.innerHTML = label;

  let bar = el.querySelector(".cat-progress");
  if (waiting) {
    bar?.remove();
    return;
  }
  if (!bar) {
    sub.insertAdjacentHTML("afterend", progressBarHtml());
    bar = el.querySelector(".cat-progress");
  }
  const fill = bar?.querySelector(".cat-progress-fill");
  if (fill) fill.style.width = `${Math.round(rowState.progress * 100)}%`;
}

/** A background import has finished (or failed, or was cancelled). It has no
 *  per-job stream, so fetch its final state once and complete its library entry
 *  -- stems, duration and analysis all land here, which is what makes the track
 *  playable from the sidebar without a page reload. */
async function completeSettledJob(jobId) {
  const existing = tracks[jobId];
  if (!existing) return; // not ours (or already deleted)
  try {
    const res = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
    if (!res.ok) {
      // 404 means the job is gone from the backend entirely.
      if (res.status === 404) updateTrackStatus(jobId, "unavailable");
      return;
    }
    const state = await res.json();
    if (state.status === "cancelled") {
      // Nothing was produced; drop the placeholder row rather than leaving a
      // track that can never be loaded.
      delete tracks[jobId];
      removeTrackFromFolders(jobId);
      saveState();
      render();
      return;
    }
    // A background job that failed used to say nothing at all: no banner (that
    // belongs to the foreground import), no queue UI, just a console warning
    // and a library row indistinguishable from a healthy one. Queue three
    // tracks, lose one, never find out. It gets a notification like any other
    // failure now.
    if (state.status === "error") {
      notifyFailure({
        kind: "import",
        message: state.error || i18nT("job.audioProcessingFailed"),
        detail: state.error_detail || null,
        context: {
          jobId,
          stage: state.stage,
          device: state.compute_device,
          gpuFallback: state.gpu_fallback,
          timings: state.stage_timings ? JSON.stringify(state.stage_timings) : null,
        },
      });
    }

    // Background jobs have no per-job stream (see the comment above), so this
    // is the only place a background import's on-demand vocal split (#275)
    // can be triggered -- runVocalSplitIfWanted no-ops if it wasn't requested.
    const finalState = state.status === "done" ? await runVocalSplitIfWanted(state) : state;
    const track = stateMetadataToTrack(finalState, { ...existing, id: jobId });
    track.id = jobId;
    track.channel = finalState.status === "done" ? i18nT("footer.extractedLabel") : existing.channel;
    addTrackToLibrary(track);
  } catch (e) {
    console.warn("[catalog] could not finish background job", jobId, e);
  }
}

// ─── Queue view ───

function queueEntries(snap) {
  const entries = [];
  if (snap.running) entries.push({ job: snap.running, running: true });
  for (const job of snap.queued ?? []) entries.push({ job, running: false });
  return entries;
}

function queueRowHtml({ job, running }, place, { paused = false } = {}) {
  const track = tracks[job.job_id];
  const label = running ? runningLabel(job) : paused ? i18nT("queue.pausedStatus") : i18nT("queue.positionInLine", { position: place });
  const thumb = track ? thumbHtml(track) : thumbHtml({ thumb: job.thumbnail });
  // The running job cannot be reordered -- it is already running. Only waiting
  // rows drag, and only they offer "play next".
  const handle = running
    ? ""
    : `<span class="queue-grip" title="Drag to reorder" aria-hidden="true">
         <svg viewBox="0 0 24 24" width="10" height="10" fill="currentColor">
           <circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/>
           <circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/>
           <circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/>
         </svg>
       </span>`;
  return `
    <div class="cat-item queue-row ${running ? "queue-running" : "queue-waiting"}" data-id="${esc(job.job_id)}"${running ? "" : ' draggable="true"'}>
      ${handle}
      <div class="cat-thumb">${thumb}</div>
      <div class="cat-meta">
        <div class="cat-title">${esc(displayTitle(job.title || track?.title || job.source_url))}</div>
        <div class="cat-sub"><span class="cat-queue-label">${esc(label)}</span></div>
        ${running ? '<div class="cat-progress"><div class="cat-progress-fill"></div></div>' : ""}
      </div>
      ${running || place <= 2 ? "" : `<button class="queue-top" type="button" title="${esc(i18nT("queue.extractNext"))}"
              aria-label="${esc(i18nT("queue.moveToFront", { title: displayTitle(job.title || job.source_url) }))}">
        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true">
          <path d="M12 19V5 M5 12l7-7 7 7"></path>
        </svg>
      </button>`}
      <button class="queue-cancel" type="button" title="${esc(i18nT("queue.cancelImport"))}"
              aria-label="${esc(i18nT("queue.cancelImportOf", { title: displayTitle(job.title || job.source_url) }))}">
        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true">
          <path d="M18 6 6 18 M6 6l12 12"></path>
        </svg>
      </button>
    </div>`;
}

// True while the user is dragging a queue row. Incoming queue frames arrive
// several times a second; re-rendering the list mid-drag would pull the row out
// from under the cursor and cancel the drag.
let _queueDragId = null;

export function isQueueDragging() {
  return _queueDragId !== null;
}

/** The id the dragged row should sit after, given where it was dropped.
 *  Null means the front of the queue. */
function dropAnchorId(listEl, draggedId, clientY) {
  const rows = [...listEl.querySelectorAll(".queue-row")].filter(
    (r) => r.dataset.id !== draggedId,
  );
  let anchor = null;
  for (const row of rows) {
    const box = row.getBoundingClientRect();
    if (clientY > box.top + box.height / 2) anchor = row.dataset.id;
  }
  return anchor;
}

function wireQueueRow(el, listEl) {
  el.querySelector(".queue-cancel")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    const btn = e.currentTarget;
    btn.disabled = true;
    await cancelQueuedJob(el.dataset.id);
  });

  el.querySelector(".queue-top")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    e.currentTarget.disabled = true;
    await reorderQueuedJob(el.dataset.id, null);
  });

  if (el.getAttribute("draggable") !== "true") return;

  el.addEventListener("dragstart", (e) => {
    _queueDragId = el.dataset.id;
    el.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    // Firefox refuses to start a drag without payload.
    e.dataTransfer.setData("text/plain", el.dataset.id);
  });

  el.addEventListener("dragend", () => {
    _queueDragId = null;
    el.classList.remove("dragging");
    for (const r of listEl.querySelectorAll(".queue-row")) r.classList.remove("drop-below");
  });
}

function wireQueueListDrop(listEl) {
  listEl.addEventListener("dragover", (e) => {
    if (!_queueDragId) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const anchor = dropAnchorId(listEl, _queueDragId, e.clientY);
    for (const r of listEl.querySelectorAll(".queue-row")) {
      r.classList.toggle("drop-below", !!anchor && r.dataset.id === anchor);
    }
  });

  listEl.addEventListener("drop", async (e) => {
    if (!_queueDragId) return;
    e.preventDefault();
    const dragged = _queueDragId;
    const anchor = dropAnchorId(listEl, dragged, e.clientY);
    _queueDragId = null;
    for (const r of listEl.querySelectorAll(".queue-row")) r.classList.remove("drop-below");
    if (anchor !== dragged) await reorderQueuedJob(dragged, anchor);
  });
}

function queuePausedBannerHtml(count) {
  return `
    <div class="queue-paused-banner">
      <div class="queue-paused-text">
        ${i18nPlural("queue.paused", count)}
      </div>
      <button class="queue-start-btn" type="button">${i18nT("queue.start")}</button>
    </div>`;
}

function renderQueueList(listEl, snap = getQueueSnapshot()) {
  const entries = queueEntries(snap);
  listEl.innerHTML = "";

  const section = document.createElement("div");
  section.className = "lib-section queue-section";
  section.innerHTML = `<div class="lib-section-head"><span>${esc(i18nT("queue.importQueue"))}</span></div>`;

  if (isPaused(snap)) {
    section.insertAdjacentHTML("beforeend", queuePausedBannerHtml(entries.length));
    section.querySelector(".queue-start-btn")?.addEventListener("click", async (e) => {
      e.currentTarget.disabled = true;
      e.currentTarget.textContent = i18nT("queue.starting");
      await startQueue();
    });
  }

  if (!entries.length) {
    section.insertAdjacentHTML(
      "beforeend",
      '<span class="folder-empty trash-empty">Nothing importing. Queued tracks appear here.</span>',
    );
    listEl.appendChild(section);
    return;
  }

  const paused = isPaused(snap);
  section.insertAdjacentHTML(
    "beforeend",
    entries.map((entry, i) => queueRowHtml(entry, i + 1, { paused })).join(""),
  );
  listEl.appendChild(section);
  for (const el of section.querySelectorAll(".queue-row")) wireQueueRow(el, listEl);
  wireQueueListDrop(listEl);
  updateQueueRows(snap);
}

/** Patch the open queue view in place. Only a change to which jobs are present
 *  (or their order) costs a rebuild; progress and stage text are written
 *  straight to the existing nodes, because this runs several times a second. */
function updateQueueRows(snap = getQueueSnapshot()) {
  const listEl = document.getElementById("catalogList");
  if (!listEl || catalogView !== "queue") return;
  // A frame landing mid-drag would rebuild the list and yank the row out
  // from under the cursor.
  if (isQueueDragging()) return;

  const entries = queueEntries(snap);
  const shown = [...listEl.querySelectorAll(".queue-row")].map((el) => el.dataset.id);
  const wanted = entries.map((e) => e.job.job_id);
  const bannerShown = !!listEl.querySelector(".queue-paused-banner");
  if (
    shown.length !== wanted.length ||
    shown.some((id, i) => id !== wanted[i]) ||
    bannerShown !== isPaused(snap)
  ) {
    renderQueueList(listEl, snap);
    return;
  }

  entries.forEach((entry, i) => {
    const el = listEl.querySelector(`.queue-row[data-id="${entry.job.job_id}"]`);
    if (!el) return;
    const label = entry.running
      ? runningLabel(entry.job)
      : isPaused(snap)
        ? i18nT("queue.pausedStatus")
        : i18nT("queue.positionInLine", { position: i + 1 });
    const labelEl = el.querySelector(".cat-queue-label");
    if (labelEl && labelEl.textContent !== label) labelEl.textContent = label;
    const fill = el.querySelector(".cat-progress-fill");
    if (fill) fill.style.width = `${Math.round((entry.job.progress || 0) * 100)}%`;
  });
}

/** The rail button only exists while there is something to look at, and carries
 *  the count so the queue is legible without opening it. */
function updateQueueBadge(snap = getQueueSnapshot()) {
  const btn = document.querySelector(".rail-queue");
  const badge = document.getElementById("queueBadge");
  if (!btn) return;
  const count = queueCount(snap);
  btn.classList.toggle("hidden", count === 0 && catalogView !== "queue");
  if (badge) {
    badge.textContent = count > 99 ? "99+" : String(count);
    badge.classList.toggle("hidden", count === 0);
  }
}

function onQueueFrame(snap) {
  updateQueueBadge(snap);
  if (catalogView !== "queue") {
    applyQueueDecorations(snap);
    return;
  }
  if (queueCount(snap) === 0) {
    // Nothing left to manage. Fall back to the library rather than leaving the
    // user in a view that can only ever be empty from here. Deliberately not
    // done inside updateQueueBadge, which render() calls -- that would recurse.
    setCatalogView("library");
    return;
  }
  updateQueueRows(snap);
}

function applyQueueDecorations(snap = getQueueSnapshot()) {
  const states = queueRowStates(snap);
  for (const el of document.querySelectorAll(".cat-item[data-id]")) {
    const state = states.get(el.dataset.id);
    // Only touch rows that are, or just were, in the queue.
    if (!state && !el.classList.contains("in-queue")) continue;
    decorateRow(el, state);
  }
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
  document.querySelector(".rail-queue")?.addEventListener("click", () => setCatalogView("queue"));
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
// The releases LIST, not /releases/latest. GitHub defines "latest" as the most
// recent NON-PRERELEASE release, so the moment a version ships with the
// pre-release box ticked it becomes invisible here and nobody is ever told an
// update exists. StemDeck has historically published even its alphas as normal
// releases, which is why that has not bitten yet -- this makes the check
// correct either way rather than dependent on remembering not to tick a box.
const RELEASES_API =
  "https://api.github.com/repos/stemdeckapp/stemdeck/releases?per_page=10";
const DISMISSED_UPDATE_KEY = "stemdeck.dismissed_update";

// The full GitHub release object from the last successful update check, used to
// populate the release dialog (notes + per-arch download link) on card click.
let latestRelease = null;
let cachedBuildTarget = null;

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

// Kept from the health check so a bug report can state the running version,
// model and ffmpeg status without a second round trip.
let healthInfo = {};

async function loadCurrentVersion() {
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    healthInfo = data;
    setDisplayedVersion(data.version);
  } catch (e) { console.warn("[catalog] version fetch failed:", e); }
}

/** Everything a bug report needs about this install. */
export async function collectDiagnostics() {
  return {
    version: currentVersion,
    model: healthInfo.demucs_model,
    ffmpegConfigured: healthInfo.ffmpeg_configured,
    buildTarget: await getBuildTarget(),
    isDesktop: Boolean(window.__TAURI__?.core?.invoke),
  };
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Inline markdown, applied AFTER escapeHtml so the markers (`* [ ] ( )`) are
// still literal characters and never HTML. Links are restricted to http(s) to
// block javascript:/data: URIs; label and URL are already entity-escaped.
function renderInlineMd(text) {
  return text
    .replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`,
    );
}

// Minimal, XSS-safe markdown subset for GitHub release bodies: headings, bullet
// lists, fenced code blocks, paragraphs, and the inline set above. Everything is
// HTML-escaped first; only a fixed whitelist of tags is ever emitted.
function renderReleaseNotes(markdown) {
  const lines = escapeHtml(markdown).split(/\r?\n/);
  const out = [];
  let inList = false;
  let inCode = false;
  let inQuote = false;
  const code = [];
  const closeList = () => {
    if (inList) {
      out.push("</ul>");
      inList = false;
    }
  };
  const closeQuote = () => {
    if (inQuote) {
      out.push("</blockquote>");
      inQuote = false;
    }
  };
  for (const raw of lines) {
    let line = raw.replace(/\s+$/, "");
    // Blockquote prefix — GitHub admonitions (macOS block) use `> [!IMPORTANT]`
    // and `> ...` lines, and can wrap a fenced code block. escapeHtml already
    // ran, so the marker is `&gt;`. Strip it unconditionally (so a blockquoted
    // closing fence is still recognised), but only open/close the <blockquote>
    // wrapper when not mid-fence, to avoid toggling it inside a code block.
    const quoted = /^&gt;\s?/.test(line);
    if (quoted) line = line.replace(/^&gt;\s?/, "");
    if (!inCode) {
      if (quoted && !inQuote) {
        closeList();
        out.push("<blockquote>");
        inQuote = true;
      } else if (!quoted && inQuote) {
        closeList();
        closeQuote();
      }
    }

    if (/^```/.test(line)) {
      if (inCode) {
        out.push(`<pre><code>${code.join("\n")}</code></pre>`);
        code.length = 0;
        inCode = false;
      } else {
        closeList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      code.push(line);
      continue;
    }
    let m;
    if ((m = /^\[!(\w+)\]/.exec(line))) {
      // GitHub admonition label ([!IMPORTANT] -> "Important").
      const label = m[1].charAt(0) + m[1].slice(1).toLowerCase();
      out.push(`<p class="release-callout">${label}</p>`);
    } else if ((m = /^#{1,6}\s+(.*)$/.exec(line))) {
      closeList();
      out.push(`<h4>${renderInlineMd(m[1])}</h4>`);
    } else if ((m = /^\s*[-*]\s+(.*)$/.exec(line))) {
      if (!inList) {
        out.push("<ul>");
        inList = true;
      }
      out.push(`<li>${renderInlineMd(m[1])}</li>`);
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList();
      out.push(`<p>${renderInlineMd(line)}</p>`);
    }
  }
  if (inCode) out.push(`<pre><code>${code.join("\n")}</code></pre>`);
  closeList();
  closeQuote();
  return out.join("\n");
}

// Resolve the running build's OS/arch/GPU variant. On desktop this is exact
// (Rust build_target); on web/server there is no reliable signal, so guess the
// OS from the user agent and leave the variant as CPU.
export async function getBuildTarget() {
  if (cachedBuildTarget) return cachedBuildTarget;
  const invoke = window.__TAURI__?.core?.invoke;
  if (invoke) {
    try {
      cachedBuildTarget = await invoke("build_target");
      return cachedBuildTarget;
    } catch (e) {
      console.warn("[catalog] build_target failed:", e);
    }
  }
  const ua = navigator.userAgent || "";
  let os = "linux";
  if (/Mac/i.test(ua)) os = "macos";
  else if (/Win/i.test(ua)) os = "windows";
  const arch = /arm64|aarch64/i.test(ua) ? "arm64" : "x64";
  cachedBuildTarget = { os, arch, gpu: "cpu" };
  return cachedBuildTarget;
}

// Expected release-asset filename for a build target. Names are deterministic
// from the release workflows: macOS keys on arch; Windows/Linux add a .NVIDIA
// infix for the CUDA variant.
function assetNameFor(target) {
  if (target.os === "macos") {
    return `StemDeck-macOS-${target.arch === "arm64" ? "arm64" : "x64"}.dmg`;
  }
  const variant = target.gpu === "nvidia" ? ".NVIDIA" : "";
  if (target.os === "windows") return `StemDeck-Windows-x64${variant}.zip`;
  return `StemDeck-Linux-x64${variant}.tar.gz`;
}

function pickReleaseAsset(release, target) {
  const name = assetNameFor(target);
  const asset = (release.assets || []).find((a) => a.name === name);
  return asset ? { url: asset.browser_download_url, name } : null;
}

// ─── In-app updater ───
// Downloads and applies an update without leaving the app, instead of sending
// the user to a browser download -- see download_app_update/apply_app_update in
// desktop/src-tauri/src/main.rs for the file swap.
//
// Windows and Linux only. Both ship a flat directory with the executable,
// backend/ and python/ side by side, which is the shape the swap needs. macOS
// resolves its backend inside the downloaded runtime pack rather than the .app,
// so its app layer is a different thing entirely and is handled separately.
//
// The updater replaces the executable and backend/ ONLY. It never touches
// python/, because an NVIDIA install rewrites that directory with CUDA torch on
// first run and replacing it would silently drop the machine back to CPU. So an
// in-app update is only safe when the release needs the same Python
// dependencies the install already has -- that is what the runtime id gates.
// When it doesn't match, we fall back to the normal full-package download.

function findReleaseAsset(release, name) {
  return (release.assets || []).find((a) => a.name === name) || null;
}

// Asset names the packaging scripts publish for each in-place-updatable
// platform. The archive format differs because each script already produces
// one: Compress-Archive on Windows, tar on Linux (which also preserves the
// executable bit the relaunch depends on).
function updaterAssetNames(target) {
  if (target.os === "windows") {
    return { app: "StemDeck-Windows-x64-app.zip", runtimeId: "StemDeck-Windows-x64-runtime-version.json" };
  }
  if (target.os === "linux") {
    return { app: "StemDeck-Linux-x64-app.tar.gz", runtimeId: "StemDeck-Linux-x64-runtime-version.json" };
  }
  return null;
}

// Resolves the app-layer asset for the in-app updater, or null when this
// release cannot be applied in place -- it predates the updater assets, is
// missing one, or changed the Python dependency set. Null means "use the full
// download link", which is always correct, just less convenient.
//
// The checksum and runtime-id files are read by Rust (`check_app_update`), not
// fetched here. This page is served by the Python backend, so its CSP applies,
// and connect-src allows api.github.com but NOT the github.com /
// objects.githubusercontent.com hosts that serve release *assets*. Fetching
// them from JS is blocked outright; Rust's HTTP client is not bound by the page
// CSP, so the policy stays as tight as it is today.
async function resolveInAppUpdatePlan(release, target) {
  const names = updaterAssetNames(target);
  if (!names) return null;
  const appAsset = findReleaseAsset(release, names.app);
  const appShaAsset = findReleaseAsset(release, `${names.app}.sha256`);
  const runtimeIdAsset = findReleaseAsset(release, names.runtimeId);
  if (!appAsset || !appShaAsset || !runtimeIdAsset) return null;

  const check = await window.__TAURI__.core.invoke("check_app_update", {
    query: {
      appShaUrl: appShaAsset.browser_download_url,
      runtimeIdUrl: runtimeIdAsset.browser_download_url,
    },
  });
  if (!check?.supported) {
    console.info("[catalog] in-app update unavailable:", check?.reason || "unknown");
    return null;
  }

  return { appUrl: appAsset.browser_download_url, appSha256: check.appSha256 };
}

function showInappError(message) {
  const errorEl = document.getElementById("releaseInappError");
  if (!errorEl) return;
  errorEl.textContent = `${i18nT("release.updateFailed")}: ${message}`;
  errorEl.classList.remove("hidden");
}

// Wires the download/apply buttons for a resolved plan. Returns false (and
// touches nothing) when this release has no in-app-updatable assets, so the
// caller can fall back to the plain download link.
async function wireInAppUpdate(target) {
  const downloadBtn = document.getElementById("releaseDownloadApp");
  const applyBtn = document.getElementById("releaseApplyUpdate");
  const inapp = document.getElementById("releaseInapp");
  const progress = document.getElementById("releaseInappProgress");
  const progressText = document.getElementById("releaseInappProgressText");
  const errorEl = document.getElementById("releaseInappError");
  if (!downloadBtn || !applyBtn || !latestRelease) return false;

  const plan = await resolveInAppUpdatePlan(latestRelease, target);
  if (!plan) return false;

  // The manual download stays visible alongside the auto-update pill: some
  // people would rather grab the zip, and it is the escape hatch if an in-app
  // update fails.
  inapp?.classList.remove("hidden");
  progress?.classList.add("hidden");
  errorEl?.classList.add("hidden");
  downloadBtn.disabled = false;
  applyBtn.disabled = false;
  downloadBtn.classList.remove("hidden");
  applyBtn.classList.add("hidden");

  // Indeterminate, not a byte-accurate bar. Real progress would mean listening
  // to the Rust download event, and this page is served over http by the Python
  // backend -- a remote origin, which the Tauri capability in
  // desktop/src-tauri/capabilities/default.json does not cover, so
  // `plugin:event|listen` is refused by the ACL. Granting a remote origin event
  // permissions would widen exactly the IPC surface #171 locked down, and the
  // app layer is ~5 MB. Not worth it. (App-defined commands like the invokes
  // below are not ACL-gated, which is why those work.)
  downloadBtn.onclick = async () => {
    errorEl?.classList.add("hidden");
    downloadBtn.disabled = true;
    if (progressText) progressText.textContent = i18nT("release.downloading");
    progress?.classList.remove("hidden");
    progress?.classList.add("indeterminate");
    try {
      await window.__TAURI__.core.invoke("download_app_update", { plan });
      progress?.classList.add("hidden");
      downloadBtn.classList.add("hidden");
      applyBtn.classList.remove("hidden");
    } catch (e) {
      console.warn("[catalog] download_app_update failed:", e);
      showInappError(String(e?.message || e));
      downloadBtn.disabled = false;
      progress?.classList.add("hidden");
    }
  };

  applyBtn.onclick = async () => {
    errorEl?.classList.add("hidden");
    applyBtn.disabled = true;
    if (progressText) progressText.textContent = i18nT("release.applying");
    progress?.classList.remove("hidden");
    try {
      // On success the app exits and relaunches -- this promise never resolves.
      await window.__TAURI__.core.invoke("apply_app_update");
    } catch (e) {
      console.warn("[catalog] apply_app_update failed:", e);
      showInappError(String(e?.message || e));
      applyBtn.disabled = false;
      progress?.classList.add("hidden");
    }
  };

  return true;
}

async function openReleaseDialog() {
  const dialog = document.getElementById("releaseDialog");
  if (!dialog || !latestRelease) return;
  const version = document.getElementById("releaseVersion");
  const notes = document.getElementById("releaseNotes");
  const download = document.getElementById("releaseDownload");
  const docker = document.getElementById("releaseDocker");
  const dockerCmd = document.getElementById("releaseDockerCmd");

  if (version) version.textContent = `v${normalizeVersion(latestRelease.tag_name)}`;
  if (notes) {
    const body = (latestRelease.body || "").trim();
    notes.innerHTML = body
      ? renderReleaseNotes(body)
      : `<p>No release notes provided. See the full release on GitHub.</p>`;
  }

  // Server/Docker mode has no Tauri: updating is an image pull, not a file
  // download, and the client browser's OS/arch is irrelevant to the container.
  // Show the docker pull command instead of a (meaningless) desktop download.
  const serverMode = !window.__TAURI__?.core?.invoke;
  if (serverMode) {
    const tag = normalizeVersion(latestRelease.tag_name);
    if (dockerCmd) dockerCmd.textContent = `docker pull ghcr.io/stemdeckapp/stemdeck:${tag}`;
    docker?.classList.remove("hidden");
    download?.classList.add("hidden");
  } else if (download) {
    docker?.classList.add("hidden");
    const target = await getBuildTarget();

    // The manual download is always offered. On Windows, when the release can
    // be applied in place, an "Update now" pill appears beside it -- additive,
    // never a replacement, so the zip stays one click away either way.
    const picked = pickReleaseAsset(latestRelease, target);
    if (picked) {
      download.href = picked.url;
      download.textContent = i18nT("release.download");
    } else {
      // No matching asset (e.g. an arch we don't build): fall back to the
      // release page so the user can pick manually.
      download.href = latestRelease.html_url || RELEASES_URL;
      download.textContent = i18nT("release.viewDownload");
    }
    download.classList.remove("hidden");

    let usedInapp = false;
    if (updaterAssetNames(target)) {
      try {
        usedInapp = await wireInAppUpdate(target);
      } catch (e) {
        console.warn("[catalog] in-app update setup failed, falling back to link:", e);
      }
    }
    if (!usedInapp) {
      document.getElementById("releaseInapp")?.classList.add("hidden");
      document.getElementById("releaseDownloadApp")?.classList.add("hidden");
      document.getElementById("releaseApplyUpdate")?.classList.add("hidden");
    }
  }

  dialog.classList.remove("hidden");
}

function wireReleaseDialog() {
  const dialog = document.getElementById("releaseDialog");
  const close = document.getElementById("releaseClose");
  if (!dialog) return;
  const hide = () => dialog.classList.add("hidden");
  close?.addEventListener("click", hide);
  dialog.addEventListener("mousedown", (e) => {
    if (e.target === dialog) hide();
  });
  dialog.addEventListener("keydown", (e) => {
    if (e.code === "Escape") hide();
  });
}

async function checkForUpdate() {
  try {
    const res = await fetch(RELEASES_API, { headers: { Accept: "application/vnd.github+json" } });
    if (!res.ok) return;
    // The check itself succeeded, regardless of what it finds below — clear
    // any stale "update check failed" card (#401).
    dismissFailuresByKind("update");
    // Newest first, as GitHub returns them. Drafts are invisible to an
    // unauthenticated request anyway, but filter them so a maintainer running a
    // dev build is not offered a release that has no assets yet.
    //
    // Pre-releases are skipped as well: a release is published as a pre-release
    // first, verified, and only then promoted ("Set as the latest release"), so
    // nobody is offered a build that has not been through that. The list
    // endpoint is used rather than /releases/latest because it keeps the choice
    // here, in code, rather than in GitHub's endpoint semantics. It assumes a
    // stable release inside the last 10 -- true unless ten consecutive
    // pre-releases go out without one being promoted.
    const releases = await res.json();
    const data = Array.isArray(releases)
      ? releases.find((r) => !r.draft && !r.prerelease)
      : null;
    if (!data) return;
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

    // Keep the full release so the dialog can render notes + pick the download.
    latestRelease = data;

    const card = document.getElementById("notifReleaseCard");
    const desc = document.getElementById("notifReleaseDesc");
    const dismissBtn = document.getElementById("notifReleaseDismiss");

    if (desc) desc.textContent = `v${latest}`;
    card?.classList.remove("hidden");
    // The badge and empty state are shared with failure cards now, so they are
    // decided in one place from the full set rather than toggled from here.
    setReleasePending(true);

    // Clicking the card (anywhere but the dismiss button) opens the release dialog.
    card?.addEventListener("click", (e) => {
      if (e.target.closest("#notifReleaseDismiss")) return;
      openReleaseDialog();
    });

    dismissBtn?.addEventListener("click", (e) => {
      e.stopPropagation();
      try { localStorage.setItem(DISMISSED_UPDATE_KEY, latest); } catch (e) { console.warn(e); }
      card?.classList.add("hidden");
      setReleasePending(false);
    }, { once: true });
  } catch (e) {
    console.warn("[catalog] update check failed:", e);
    // Only report a genuine failure, not "we are offline": an update check that
    // cannot reach GitHub is not a StemDeck bug and must not file one.
    if (!(e instanceof TypeError)) {
      notifyFailure({
        kind: "update",
        message: i18nT("update.checkFailed"),
        detail: String(e?.message || e),
      });
    }
  }
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

// Ground truth for "done" <-> "unavailable" is the server (it checks the
// stems folder on disk), catching a job whose registry entry survived but
// whose files did not - something mere presence in `jobs` cannot tell apart
// from a healthy one. Shared by the passive startup sync and the
// user-triggered "Resync" button so both apply the same rule.
function reconcileAvailability(jobs) {
  const serverIds = new Set(jobs.map((j) => j.job_id));
  const serverStatus = new Map(jobs.map((j) => [j.job_id, j.status]));
  const trashIds = new Set(getTrashFolder()?.items || []);
  for (const [id, t] of Object.entries(tracks)) {
    if (trashIds.has(id)) continue;
    if (t.status !== "done" && t.status !== "unavailable") continue;
    if (serverIds.has(id)) t.status = serverStatus.get(id) === "unavailable" ? "unavailable" : "done";
    else if (t.status === "done") t.status = "unavailable"; // gone from the registry entirely
  }
  saveState();
  render();
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
    reconcileAvailability(jobs);
  } catch (e) { console.warn("[catalog] failed to load jobs from backend:", e); }
}

// ─── Settings menu + Library editor ───

let libraryEditor = null;
let libraryEditorOnKey = null;

// The settings modal has a lot of server-fetched, dynamically-computed text
// (resolved device suffix, out-of-sync status, stems-location message, ...)
// that a generic data-i18n re-apply pass can't safely re-derive after a
// language switch. Simplest robust fix: if the modal is open when the
// language changes, just rebuild it from scratch -- openLibraryEditor()
// already re-fetches everything fresh and is safe to call while already open
// (it closes any existing instance first).
onLanguageChange(() => {
  if (libraryEditor) openLibraryEditor();
});

// Library list rows bake their subtitle/placeholder/empty-state text into
// plain innerHTML at render time (no data-i18n hooks to re-resolve), so a
// generic applyTranslations() pass can't fix an already-rendered list --
// rebuild it explicitly on language switch (same reasoning as the editor
// listener above).
onLanguageChange(() => render());

// Human-readable "Location" for a track: the imported filename for local
// uploads, otherwise the source URL.
function libraryLocation(sourceUrl) {
  if (!sourceUrl) return "—";
  if (sourceUrl.startsWith("local:")) return sourceUrl.slice(6) || i18nT("library.importedFile");
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
    ? i18nPlural("settings.outOfSync.summary", n)
    : i18nT("settings.outOfSync.allSynced");
}

function closeLibraryEditor() {
  if (libraryEditorOnKey) {
    document.removeEventListener("keydown", libraryEditorOnKey);
    libraryEditorOnKey = null;
  }
  closeResetConfirm();
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
    td.textContent = i18nT("settings.stemsLocation.inSync");
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
      badge.textContent = i18nT("status.unavailable");
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
          <div class="settings-row-title" data-i18n="settings.network.allowTitle">Make StemDeck available on your network</div>
          <div class="settings-row-desc" data-i18n="settings.network.allowDesc">Let other devices (like your phone) open StemDeck at the address below.</div>
          <div class="settings-row-desc settings-lock-note" data-i18n="settings.network.lockNote">Read-only when StemDeck is started in server mode — network access is then set by your server configuration.</div>
        </div>
        <label class="settings-switch">
          <input type="checkbox" class="net-access-input" />
          <span class="settings-switch-track"><span class="settings-switch-thumb"></span></span>
        </label>
      </div>
      <div class="settings-net hidden">
        <div class="settings-net-qr"></div>
      </div>
    </div>
  `;
}

/** Long paths are truncated from the LEFT: the folder name is what the user
 *  needs to see, and the leading /Users/... is the part they already know.
 *  Done here rather than with CSS -- the direction:rtl trick that gives a
 *  leading ellipsis also moves the path's leading slash to the far end, so
 *  /private/tmp/x renders as tmp/x/ and reads like a different path. */
export function shortenPath(path, max = 52) {
  const text = String(path ?? "");
  if (text.length <= max) return text;
  return "…" + text.slice(text.length - (max - 1));
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

// Where extracted stems live (#354). Documents is a fine default until you
// notice it is syncing tens of gigabytes to iCloud.
async function wireStemsLocation(overlay) {
  const pathEl = overlay.querySelector(".stems-location-path");
  const sizeEl = overlay.querySelector(".stems-location-size");
  const btn = overlay.querySelector(".set-stems-location");
  const msg = overlay.querySelector(".stems-location-msg");
  if (!pathEl || !btn) return;

  const hideRow = () =>
    overlay.querySelector(".stems-location")?.closest(".settings-row")?.remove();

  let current = null;

  const setMessage = (text, kind = "") => {
    if (!msg) return;
    msg.textContent = text || "";
    msg.className = `stems-location-msg${kind ? " " + kind : ""}`;
  };

  const apply = (d) => {
    current = d.path;
    pathEl.textContent = shortenPath(d.path);
    pathEl.title = d.path;
    if (sizeEl) sizeEl.textContent = formatSize(d.bytes);
    // Reopening Settings after a move, before the restart, should still say so.
    if (d.restart_required) setMessage(i18nT("settings.stemsLocation.restartNote"), "ok");
  };

  // The backend decides whether this setting exists at all -- it is false on a
  // server, Docker or Unraid deployment, where storage comes from a mounted
  // volume the operator chose and moving it from inside the app would fight the
  // mount. Asking it, rather than sniffing for Tauri, keeps that judgement in
  // one place and means the row is testable in a browser against a desktop
  // backend.
  try {
    const r = await fetch("/api/settings/stems-location", { cache: "no-store" });
    if (!r.ok) {
      hideRow();
      return;
    }
    const data = await r.json();
    if (!data.editable) {
      hideRow();
      return;
    }
    apply(data);
  } catch (e) {
    console.warn("[settings] could not read the stems location:", e);
    hideRow();
    return;
  }

  btn.addEventListener("click", async () => {
    let picked = null;
    const invoke = window.__TAURI__?.core?.invoke;
    if (invoke) {
      try {
        picked = await invoke("pick_stems_folder");
      } catch (e) {
        console.warn("[settings] folder picker failed:", e);
        setMessage(i18nT("settings.stemsLocation.pickerFailed"), "error");
        return;
      }
    } else {
      // No native picker outside the desktop shell. Only reachable when a
      // desktop-mode backend is being driven from a browser, which is a
      // development setup -- in the shipped app invoke is always there.
      picked = window.prompt("Full path to the folder for extracted stems:", current || "");
    }
    if (!picked) return; // cancelled

    btn.disabled = true;
    setMessage(i18nT("settings.stemsLocation.moving"));
    try {
      const r = await fetch("/api/settings/stems-location", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: picked }),
      });
      const data = await r.json();
      if (!r.ok) {
        setMessage(data.detail || i18nT("settings.stemsLocation.moveFailed"), "error");
        return;
      }
      // Re-read rather than trust the POST: the GET reports where the stems
      // actually are now, including the size at the new location.
      try {
        const again = await fetch("/api/settings/stems-location", { cache: "no-store" });
        if (again.ok) apply(await again.json());
        else apply({ path: data.path, bytes: 0 });
      } catch (e) {
        console.warn("[settings] refresh failed:", e);
        apply({ path: data.path, bytes: 0 });
      }
      if (data.persisted === false) {
        // The physical move genuinely succeeded (moved_entries is real) --
        // but settings.json didn't take the new path, so a restart right now
        // would read the OLD default back while the library sits at the new
        // folder (#403). Say so plainly rather than the usual "ok" message.
        setMessage(i18nPlural("settings.stemsLocation.movedPersistFailed", data.moved_entries), "error");
      } else {
        setMessage(i18nPlural("settings.stemsLocation.movedOk", data.moved_entries), "ok");
      }
    } catch (e) {
      console.warn("[settings] move failed:", e);
      setMessage(i18nT("settings.stemsLocation.serverUnreachable"), "error");
    } finally {
      btn.disabled = false;
    }
  });
}

// Language picker: purely a client-side/cosmetic preference (no server
// behavior depends on it), so it's read/written via setLanguage() (i18n.js,
// storeGet/storeSet under "stemdeck.language") rather than the /api/settings
// round trip the rest of this modal uses -- see i18n.js's module comment for
// why that split matches the app's existing convention.
function wireLanguageSetting(overlay) {
  const sel = overlay.querySelector(".set-language");
  if (!sel) return;
  sel.innerHTML = LANGUAGES.map(
    (l) => `<option value="${l.code}">${esc(l.flag)} ${esc(l.name)}</option>`,
  ).join("");
  sel.value = getLanguage();
  sel.addEventListener("change", () => setLanguage(sel.value));
}

// General settings: max track length (minutes), playlist import limit, and
// MP4 video quality. Read live
// and POSTed on change to /api/settings (same runtime store as the toggle).
async function wireGeneralSettings(overlay) {
  const durInput = overlay.querySelector(".set-max-duration");
  const durDesc = overlay.querySelector(".set-max-duration-desc");
  const playlistInput = overlay.querySelector(".set-playlist-max");
  const heightSel = overlay.querySelector(".set-video-height");
  const sampleRateSel = overlay.querySelector(".set-export-samplerate");
  const portInput = overlay.querySelector(".set-port");
  const deviceSel = overlay.querySelector(".set-demucs-device");
  const deviceDesc = overlay.querySelector(".set-demucs-desc");
  const qualitySel = overlay.querySelector(".set-separation-quality");
  const cookiesInput = overlay.querySelector(".set-cookies-file");
  const cookiesMsg = overlay.querySelector(".cookies-file-msg");
  const autoDeleteInput = overlay.querySelector(".auto-delete-input");
  const autoDeleteDaysRow = overlay.querySelector(".auto-delete-days-row");
  const autoDeleteDays = overlay.querySelector(".set-auto-delete-days");
  const autoDeleteDaysDesc = overlay.querySelector(".auto-delete-days-desc");
  if (!durInput && !playlistInput && !heightSel && !sampleRateSel && !portInput && !deviceSel && !qualitySel && !cookiesInput && !autoDeleteInput) return;

  // Last server-confirmed device choice, to revert the select when the server
  // rejects a forced device (e.g. CUDA not available on this machine).
  let lastDevice = "auto";

  // "Delete after" only means anything while automatic deletion is on. Dim it
  // and take it out of the tab order rather than removing it, so the number is
  // still readable: deciding whether to switch deletion on is easier when you
  // can already see how long tracks would be kept.
  const setDaysEnabled = (on) => {
    autoDeleteDaysRow?.classList.toggle("disabled", !on);
    if (autoDeleteDays) autoDeleteDays.disabled = !on;
  };

  const apply = (d) => {
    if (durInput && d.max_duration_sec) durInput.value = String(Math.round(d.max_duration_sec / 60));
    // Same reason: the copy in the description text went stale alongside the
    // clamp, telling the user "max 20" for a limit that was really 60.
    if (durDesc && d.max_duration_max_sec) {
      durDesc.textContent = i18nT("settings.maxDuration.desc", {
        max: Math.round(d.max_duration_max_sec / 60),
      });
    }
    if (playlistInput && d.playlist_max_items) playlistInput.value = String(d.playlist_max_items);
    if (heightSel && d.video_max_height) heightSel.value = String(d.video_max_height);
    if (sampleRateSel && d.export_sample_rate) sampleRateSel.value = String(d.export_sample_rate);
    if (portInput && d.port) portInput.value = String(d.port);
    if (qualitySel && d.separation_quality) qualitySel.value = d.separation_quality;
    // Unset is the normal case, so read the key rather than truthiness --
    // clearing the field must survive the round trip and not be repopulated.
    if (cookiesInput && "cookies_file" in d) cookiesInput.value = d.cookies_file || "";
    // Read the key, not truthiness: false is the normal value here and the
    // whole point of the setting, so `d.auto_delete_jobs &&` would leave the
    // switch showing whatever it showed last.
    if (autoDeleteInput && "auto_delete_jobs" in d) {
      autoDeleteInput.checked = d.auto_delete_jobs === true;
      setDaysEnabled(autoDeleteInput.checked);
    }
    // Never overwrite a field the user is currently in. Flipping the switch
    // POSTs, and that response used to land on top of whatever they had just
    // started typing into the field the switch had only just enabled. The
    // days handler below writes its own result back explicitly, so the
    // server still owns the ceiling.
    if (autoDeleteDays && d.auto_delete_days && document.activeElement !== autoDeleteDays) {
      autoDeleteDays.value = String(d.auto_delete_days);
    }
    if (autoDeleteDaysDesc && d.auto_delete_days_max) {
      autoDeleteDaysDesc.textContent = i18nT("settings.autoDelete.daysDesc", {
        max: d.auto_delete_days_max,
      });
    }
    if (deviceSel) {
      // Gray out devices this machine can't use (Auto and CPU are always
      // available). Label disabled options so it's clear WHY they're greyed.
      // The base label is captured into a data attribute the first time this
      // runs (before any "not available" suffix is ever appended), rather
      // than stripped back out of a previous textContent each call -- a
      // regex keyed to the English suffix would silently stop matching once
      // that suffix is translated, leaving the untranslated suffix appended
      // forever on every subsequent apply().
      const avail = new Set(d.demucs_devices_available || []);
      for (const opt of deviceSel.options) {
        if (opt.dataset.baseLabel === undefined) opt.dataset.baseLabel = opt.textContent;
        const base = opt.dataset.baseLabel;
        const ok = opt.value === "auto" || avail.has(opt.value);
        opt.disabled = !ok;
        opt.textContent = ok ? base : `${base}${i18nT("settings.device.notAvailable")}`;
      }
      if (d.demucs_device) {
        deviceSel.value = d.demucs_device;
        lastDevice = d.demucs_device;
      }
    }
    if (deviceDesc) {
      const resolved = d.demucs_device_resolved ? i18nT("settings.device.currently", { device: d.demucs_device_resolved }) : "";
      deviceDesc.textContent = i18nT("settings.device.desc", { resolved });
    }
  };

  // Keep the text inputs digit-only as the user types (maxlength caps the rest).
  const digitsOnly = (input) => input?.addEventListener("input", () => {
    const cleaned = input.value.replace(/\D/g, "");
    if (cleaned !== input.value) input.value = cleaned;
  });
  digitsOnly(durInput);
  digitsOnly(playlistInput);
  digitsOnly(portInput);
  digitsOnly(autoDeleteDays);

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
      if (r.ok) {
        const data = await r.json();
        apply(data); // reflect the server's clamped value
        return data;
      }
    } catch { /* ignore */ }
    return null;
  };

  durInput?.addEventListener("change", () => {
    // No ceiling of its own. This used to clamp to 20 while the backend
    // allowed 60, so typing 60 silently posted 20 and the field snapped back
    // with no explanation. The server clamps and returns the value it kept,
    // and apply() writes that back, so it stays the single authority.
    const mins = Math.max(1, parseInt(durInput.value, 10) || 20);
    post({ max_duration_sec: mins * 60 });
  });
  playlistInput?.addEventListener("change", () => {
    const items = Math.max(1, Math.min(200, parseInt(playlistInput.value, 10) || 50));
    post({ playlist_max_items: items });
  });
  // Not routed through post(): that helper drops a non-ok response silently,
  // which is exactly the wrong behaviour for a path the user typed. A bad path
  // has to say so, or the user retypes it and never learns why nothing
  // happened.
  cookiesInput?.addEventListener("change", async () => {
    if (cookiesMsg) cookiesMsg.textContent = "";
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cookies_file: cookiesInput.value.trim() }),
      });
      if (r.ok) {
        apply(await r.json());
      } else if (cookiesMsg) {
        // The server's detail is an English string; show the translated key
        // instead so this reads correctly in every locale.
        cookiesMsg.textContent = i18nT("settings.cookies.invalid");
      }
    } catch { /* offline: leave the field as typed */ }
  });
  heightSel?.addEventListener("change", () => {
    post({ video_max_height: parseInt(heightSel.value, 10) });
  });
  sampleRateSel?.addEventListener("change", () => {
    post({ export_sample_rate: parseInt(sampleRateSel.value, 10) });
  });
  portInput?.addEventListener("change", () => {
    const port = Math.max(1024, Math.min(65535, parseInt(portInput.value, 10) || 8000));
    post({ port });
  });
  qualitySel?.addEventListener("change", () => {
    post({ separation_quality: qualitySel.value });
  });
  autoDeleteInput?.addEventListener("change", () => {
    // Enable the days field immediately rather than waiting for the round
    // trip, so the switch does not appear to do nothing on a slow response.
    // apply() sets it again from the server's answer either way.
    setDaysEnabled(autoDeleteInput.checked);
    post({ auto_delete_jobs: autoDeleteInput.checked });
  });
  autoDeleteDays?.addEventListener("change", async () => {
    // Floor of 1 only. The server owns the ceiling and returns what it kept,
    // the same arrangement as max track length, so the two cannot drift.
    const days = Math.max(1, parseInt(autoDeleteDays.value, 10) || 30);
    const data = await post({ auto_delete_days: days });
    // Written back here rather than left to apply(), which skips a focused
    // field: committing with Enter keeps focus, and the user still has to see
    // the number the server actually kept.
    if (data?.auto_delete_days) autoDeleteDays.value = String(data.auto_delete_days);
  });
  // Compute device needs its own POST path: unlike the clamped numeric
  // settings, the server can REJECT a forced device (422 with a reason, e.g.
  // "cuda is not available on this machine") -- surface that and revert.
  deviceSel?.addEventListener("change", async () => {
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ demucs_device: deviceSel.value }),
      });
      if (r.ok) {
        apply(await r.json());
        return;
      }
      let detail = i18nT("settings.device.changeFailed");
      try {
        detail = (await r.json()).detail || detail;
      } catch (err) {
        console.warn("settings error body parse failed:", err);
      }
      showError(detail);
      deviceSel.value = lastDevice;
    } catch (err) {
      console.warn("compute device update failed:", err);
      deviceSel.value = lastDevice;
    }
  });
}

async function wireNetworkSetting(overlay) {
  const input = overlay.querySelector(".net-access-input");
  const netWrap = overlay.querySelector(".settings-net");
  const qrWrap = overlay.querySelector(".settings-net-qr");
  if (!input) return;

  // Server mode (no Tauri shell): network availability is governed by the server
  // deployment, not this toggle. A headless server exists to be reached over the
  // network, so present the switch as on and read-only (the "read-only in server
  // mode" note explains how to change it via server config).
  const serverMode = !window.__TAURI__?.core?.invoke;

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
  if (serverMode) enabled = true;

  // QR codes: one per LAN address, each encodes the /mobile/ URL so the
  // phone camera opens StemDeck directly. Cards start blurred so an open
  // camera app on a nearby device doesn't scan them before you're ready.
  if (qrWrap) {
    qrWrap.textContent = "";
    if (addresses.length) {
      const hint = document.createElement("p");
      hint.className = "qr-hint";
      hint.textContent = i18nT("settings.network.qrHint");
      qrWrap.appendChild(hint);
      const row = document.createElement("div");
      row.className = "qr-cards-row";
      for (const a of addresses) {
        const mobileUrl = `${a}/mobile/`;
        const card = document.createElement("div");
        card.className = "qr-card qr-blurred";
        card.title = i18nT("settings.network.tapToUnblur");
        card.addEventListener("click", () => card.classList.toggle("qr-blurred"));
        const img = document.createElement("img");
        img.src = `/api/qr?url=${encodeURIComponent(mobileUrl)}`;
        img.alt = i18nT("settings.network.qrCodeFor", { url: mobileUrl });
        img.width = 130;
        img.height = 130;
        const label = document.createElement("div");
        label.className = "qr-label";
        label.textContent = mobileUrl;
        const imgWrap = document.createElement("div");
        imgWrap.className = "qr-img-wrap";
        imgWrap.appendChild(img);
        card.append(imgWrap, label);
        row.appendChild(card);
      }
      qrWrap.appendChild(row);
    } else {
      const span = document.createElement("span");
      span.className = "settings-net-empty";
      span.textContent = i18nT("settings.network.noConnection");
      qrWrap.appendChild(span);
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

async function loadRegistryView(overlay) {
  // Scoped to the registry pane: the log viewers reuse .settings-registry-view
  // for its read-only-textarea styling and sit earlier in the markup, so a bare
  // class lookup returned the *application log* box. The registry JSON was
  // being written into a hidden textarea while the registry pane sat on its
  // literal "Loading…" placeholder for ever.
  const view = overlay.querySelector('[data-pane="registry"] .settings-registry-view');
  if (!view) return;
  view.value = "Loading…";
  try {
    const r = await fetch("/api/registry", { cache: "no-store" });
    view.value = r.ok ? await r.text() : `Failed to load registry (status ${r.status}).`;
  } catch {
    view.value = "Failed to load registry — check your connection.";
  }
}

function _fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

/** Read-only listing of the log files: paths, sizes and what writes each one.
 *  Contents are deliberately not served -- a traceback can capture anything,
 *  and the files are on the machine the user is already sitting at. */
async function loadLogsView(overlay) {
  const pathEl = overlay.querySelector(".settings-logs-path");
  const listEl = overlay.querySelector(".settings-logs-list");
  if (!pathEl || !listEl) return;
  listEl.textContent = i18nT("settings.logs.loading");
  try {
    const r = await fetch("/api/logs", { cache: "no-store" });
    if (!r.ok) throw new Error(`status ${r.status}`);
    const info = await r.json();
    pathEl.textContent = info.dir;
    const present = (info.files || []).filter((f) => f.exists);
    if (!present.length) {
      listEl.innerHTML = `<div class="settings-logs-empty">${esc(i18nT("settings.logs.noFilesYet", {
        folderNote: info.dir_exists ? "" : i18nT("settings.logs.folderNotCreated"),
      }))}</div>`;
      return;
    }
    listEl.innerHTML = present
      .map((f) => {
        const when = f.modified
          ? new Date(f.modified * 1000).toLocaleString()
          : "";
        return `<div class="settings-log-row">
            <div class="settings-log-main">
              <code class="settings-log-name">${f.name}</code>
              <span class="settings-log-meta">${_fmtBytes(f.size)}${when ? ` · ${when}` : ""}</span>
            </div>
            <div class="settings-log-desc">${f.description}</div>
          </div>`;
      })
      .join("");
  } catch (e) {
    console.warn("[settings] failed to load log info:", e);
    pathEl.textContent = i18nT("status.unavailable");
    listEl.textContent = i18nT("settings.logs.failedToLoad");
  }
}

/** Recent lines from one log, read-only. The window is applied server-side so
 *  a large file is never shipped in full. */
async function loadLogTail(overlay, view) {
  const el = overlay.querySelector(`.settings-logtail-view[data-view="${view}"]`);
  if (!el) return;
  el.value = "Loading…";
  try {
    const r = await fetch(`/api/logs/${view}?minutes=60`, { cache: "no-store" });
    el.value = r.ok ? await r.text() : `Failed to load the log (status ${r.status}).`;
  } catch (e) {
    console.warn("[settings] failed to load log tail:", e);
    el.value = "Failed to load the log — check your connection.";
  }
  // Newest entries are the interesting ones.
  el.scrollTop = el.scrollHeight;
}

/** Download every log file as one zip, for attaching to a bug report. */
async function exportLogs(btn) {
  if (btn) { btn.disabled = true; btn.textContent = i18nT("settings.exportLogs.preparing"); }
  try {
    const r = await fetch("/api/logs.zip", { cache: "no-store" });
    if (!r.ok) throw new Error(`status ${r.status}`);
    const blob = await r.blob();
    const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `stemdeck-logs-${stamp}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    // Log export isn't tied to a track/job, so a plain by-kind dismiss is the
    // right granularity — it never touches a per-track export failure (#401).
    dismissFailuresByKind("export");
  } catch (e) {
    console.warn("[settings] log export failed:", e);
    showError(i18nT("settings.exportLogs.error"), null, { retry: false });
    notifyFailure({
      kind: "export",
      message: i18nT("settings.exportLogs.error"),
      detail: String(e?.message || e),
      context: { stage: "Exporting logs" },
    });
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = i18nT("settings.exportLogs.button"); }
  }
}

let resetConfirmOverlay = null;

function closeResetConfirm() {
  resetConfirmOverlay?.remove();
  resetConfirmOverlay = null;
}

function openResetConfirm() {
  closeResetConfirm();
  const overlay = document.createElement("div");
  overlay.className = "reset-confirm-backdrop";
  overlay.innerHTML = `
    <div class="reset-confirm-card" role="dialog" aria-modal="true" aria-label="Reset app data" data-i18n-aria-label="resetConfirm.ariaLabel">
      <div class="reset-confirm-title" data-i18n="resetConfirm.title">Reset app data?</div>
      <p class="reset-confirm-body" data-i18n="resetConfirm.body">This permanently deletes every track, job, and library entry. On a shared server this affects everyone who uses it. This cannot be undone.</p>
      <p class="reset-confirm-hint" data-i18n="resetConfirm.hint">Type <strong>RESET</strong> to confirm.</p>
      <input class="reset-confirm-input" type="text" autocomplete="off" spellcheck="false" aria-label="Type RESET to confirm" data-i18n-aria-label="resetConfirm.typeToConfirmAria" />
      <div class="reset-confirm-msg" role="alert" aria-live="polite"></div>
      <div class="reset-confirm-actions">
        <button class="reset-confirm-cancel" type="button" data-i18n="resetConfirm.cancel">Cancel</button>
        <button class="reset-confirm-go" type="button" disabled data-i18n="resetConfirm.go">Reset app data</button>
      </div>
    </div>
  `;
  applyTranslations(overlay);

  const input = overlay.querySelector(".reset-confirm-input");
  const goBtn = overlay.querySelector(".reset-confirm-go");
  const msg = overlay.querySelector(".reset-confirm-msg");

  input.addEventListener("input", () => {
    goBtn.disabled = input.value.trim() !== "RESET";
  });
  overlay.querySelector(".reset-confirm-cancel").addEventListener("click", closeResetConfirm);
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeResetConfirm(); });
  const onKey = (e) => { if (e.code === "Escape") closeResetConfirm(); };
  document.addEventListener("keydown", onKey, { once: true });

  goBtn.addEventListener("click", async () => {
    goBtn.disabled = true;
    input.disabled = true;
    msg.textContent = i18nT("resetConfirm.resetting");
    try {
      const r = await fetch("/api/reset", { method: "POST" });
      if (!r.ok) {
        let detail = i18nT("resetConfirm.failed");
        try { detail = (await r.json()).detail || detail; } catch (err) { console.warn("reset error body parse failed:", err); }
        msg.textContent = detail;
        goBtn.disabled = false;
        input.disabled = false;
        return;
      }
      // Backend job/registry data is gone. The browser-side library index
      // (folders, tracks, per-job mixer state, trash) is a separate store
      // the API call above doesn't touch -- clear it too, then reload so
      // every in-memory JS structure re-initializes from the now-empty state
      // instead of trying to reconcile itself piecemeal.
      if (window.__TAURI__?.core?.invoke) {
        try {
          await window.__TAURI__.core.invoke("reset_user_data");
        } catch (err) {
          console.warn("reset_user_data failed:", err);
        }
      }
      try { localStorage.clear(); } catch (err) { console.warn("localStorage.clear failed:", err); }
      window.location.reload();
    } catch (err) {
      console.warn("reset failed:", err);
      msg.textContent = i18nT("resetConfirm.failedConnection");
      goBtn.disabled = false;
      input.disabled = false;
    }
  });

  document.body.appendChild(overlay);
  resetConfirmOverlay = overlay;
  input.focus();
}

function openLibraryEditor() {
  closeFolderEditor();
  closeLibraryEditor();

  const overlay = document.createElement("div");
  overlay.className = "library-editor-backdrop";
  overlay.innerHTML = `
    <div class="library-editor" role="dialog" aria-modal="true" aria-label="Settings" data-i18n-aria-label="settings.title">
      <div class="library-editor-head">
        <span data-i18n="settings.title">Settings</span>
        <button class="library-editor-close" type="button" aria-label="Close" data-i18n-aria-label="settings.closeAria">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"></path></svg>
        </button>
      </div>
      <div class="settings-tabs" role="tablist">
        <button class="settings-tab active" type="button" data-tab="general" role="tab" data-i18n="settings.tab.general">General</button>
        <button class="settings-tab" type="button" data-tab="network" role="tab" data-i18n="settings.tab.network">Network</button>
        <button class="settings-tab" type="button" data-tab="export" role="tab" data-i18n="settings.tab.export">Export</button>
        <button class="settings-tab" type="button" data-tab="logs" role="tab" data-i18n="settings.tab.logs">Logs</button>
        <button class="settings-tab" type="button" data-tab="registry" role="tab" data-i18n="settings.tab.registry">Registry</button>
      </div>
      <div class="settings-pane" data-pane="general">
        <div class="settings-section">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.language.title">Language</div>
              <div class="settings-row-desc" data-i18n="settings.language.desc">Display language for this app.</div>
            </div>
            <select class="settings-select settings-select-wide set-language" aria-label="Language"></select>
          </div>
        </div>
        <div class="settings-section">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.maxDuration.title">Max track length</div>
              <div class="settings-row-desc set-max-duration-desc">Longest track accepted for processing, in minutes (max 60).</div>
            </div>
            <input type="text" class="settings-num-input set-max-duration" inputmode="numeric" maxlength="2" aria-label="Max track length in minutes" data-i18n-aria-label="settings.maxDuration.title" />
          </div>
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.playlistLimit.title">Playlist import limit</div>
              <div class="settings-row-desc" data-i18n="settings.playlistLimit.desc">Most tracks one playlist import will queue (max 200).</div>
            </div>
            <input type="text" class="settings-num-input set-playlist-max" inputmode="numeric" maxlength="3" aria-label="Playlist import limit" data-i18n-aria-label="settings.playlistLimit.title" />
          </div>
          <div class="settings-row settings-row-stack">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.cookies.title">YouTube cookies</div>
              <div class="settings-row-desc" data-i18n="settings.cookies.desc">Optional. Path to a cookies.txt file, used only when YouTube asks StemDeck to confirm it is not a bot. Leave this empty unless imports are failing.</div>
            </div>
            <input type="text" class="settings-text-input set-cookies-file" spellcheck="false" autocomplete="off" placeholder="Path to cookies.txt" data-i18n-placeholder="settings.cookies.placeholder" aria-label="YouTube cookies" data-i18n-aria-label="settings.cookies.title" />
            <div class="cookies-file-msg" role="status" aria-live="polite"></div>
          </div>
          <div class="settings-row settings-row-stack">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.stemsLocation.title">StemData location</div>
            </div>
            <div class="stems-location">
              <code class="stems-location-path" title=""></code>
              <span class="stems-location-size"></span>
              <button class="settings-btn set-stems-location" type="button" data-i18n="settings.stemsLocation.change">Change…</button>
            </div>
            <div class="stems-location-msg" role="status" aria-live="polite"></div>
          </div>
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.autoDelete.title">Automatically delete finished tracks</div>
              <div class="settings-row-desc" data-i18n="settings.autoDelete.desc">Off unless you turn it on. Separated tracks are kept forever by default. Deleting them cannot be undone.</div>
            </div>
            <label class="settings-switch">
              <input type="checkbox" class="auto-delete-input" />
              <span class="settings-switch-track"><span class="settings-switch-thumb"></span></span>
            </label>
          </div>
          <div class="settings-row auto-delete-days-row disabled">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.autoDelete.daysTitle">Delete after</div>
              <div class="settings-row-desc auto-delete-days-desc">Days a finished track is kept before it is deleted.</div>
            </div>
            <input type="text" class="settings-num-input set-auto-delete-days" inputmode="numeric" maxlength="3" aria-label="Days a track is kept" data-i18n-aria-label="settings.autoDelete.daysTitle" />
          </div>
        </div>
        <div class="settings-section">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.device.title">Compute device</div>
              <div class="settings-row-desc set-demucs-desc">Device used for stem separation. Applies to the next track.</div>
            </div>
            <select class="settings-select settings-select-wide set-demucs-device" aria-label="Compute device" data-i18n-aria-label="settings.device.title">
              <option value="auto" data-i18n="settings.device.auto">Auto</option>
              <option value="cuda">CUDA (NVIDIA)</option>
              <option value="mps">MPS (Apple Silicon)</option>
              <option value="cpu">CPU</option>
            </select>
          </div>
        </div>
        <div class="settings-section">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.quality.title">Separation quality</div>
              <div class="settings-row-desc" data-i18n="settings.quality.desc">Best runs the separator twice with randomized shifts and averages the result — cleaner stems, twice the time.</div>
            </div>
            <select class="settings-select settings-select-wide set-separation-quality" aria-label="Separation quality" data-i18n-aria-label="settings.quality.title">
              <option value="standard" data-i18n="settings.quality.standard">Standard</option>
              <option value="best" data-i18n="settings.quality.best">Best (2× slower)</option>
            </select>
          </div>
        </div>
        <div class="settings-subhead" data-i18n="settings.outOfSync.subhead">Out of sync tracks</div>
        <div class="library-editor-table-wrap">
          <table class="library-editor-table">
            <thead><tr><th data-i18n="settings.outOfSync.colName">Name</th><th data-i18n="settings.outOfSync.colSource">Source</th><th data-i18n="settings.outOfSync.colLocation">Location</th></tr></thead>
            <tbody class="library-editor-body"></tbody>
          </table>
        </div>
        <div class="library-editor-foot">
          <span class="library-editor-status" aria-live="polite"></span>
          <button class="library-editor-sync" type="button" data-i18n="settings.outOfSync.resync">Resync out of sync tracks</button>
        </div>
        <div class="settings-section">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.exportLogs.title">Export logs</div>
              <div class="settings-row-desc" data-i18n="settings.exportLogs.desc">Download every log file as a single zip — the thing to attach to a bug report. See the Logs tab for where they live.</div>
            </div>
            <button class="settings-export-logs" type="button" data-i18n="settings.exportLogs.button">Export logs</button>
          </div>
        </div>
        <div class="settings-section settings-danger-zone">
          <div class="settings-subhead settings-danger-subhead" data-i18n="settings.dangerZone">Danger zone</div>
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.resetData.title">Reset app data</div>
              <div class="settings-row-desc" data-i18n="settings.resetData.desc">Permanently deletes every track, job, and library entry. On a shared server this affects everyone who uses it. Cannot be undone.</div>
            </div>
            <button class="settings-reset-btn" type="button" data-i18n="settings.resetData.button">Reset app data…</button>
          </div>
        </div>
      </div>
      <div class="settings-pane hidden" data-pane="network">
        ${networkSettingsHtml()}
        <div class="settings-section">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.network.port.title">Port</div>
              <div class="settings-row-desc" data-i18n="settings.network.port.desc">Port StemDeck runs on. Restart to apply.</div>
            </div>
            <input type="text" class="settings-num-input set-port" inputmode="numeric" maxlength="5" aria-label="Port" data-i18n-aria-label="settings.network.port.title" />
          </div>
        </div>
      </div>
      <div class="settings-pane hidden" data-pane="export">
        <div class="settings-section">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.export.sampleRate.title">Sample rate</div>
              <div class="settings-row-desc" data-i18n="settings.export.sampleRate.desc">Sample rate for exported mixes and regions (WAV, FLAC, MP3). 44.1 kHz suits most DAWs and samplers; pick another if your hardware needs it.</div>
            </div>
            <select class="settings-select set-export-samplerate" aria-label="Export sample rate" data-i18n-aria-label="settings.export.sampleRate.title">
              <option value="22050">22.05 kHz</option>
              <option value="32000">32 kHz</option>
              <option value="44100">44.1 kHz</option>
              <option value="48000">48 kHz</option>
            </select>
          </div>
        </div>
        <div class="settings-section">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.export.videoQuality.title">MP4 video quality</div>
              <div class="settings-row-desc" data-i18n="settings.export.videoQuality.desc">Max resolution for MP4 export and YouTube video.</div>
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
      <div class="settings-pane hidden" data-pane="logs">
        <div class="settings-subtabs" role="tablist">
          <button class="settings-subtab active" type="button" data-sub="location" role="tab" data-i18n="settings.logs.locationTab">Location</button>
          <button class="settings-subtab" type="button" data-sub="application" role="tab" data-i18n="settings.logs.applicationTab">Application log</button>
          <button class="settings-subtab" type="button" data-sub="backend" role="tab" data-i18n="settings.logs.backendTab">Backend log</button>
          <button class="settings-subtab" type="button" data-sub="setup" role="tab" data-i18n="settings.logs.setupTab">Setup log</button>
        </div>
        <div class="settings-subpane" data-subpane="location">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.logs.location.title">Log location</div>
              <div class="settings-row-desc" data-i18n="settings.logs.location.desc">Where StemDeck writes its logs on this machine. Read-only — open them in a file manager or use Export logs.</div>
            </div>
            <button class="settings-registry-refresh settings-logs-refresh" type="button" data-i18n="settings.logs.refresh">Refresh</button>
          </div>
          <div class="settings-logs-dir"><code class="settings-logs-path" data-i18n="settings.logs.loading">Loading…</code></div>
          <div class="settings-logs-list" data-i18n="settings.logs.loading">Loading…</div>
        </div>
        <div class="settings-subpane hidden" data-subpane="application">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.logs.application.title">Application log</div>
              <div class="settings-row-desc" data-i18n="settings.logs.application.desc">The last hour from <code>stemdeck.log</code> — pipeline, API and job activity. Read-only.</div>
            </div>
            <button class="settings-registry-refresh settings-logtail-refresh" type="button" data-view="application" data-i18n="settings.logs.refresh">Refresh</button>
          </div>
          <textarea class="settings-registry-view settings-logtail-view" data-view="application" readonly spellcheck="false" aria-label="Application log (read only)" data-i18n-aria-label="settings.logs.applicationAria" data-i18n="settings.logs.loading">Loading…</textarea>
        </div>
        <div class="settings-subpane hidden" data-subpane="backend">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.logs.backend.title">Backend log</div>
              <div class="settings-row-desc" data-i18n="settings.logs.backend.desc">The last hour from <code>backend.log</code> — raw output of the bundled Python process, including anything that crashed it before the application log could record it. Desktop app only. Read-only.</div>
            </div>
            <button class="settings-registry-refresh settings-logtail-refresh" type="button" data-view="backend" data-i18n="settings.logs.refresh">Refresh</button>
          </div>
          <textarea class="settings-registry-view settings-logtail-view" data-view="backend" readonly spellcheck="false" aria-label="Backend log (read only)" data-i18n-aria-label="settings.logs.backendAria" data-i18n="settings.logs.loading">Loading…</textarea>
        </div>
        <div class="settings-subpane hidden" data-subpane="setup">
          <div class="settings-row">
            <div class="settings-row-text">
              <div class="settings-row-title" data-i18n="settings.logs.setup.title">Setup log</div>
              <div class="settings-row-desc" data-i18n="settings.logs.setup.desc">The last hour from <code>setup.log</code> — first-run setup and GPU runtime installation. Desktop app only. Read-only.</div>
            </div>
            <button class="settings-registry-refresh settings-logtail-refresh" type="button" data-view="setup" data-i18n="settings.logs.refresh">Refresh</button>
          </div>
          <textarea class="settings-registry-view settings-logtail-view" data-view="setup" readonly spellcheck="false" aria-label="Setup log (read only)" data-i18n-aria-label="settings.logs.setupAria" data-i18n="settings.logs.loading">Loading…</textarea>
        </div>
      </div>
      <div class="settings-pane hidden" data-pane="registry">
        <div class="settings-row">
          <div class="settings-row-text">
            <div class="settings-row-title" data-i18n="settings.registry.title">Job registry</div>
            <div class="settings-row-desc" data-i18n="settings.registry.desc">Read-only view of <code>registry.json</code> — the persisted list of completed jobs on disk.</div>
          </div>
          <button class="settings-registry-refresh" type="button" data-i18n="settings.logs.refresh">Refresh</button>
        </div>
        <textarea class="settings-registry-view" readonly spellcheck="false" aria-label="Job registry (read only)" data-i18n-aria-label="settings.registry.aria" data-i18n="settings.logs.loading">Loading…</textarea>
      </div>
      <div class="settings-foot">
        <button class="settings-done" type="button" data-i18n="settings.done">Done</button>
      </div>
    </div>
  `;
  applyTranslations(overlay);

  renderLibraryRows(overlay.querySelector(".library-editor-body"));

  overlay.querySelectorAll(".settings-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.dataset.tab;
      overlay.querySelectorAll(".settings-tab").forEach((t) => t.classList.toggle("active", t === tab));
      overlay.querySelectorAll(".settings-pane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== name));
      if (name === "registry") loadRegistryView(overlay);
      if (name === "logs") loadLogsView(overlay);
    });
  });
  overlay.querySelector(".settings-registry-refresh:not(.settings-logs-refresh)")
    ?.addEventListener("click", () => loadRegistryView(overlay));
  overlay.querySelector(".settings-logs-refresh")?.addEventListener("click", () => loadLogsView(overlay));
  overlay.querySelectorAll(".settings-subtab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.dataset.sub;
      overlay.querySelectorAll(".settings-subtab").forEach((t) => t.classList.toggle("active", t === tab));
      overlay.querySelectorAll(".settings-subpane").forEach((p) => p.classList.toggle("hidden", p.dataset.subpane !== name));
      if (name === "location") loadLogsView(overlay);
      else loadLogTail(overlay, name);
    });
  });
  overlay.querySelectorAll(".settings-logtail-refresh").forEach((b) =>
    b.addEventListener("click", () => loadLogTail(overlay, b.dataset.view)));
  overlay.querySelector(".settings-export-logs")?.addEventListener("click", (e) => exportLogs(e.currentTarget));

  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeLibraryEditor(); });
  // (status summary is filled in after the overlay is in the DOM, below)
  overlay.querySelector(".library-editor-close")?.addEventListener("click", closeLibraryEditor);
  overlay.querySelector(".settings-done")?.addEventListener("click", closeLibraryEditor);
  overlay.querySelector(".library-editor-sync")?.addEventListener("click", () => resyncLibrary());
  // Escape closes from anywhere (the overlay isn't focused, so listen on document).
  libraryEditorOnKey = (e) => { if (e.code === "Escape") closeLibraryEditor(); };
  document.addEventListener("keydown", libraryEditorOnKey);

  document.body.appendChild(overlay);
  libraryEditor = overlay;
  refreshLibrarySyncSummary();
  const isDesktop = Boolean(window.__TAURI__?.core?.invoke);
  wireLanguageSetting(overlay);
  wireGeneralSettings(overlay);
  wireStemsLocation(overlay);
  wireNetworkSetting(overlay);
  if (!isDesktop) {
    overlay.querySelector(".net-access-input")?.setAttribute("disabled", "");
    overlay.querySelector(".set-port")?.setAttribute("readonly", "");
    overlay.querySelector(".set-port")?.setAttribute("disabled", "");
    const note = document.createElement("p");
    note.className = "settings-server-note";
    note.textContent = i18nT("settings.readOnlyServer");
    overlay.querySelector("[data-pane='network']")?.prepend(note);
  }
  // Reset app data (#312): originally a "session keeps coming back across
  // desktop reinstalls" fix (the real persisted state lives in
  // ~/Documents/StemDeck, not the extracted package's own bundled data/
  // folder), available in server mode too -- same network_gate trust
  // boundary as every other settings-mutating endpoint, enforced
  // server-side (not just hidden here). On a shared server this deletes
  // every user's library, which the confirm dialog says explicitly.
  overlay.querySelector(".settings-reset-btn")?.addEventListener("click", () => {
    openResetConfirm();
  });
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
  if (statusEl) statusEl.textContent = i18nT("library.syncing");
  if (syncBtn) syncBtn.disabled = true;

  try {
    const res = await fetch("/api/jobs", { cache: "no-store" });
    if (!res.ok) throw new Error(`status ${res.status}`);

    await syncWithServer(); // pulls in new server jobs, reconciles done <-> unavailable
    if (libraryEditor) renderLibraryRows(libraryEditor.querySelector(".library-editor-body"));

    // Collect what's still unavailable; auto-restore the ones with a URL source.
    const trashIds = new Set(getTrashFolder()?.items || []);
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
    if (statusEl) statusEl.textContent = i18nT("library.syncFailed");
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
  wireReleaseDialog();
  wireSettingsMenu();
  setDisplayedVersion(currentVersion);
  render();

  // Patch rows in place on every queue frame. A full render() here would
  // rebuild the sidebar several times a second.
  onQueueChange(onQueueFrame);
  onJobSettled(completeSettledJob);
  startQueueStream();

  loadCurrentVersion().finally(checkForUpdate);
  syncWithServer();
}
