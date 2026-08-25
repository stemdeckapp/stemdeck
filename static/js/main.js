import {
  playBtn, loopBtn, multitrack, totalDuration, loopEnabled, loopStart, loopEnd,
  setLoopStart, setLoopEnd, selectedStems, saveSelectedStems, stemSelectionReady,
  currentJobId, vocalSplitMode, vocalSplitModeReady, setVocalSplitMode,
} from "./state.js";
import { STEM_NAMES, syncStemNamesFromAPI } from "./constants.js";
import { renderEmptyShell, buildStripStems, downloadCurrentMix, downloadCurrentVideo, downloadAllStemsZip, downloadRegionMix, drawFooterPlaceholder } from "./player.js";
import { wireJobForm, showError } from "./job.js";
import { initSearch } from "./search.js";
import { wireTransportButtons } from "./transport.js";
import { wireBeatGridUi } from "./beatgridUi.js";
import { togglePlayPause, updateLoopRegionVisual, toggleMetronome } from "./transport.js";
import { wireStemListControls, wireMixerToolbar } from "./mixer.js";
import { initCatalog, collectDiagnostics } from "./catalog.js";
import { initNotifications, notifyFailure, dismissFailuresByJobId } from "./notifications.js";
import { runStoreMigrationIfNeeded } from "./utils.js";
import { initI18n, applyTranslations, t, plural, onLanguageChange } from "./i18n.js";

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

// ─── Vocals: All / Lead + Backing toggle (on-demand split, #275) ───
//
// Only meaningful while Vocals is actually selected above -- hidden
// otherwise so it can't imply a choice that has nothing to act on.

function refreshVocalModeToggleVisibility() {
  document.getElementById("vocalModeToggle")?.classList.toggle("hidden", !selectedStems.has("vocals"));
}

