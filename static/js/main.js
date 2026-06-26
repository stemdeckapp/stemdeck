import {
  playBtn, loopBtn, multitrack, totalDuration, loopEnabled, loopStart, loopEnd,
  setLoopStart, setLoopEnd, selectedStems, saveSelectedStems, stemSelectionReady,
} from "./state.js";
import { STEM_NAMES, syncStemNamesFromAPI } from "./constants.js";
import { renderEmptyShell, buildStripStems, downloadCurrentMix, downloadCurrentVideo, downloadAllStemsZip, downloadRegionMix, drawFooterPlaceholder } from "./player.js";
import { wireJobForm, showError } from "./job.js";
import { wireTransportButtons } from "./transport.js";
import { togglePlayPause, updateLoopRegionVisual } from "./transport.js";
import { wireStemListControls, wireMixerToolbar } from "./mixer.js";
import { initCatalog } from "./catalog.js";
import { runStoreMigrationIfNeeded } from "./utils.js";

// ─── Stem choice toggles on the import page ───
//
// Filter-chip semantics (Spotify-style). The natural mental model when
// a user sees all 6 stems lit up is "everything is extracted"; when
// they then click ONE chip, they expect "now only this one". A plain
// toggle inverts the clicked chip and leaves the others on, which
// reads as "I just deselected the one I wanted" -- exactly the user
// confusion that prompted this fix.
//
// Algorithm:
//  - "All selected" is the implicit default (no filter applied).
//  - First click on a chip while in default state switches to
//    "only this stem" (clears all others).
//  - Subsequent clicks on inactive chips ADD them to the filter.
//  - Clicks on the only-selected chip clear it; if that empties the
//    selection, we revert to "all selected" (wraparound).
//
// Persisted across reloads so the next song honors the user's last
// chosen subset, but a 0-selection state is normalized to all 6.
function refreshStemChoiceVisuals() {
  for (const btn of document.querySelectorAll(".stem-choice[data-stem]")) {
    btn.setAttribute(
      "aria-pressed",
      String(selectedStems.has(btn.dataset.stem)),
    );
  }
}

function handleStemChoiceClick(stem) {
  const allSelected = selectedStems.size === STEM_NAMES.length;
  if (allSelected) {
    // Default state -> switch to "only this stem".
    selectedStems.clear();
    selectedStems.add(stem);
  } else if (selectedStems.has(stem)) {
    selectedStems.delete(stem);
    if (selectedStems.size === 0) {
      // Empty out wraps back to "all" so the user is never stuck.
      for (const n of STEM_NAMES) selectedStems.add(n);
    }
  } else {
    selectedStems.add(stem);
  }
  saveSelectedStems();
  refreshStemChoiceVisuals();
  buildStripStems();
}

function wireStemChoiceButtons() {
  refreshStemChoiceVisuals();
  for (const btn of document.querySelectorAll(".stem-choice[data-stem]")) {
    btn.addEventListener("click", () => handleStemChoiceClick(btn.dataset.stem));
  }
}

function wireAllButton() {
  const allBtn = document.getElementById("stemAllBtn");
  if (!allBtn) return;

  function syncAllBtn() {
    allBtn.setAttribute("aria-pressed", String(selectedStems.size === STEM_NAMES.length));
  }

  allBtn.addEventListener("click", () => {
    const allSelected = selectedStems.size === STEM_NAMES.length;
    if (allSelected) {
      selectedStems.clear();
    } else {
      for (const n of STEM_NAMES) selectedStems.add(n);
    }
    saveSelectedStems();
    refreshStemChoiceVisuals();
    buildStripStems();
    syncAllBtn();
  });

  /* Keep All in sync when individual stems are toggled */
  for (const btn of document.querySelectorAll(".stem-choice[data-stem]")) {
    btn.addEventListener("click", syncAllBtn);
  }

  syncAllBtn();
}

// ─── Wire everything up ───

syncStemNamesFromAPI().then(() => buildStripStems());
wireJobForm();
wireTransportButtons();
wireFooterControls();
requestAnimationFrame(drawFooterPlaceholder);
wireStemListControls();
wireMixerToolbar();
wireStemChoiceButtons();
wireAllButton();
wireFileDrop();
wireAppShellControls();