function wireVocalModeToggle() {
  const wrap = document.getElementById("vocalModeToggle");
  if (!wrap) return;
  for (const btn of wrap.querySelectorAll(".vocal-mode-btn")) {
    btn.addEventListener("click", () => {
      setVocalSplitMode(btn.dataset.mode);
      for (const b of wrap.querySelectorAll(".vocal-mode-btn")) {
        b.setAttribute("aria-pressed", String(b.dataset.mode === btn.dataset.mode));
      }
    });
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
  refreshVocalModeToggleVisibility();
  buildStripStems();
}

function wireStemChoiceButtons() {
  refreshStemChoiceVisuals();
  refreshVocalModeToggleVisibility();
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
    refreshVocalModeToggleVisibility();
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

// Applied as early as possible in module execution, ahead of every other
// top-level call below, to minimize the flash of English before the DOM
// gets its real language (unavoidable without server-side rendering, since
// static/index.html always ships pre-rendered in English).
const i18nReady = initI18n().then(() => applyTranslations(document));

syncStemNamesFromAPI().then(() => buildStripStems());
wireJobForm();
// Live search on the topbar box. Picking a result fills the box and stops
// there: extraction is minutes of work, so it stays behind the deliberate
// press of Process rather than starting on a click in a list the user may
// still be reading. The import flow (single track, playlist, capacity, stems)
// is untouched, and search never learns about any of it.
initSearch((item) => {
  const urlInput = document.getElementById("url");
  if (!urlInput) return;
  urlInput.value = item.url;
  // Fire input so anything listening to the box (the drop zone, the file pill)
  // sees the new value, then leave the caret where a correction would go.
  urlInput.dispatchEvent(new Event("input", { bubbles: true }));
  urlInput.focus();
  urlInput.setSelectionRange(urlInput.value.length, urlInput.value.length);
});
wireTransportButtons();
wireBeatGridUi();
wireFooterControls();
requestAnimationFrame(drawFooterPlaceholder);
wireStemListControls();
wireMixerToolbar();
wireStemChoiceButtons();
wireAllButton();
wireVocalModeToggle();
wireFileDrop();
wireAppShellControls();

(async () => {
  await i18nReady;
  // Waits for the language to be resolved: the empty shell's stem labels
  // (STEM_DISPLAY) are a one-time textContent snapshot, not a live binding,
  // so rendering it before i18n is ready would freeze it in English
  // regardless of the stored language preference.
  renderEmptyShell();
  await runStoreMigrationIfNeeded();
  await stemSelectionReady;
  refreshStemChoiceVisuals();
  refreshVocalModeToggleVisibility();
  await vocalSplitModeReady;
  for (const b of document.querySelectorAll(".vocal-mode-btn")) {
    b.setAttribute("aria-pressed", String(b.dataset.mode === vocalSplitMode));
  }
  // Before initCatalog: it runs the update check, which can itself notify.
  // collectDiagnostics is injected rather than imported by notifications.js,
  // which would make the two modules import each other.
  await initNotifications({ diagnostics: collectDiagnostics });
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
  const fmtOgg   = document.getElementById("t-fmt-ogg");
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
  // True from the click until the transfer starts or the dialog is cancelled.
  let picking = false;

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
    for (const [btn, val] of [[fmtWav, "wav"], [fmtMp3, "mp3"], [fmtFlac, "flac"], [fmtOgg, "ogg"], [fmtMp4, "mp4"]]) {
      btn?.classList.toggle("active", f === val);
      btn?.setAttribute("aria-checked", String(f === val));
    }
    applyFormatState();
  }
  fmtWav?.addEventListener("click", (e) => { e.stopPropagation(); setFormat("wav"); });
  fmtMp3?.addEventListener("click", (e) => { e.stopPropagation(); setFormat("mp3"); });
  fmtFlac?.addEventListener("click", (e) => { e.stopPropagation(); setFormat("flac"); });
  fmtOgg?.addEventListener("click", (e) => { e.stopPropagation(); setFormat("ogg"); });
  fmtMp4?.addEventListener("click", (e) => { e.stopPropagation(); setFormat("mp4"); });

  // MP4 exports the mix muxed with the source video. Stems and region have no
  // video equivalent, so they're hidden (via .fmt-mp4) while MP4 is selected,
  // leaving just "Export Mix".
  function applyFormatState() {
    const video = format === "mp4";
    exportPanel?.classList.toggle("fmt-mp4", video);
    if (mixDescEl) {
      mixDescEl.textContent = video ? t("export.mixDescVideo") : t("export.mixDesc");
    }
    if (!video) updateLoopRegionVisual(); // restores the region item's disabled state
  }
  // mixDescEl's text depends on `format`, not just the current language, so a
  // language switch needs to re-derive it rather than rely solely on the
  // generic data-i18n pass (which would otherwise reset it to the non-video
  // wording even while MP4 is selected).
  onLanguageChange(applyFormatState);

  function resetBusy() {
    busy = false;
    exportBtn?.classList.remove("is-busy");
    if (exportLabel) exportLabel.textContent = t("export.mix");
    // Clear every row, not just the ones enterBusy could see: it disables via
    // actionItems(), which filters on visibility, and it closes the panel, so
    // by the time this runs every row is hidden and a visibility-filtered clear
    // would clear nothing at all -- leaving the menu dead for the rest of the
    // session (#335).
    for (const it of [itemMix, itemStems, itemRegion]) it?.removeAttribute("aria-disabled");
    applyFormatState(); // re-derives the region row's genuine disabled state
  }

  // How long to hold the indeterminate state when the host reports nothing back.
  const EXPORT_FLASH_MS = 1200;
  // Safety net for the promise path: a pending invoke that somehow never settles
  // must not leave the menu disabled for the rest of the session (#335).
  const EXPORT_BUSY_MAX_MS = 15 * 60 * 1000;
  // Guards against a stale timer from a finished export resetting a later one.
  let busyToken = 0;

  // Show the busy state. Called when bytes actually start moving, which on
  // desktop is after the user has picked a destination -- not on click, or the
  // label would claim to be exporting for as long as the save dialog sat open
  // (#338).
  function enterBusy() {
    busy = true;
    exportBtn?.classList.add("is-busy");
    if (exportLabel) exportLabel.textContent = t("export.mixing");
    actionItems().forEach((it) => it?.setAttribute("aria-disabled", "true"));
    closePanel();
  }
  // Same reasoning as applyFormatState's listener above: exportLabel's text
  // depends on `busy`, so re-derive it after a language switch instead of
  // leaving it on whatever the generic data-i18n pass reset it to.
  onLanguageChange(() => {
    if (exportLabel) exportLabel.textContent = t(busy ? "export.mixing" : "export.mix");
  });

  // `pending` is whatever the download helper returned: a promise on desktop,
  // resolving once the file is written, or `true` in a browser, where an
  // <a download> is fire-and-forget and there is nothing to wait on. Only the
  // guess needs a fixed duration.
  //
  // `picking` covers the gap between the click and the transfer: the dialog is
  // app-modal so the menu is unreachable anyway, but the flag keeps a second
  // export from being queued behind it without lying about the label.
  // jobId is snapshotted by the caller at click time, not read live here:
  // settlement can take up to EXPORT_BUSY_MAX_MS, by which point the user may
  // have opened a different track, and currentJobId would then point at the
  // wrong one (#401).
  function settleBusy(pending, jobId) {
    const token = ++busyToken;
    const finish = () => {
      picking = false;
      if (token === busyToken) resetBusy();
    };

    if (!pending || typeof pending.then !== "function") {
      window.setTimeout(finish, EXPORT_FLASH_MS);
      return;
    }
    const backstop = window.setTimeout(finish, EXPORT_BUSY_MAX_MS);
    pending
      .then((ok) => {
        // ok === false means the save dialog was cancelled, not a real
        // export — nothing was resolved, so leave any failure notification
        // in place rather than clearing it on a no-op.
        if (jobId && ok !== false) dismissFailuresByJobId(jobId, "export");
      })
      .catch((err) => {
        // A cancelled dialog resolves false without ever entering the busy
        // state, so anything here is a real failure.
        const message = typeof err === "string" && err ? err : t("export.failed");
        showError(message, null, { retry: false });
        notifyFailure({
          kind: "export",
          message,
          detail: err instanceof Error ? String(err.message) : null,
          context: { stage: `Exporting ${format}`, jobId },
        });
      })
      .finally(() => {
        window.clearTimeout(backstop);
        finish();
      });
  }

  // Kick off an export: hand the helper a callback that flips the UI into its
  // busy state, then wait on the result.
  function runExport(start, emptyMessage) {
    if (busy || picking) return;
    picking = true;
    const jobId = currentJobId; // snapshot now -- see settleBusy's comment
    const pending = start(enterBusy);
    if (!pending) {
      picking = false;
      showError(emptyMessage, null, { retry: false });
      return;
    }
    settleBusy(pending, jobId);
  }

  exportBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (busy || picking) return;
    panelOpen() ? closePanel() : openPanel();
  });

  // Export Mix: MP4 produces the video; any other format an audio mix.
  itemMix?.addEventListener("click", (e) => {
    e.stopPropagation();
    runExport(
      (onStart) => (format === "mp4" ? downloadCurrentVideo(onStart) : downloadCurrentMix(format, onStart)),
      t("export.allMuted"),
    );
  });

  itemRegion?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (itemRegion.getAttribute("aria-disabled") === "true") return;
    runExport((onStart) => downloadRegionMix(format, onStart), t("export.allMuted"));
  });

  // All Stems = a single backend-built ZIP, named after the song. Audio-only,
  // so it's disabled (and inert) while MP4 is the selected format.
  itemStems?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (itemStems.getAttribute("aria-disabled") === "true") return;
    runExport((onStart) => downloadAllStemsZip(format, onStart), t("export.noStems"));
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
  // Inside the menu is not "away": ticking an option must not dismiss it. The
  // export panel carries two checkboxes (click track, count-in) that a user
  // may well want both of, and without this the first tick closed the menu and
  // the second needed it reopened. Rows that *should* close the menu do it
  // themselves -- the export actions via enterBusy() -> closePanel().
  exportPanel?.addEventListener("click", (e) => e.stopPropagation());
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

  const AUDIO_EXTS = [".mp3", ".wav", ".flac", ".mp4", ".m4a", ".ogg", ".opus"];
  const isAudioFile = (file) => AUDIO_EXTS.some((ext) => file.name.toLowerCase().endsWith(ext));

  function applyFiles(fileList) {
    const all = [...(fileList || [])];
    if (!all.length) return;

    // Filter here rather than letting the server reject each one: dropping a
    // folder, or a folder of mixed content, would otherwise mean one 422 per
    // stray file. Only complain if nothing usable came through.
    const audio = all.filter(isAudioFile);
    if (!audio.length) {
      showError(t("upload.unsupportedFormat"));
      return;
    }
    const files = audio.filter((f) => f.size <= MAX_UPLOAD_BYTES);
    const oversized = audio.length - files.length;
    if (!files.length) {
      showError(t("upload.fileTooLarge", { size: formatBytes(audio[0].size), max: formatBytes(MAX_UPLOAD_BYTES) }));
      return;
    }

    const skipped = all.length - files.length;
    if (fileName) {
      fileName.textContent =
        files.length === 1 ? files[0].name : `${files.length} files`;
    }
    if (fileSize) {
      const bytes = files.reduce((sum, f) => sum + f.size, 0);
      fileSize.textContent = formatBytes(bytes);
    }
    filePill.classList.remove("hidden");
    urlWrap.classList.add("has-file");
    // Cache the File objects directly on the element so job.js can always
    // retrieve them even after the browser clears fileInput.files following
    // a fetch() submission (known WKWebView / Chromium behaviour). _file stays
    // as the first one so any older single-file reader keeps working.
    fileInput._files = files;
    fileInput._file = files[0];
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    fileInput.files = dt.files;
    urlInput.value = "";
    urlInput.removeAttribute("required");

    if (skipped > 0) {
      const reason = t(oversized > 0 ? "upload.reasonTooLarge" : "upload.reasonNotAudio");
      showError(plural("upload.skippedFiles", skipped, { reason }), null, {
        retry: false,
      });
    }
  }

  function clearFile() {
    filePill.classList.add("hidden");
    urlWrap.classList.remove("has-file");
    fileInput._file = null;
    fileInput._files = null;
    fileInput.value = "";
    urlInput.setAttribute("required", "");
  }

  fileClear?.addEventListener("click", clearFile);
  // Exposed on the element, same convention as _file above, so job.js can drop
  // the selection once the upload has been handed to the server. Without it the
  // chip stays armed and the (now immediately re-enabled) Process button will
  // happily import the same file twice.
  fileInput._clear = clearFile;

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
    applyFiles(e.dataTransfer.files);
  });

  fileInput.addEventListener("change", () => {
    applyFiles(fileInput.files);
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
  } else if (e.code === "KeyK") {
    e.preventDefault();
    toggleMetronome();
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
    const invoke = window.__TAURI__?.core?.invoke;
    // A download attribute is meaningless to the OS handler, so open_url used to
    // hand the stem to a browser or media player instead of saving it. Save it
    // like every other export, which also honours the song-prefixed name (#336).
    // Placeholder rows for absent stems keep href="#" and carry no name.
    if (invoke && dl.download && !dl.getAttribute("href").endsWith("#")) {
      e.preventDefault();
      invoke("save_audio_file", { url: dl.href, filename: dl.download });
      return;
    }
    if (invoke) {
      e.preventDefault();
      invoke("open_url", { url: dl.href });
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