(async () => {
  await runStoreMigrationIfNeeded();
  await stemSelectionReady;
  refreshStemChoiceVisuals();
  await initCatalog();
})().catch(console.error);

// ─── Footer: speed dropdown, export dropdown, scrub seek ───

function wireFooterControls() {
  // ── Export split-button dropdown ──
  // The full button toggles the export menu. Export actions live inside
  // the dropdown so the hit target is predictable.
  // The menu offers Mix / All Stems / Current Region, with a WAV/MP3 toggle in
  // the header. All exports reuse the backend-served download helpers.
  const exportBtn   = document.getElementById("t-export-btn");
  const exportPanel = document.getElementById("t-export-panel");
  const exportLabel = document.getElementById("t-export-label");
  const fmtWav   = document.getElementById("t-fmt-wav");
  const fmtMp3   = document.getElementById("t-fmt-mp3");
  const fmtFlac  = document.getElementById("t-fmt-flac");
  const fmtMp4   = document.getElementById("t-fmt-mp4");
  const exportWrap = document.getElementById("footer-export-wrap");
  const itemMix    = document.getElementById("t-export-mix");
  const itemStems  = document.getElementById("t-export-stems");
  const itemRegion = document.getElementById("t-export-region");
  const mixDescEl  = itemMix?.querySelector(".chip-item-desc");
  // Only the rows actually visible in the current format mode (MP4 hides the
  // audio-only Stems/Region rows).
  const actionItems = () =>
    [itemMix, itemStems, itemRegion].filter((it) => it && it.offsetParent !== null);

  // MP4 is a format choice, shown only for jobs with a preserved video track.
  // It applies to the mix only — stems/region are audio-only.
  const videoAvailable = () => !!exportWrap?.classList.contains("has-video");

  let format = "wav";
  let busy = false;

  const panelOpen = () => exportPanel && !exportPanel.classList.contains("hidden");
  function openPanel() {
    closeAllChipPanels();
    // A previous (video) job may have left MP4 selected; revert if unavailable now.
    if (format === "mp4" && !videoAvailable()) setFormat("wav");
    exportPanel?.classList.remove("hidden");
    exportBtn?.setAttribute("aria-expanded", "true");
  }
  function closePanel() {
    exportPanel?.classList.add("hidden");
    exportBtn?.setAttribute("aria-expanded", "false");
  }

  function setFormat(f) {
    format = f;
    for (const [btn, val] of [[fmtWav, "wav"], [fmtMp3, "mp3"], [fmtFlac, "flac"], [fmtMp4, "mp4"]]) {
      btn?.classList.toggle("active", f === val);
      btn?.setAttribute("aria-checked", String(f === val));
    }
    applyFormatState();
  }
  fmtWav?.addEventListener("click", (e) => { e.stopPropagation(); setFormat("wav"); });
  fmtMp3?.addEventListener("click", (e) => { e.stopPropagation(); setFormat("mp3"); });
  fmtFlac?.addEventListener("click", (e) => { e.stopPropagation(); setFormat("flac"); });
  fmtMp4?.addEventListener("click", (e) => { e.stopPropagation(); setFormat("mp4"); });

  // MP4 exports the mix muxed with the source video. Stems and region have no
  // video equivalent, so they're hidden (via .fmt-mp4) while MP4 is selected,
  // leaving just "Export Mix".
  function applyFormatState() {
    const video = format === "mp4";
    exportPanel?.classList.toggle("fmt-mp4", video);
    if (mixDescEl) {
      mixDescEl.textContent = video ? "Export mix with the original video" : "Export the mixed audio";
    }
    if (!video) updateLoopRegionVisual(); // restores the region item's disabled state
  }

  function resetBusy() {
    busy = false;
    exportBtn?.classList.remove("is-busy");
    if (exportLabel) exportLabel.textContent = "Export Mix";
    itemMix?.removeAttribute("aria-disabled");
    applyFormatState(); // restores stems/region per the active format
  }

  // Single-file (mix/region) downloads give no JS-observable byte progress, so
  // show a brief indeterminate "Exporting…" state, then reset.
  function flashBusy() {
    busy = true;
    exportBtn?.classList.add("is-busy");
    if (exportLabel) exportLabel.textContent = "Exporting…";
    actionItems().forEach((it) => it?.setAttribute("aria-disabled", "true"));
    closePanel();
    window.setTimeout(resetBusy, 1200);
  }

  exportBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (busy) return;
    panelOpen() ? closePanel() : openPanel();
  });

  // Export Mix: MP4 produces the video; any other format an audio mix.
  itemMix?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (busy) return;
    const ok = format === "mp4" ? downloadCurrentVideo() : downloadCurrentMix(format);
    if (!ok) { showError("All stems are muted - nothing to export."); return; }
    flashBusy();
  });

  itemRegion?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (busy || itemRegion.getAttribute("aria-disabled") === "true") return;
    const ok = downloadRegionMix(format);
    if (!ok) { showError("All stems are muted - nothing to export."); return; }
    flashBusy();
  });

  // All Stems = a single backend-built ZIP, named after the song. Audio-only,
  // so it's disabled (and inert) while MP4 is the selected format.
  itemStems?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (busy || itemStems.getAttribute("aria-disabled") === "true") return;
    downloadAllStemsZip(format);
    flashBusy();
  });

  // Keyboard: ↓ opens/moves into the menu, ↑/↓ cycle rows, Esc closes + restores focus.
  exportBtn?.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!panelOpen()) openPanel();
      actionItems().find((it) => it?.getAttribute("aria-disabled") !== "true")?.focus();
    }
  });
  exportPanel?.addEventListener("keydown", (e) => {
    const focusable = actionItems().filter((it) => it && it.getAttribute("aria-disabled") !== "true");
    const idx = focusable.indexOf(document.activeElement);
    if (e.key === "Escape") { closePanel(); exportBtn?.focus(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); focusable[(idx + 1) % focusable.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); focusable[(idx - 1 + focusable.length) % focusable.length]?.focus(); }
  });

  // ── Scrub bar seek ──
  const scrub = document.getElementById("footer-scrub");
  if (scrub) {
    function seekToX(clientX) {
      if (!multitrack || !totalDuration) return;
      const rect = scrub.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      multitrack.setTime(frac * totalDuration);
    }
    let _scrubbing = false;
    scrub.addEventListener("mousedown", (e) => {
      _scrubbing = true;
      seekToX(e.clientX);
    });
    document.addEventListener("mousemove", (e) => { if (_scrubbing) seekToX(e.clientX); });
    document.addEventListener("mouseup",   () => { _scrubbing = false; });
  }

  // ── Close panels on outside click ──
  document.addEventListener("click", closeAllChipPanels);
}

function closeAllChipPanels() {
  document.querySelectorAll(".footer-chip-panel:not(.hidden)").forEach((p) => {
    p.classList.add("hidden");
    p.previousElementSibling?.setAttribute("aria-expanded", "false");
  });
}

// ─── File drop on URL input ───

function wireFileDrop() {
  const urlWrap = document.querySelector(".url-wrap");
  const urlInput = document.getElementById("url");
  const fileInput = document.getElementById("fileInput");
  const filePill = document.getElementById("filePill");
  const fileName = document.getElementById("fileName");
  const fileSize = document.getElementById("fileSize");
  const fileClear = document.getElementById("fileClear");
  if (!urlWrap || !urlInput || !fileInput || !filePill) return;

  function formatBytes(n) {
    return n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  const MAX_UPLOAD_BYTES = 400 * 1024 * 1024; // must match server _MAX_UPLOAD_BYTES

  function applyFile(file) {
    if (!file) return;
    const lower = file.name.toLowerCase();
    if (!lower.endsWith(".mp3") && !lower.endsWith(".wav") && !lower.endsWith(".flac") &&
        !lower.endsWith(".mp4") && !lower.endsWith(".m4a")) {
      showError("Only MP3, WAV, FLAC, MP4, and M4A files are supported.");
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      showError(`File is too large (${formatBytes(file.size)}). Maximum is 400 MB.`);
      return;
    }
    if (fileName) fileName.textContent = file.name;
    if (fileSize) fileSize.textContent = formatBytes(file.size);
    filePill.classList.remove("hidden");
    urlWrap.classList.add("has-file");
    // Cache the File object directly on the element so job.js can always
    // retrieve it even after the browser clears fileInput.files following
    // a fetch() submission (known WKWebView / Chromium behaviour).
    fileInput._file = file;
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    urlInput.value = "";
    urlInput.removeAttribute("required");
  }

  function clearFile() {
    filePill.classList.add("hidden");
    urlWrap.classList.remove("has-file");
    fileInput._file = null;
    fileInput.value = "";
    urlInput.setAttribute("required", "");
  }

  fileClear?.addEventListener("click", clearFile);

  urlWrap.addEventListener("dragover", (e) => {
    if (!e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    urlWrap.classList.add("drag-over");
  });
  urlWrap.addEventListener("dragleave", (e) => {
    if (!urlWrap.contains(e.relatedTarget)) urlWrap.classList.remove("drag-over");
  });
  urlWrap.addEventListener("drop", (e) => {
    e.preventDefault();
    urlWrap.classList.remove("drag-over");
    const file = e.dataTransfer.files[0];
    if (file) applyFile(file);
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) applyFile(fileInput.files[0]);
  });
}

// ─── App shell controls ───

function wireAppShellControls() {
  document.getElementById("appMenuBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const app = document.querySelector(".app");
    // In trash view: switch back to library.
    if (document.querySelector(".sidebar.trash-view, .sidebar.favorites-view")) {
      document.querySelector(".rail-library")?.click();
    }
    // If collapsed: open. Never collapse from the library button.
    if (app?.classList.contains("cat-collapsed")) {
      document.getElementById("catalogToggle")?.click();
    }
  });

}

// ─── Keyboard shortcuts ───

document.addEventListener("keydown", (e) => {
  if (!multitrack) return;
  if (e.target instanceof HTMLInputElement) return;
  if (e.code === "Space") {
    e.preventDefault();
    togglePlayPause();
  } else if (e.code === "BracketLeft") {
    e.preventDefault();
    multitrack.setTime(Math.max(0, multitrack.getCurrentTime() - 5));
  } else if (e.code === "BracketRight") {
    e.preventDefault();
    multitrack.setTime(
      Math.min(multitrack.getDuration(), multitrack.getCurrentTime() + 5),
    );
  } else if (e.code === "KeyL") {
    e.preventDefault();
    loopBtn.click();
  } else if (e.code === "KeyI" && loopEnabled && multitrack) {
    e.preventDefault();
    setLoopStart(Math.min(multitrack.getCurrentTime(), loopEnd - 0.5));
    updateLoopRegionVisual();
  } else if (e.code === "KeyO" && loopEnabled && multitrack) {
    e.preventDefault();
    setLoopEnd(Math.max(multitrack.getCurrentTime(), loopStart + 0.5));
    updateLoopRegionVisual();
  }
});

// ─── External links ───

document.addEventListener("click", (e) => {
  const dl = e.target.closest("a.lane-dl");
  if (dl?.href) {
    const openUrl = window.__TAURI__?.core?.invoke;
    if (openUrl) {
      e.preventDefault();
      openUrl("open_url", { url: dl.href });
    }
    return;
  }
  const anchor = e.target.closest('a[target="_blank"]');
  if (anchor?.href) {
    const openUrl = window.__TAURI__?.core?.invoke;
    if (openUrl) {
      e.preventDefault();
      openUrl("open_url", { url: anchor.href });
    }
  }
});

// ─── Global error logging ───

window.addEventListener("error", (e) => {
  console.error("[app:error]", e.message, "\n", e.filename, ":", e.lineno, "\n", e.error?.stack ?? "");
});
window.addEventListener("unhandledrejection", (e) => {
  console.error("[app:unhandledrejection]", e.reason?.message ?? e.reason, "\n", e.reason?.stack ?? "");
});

// ─── Bootstrap ───

buildStripStems();
renderEmptyShell();
