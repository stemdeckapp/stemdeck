import { fmtTime, fmtTickLabel } from "./utils.js";
import {
  playBtn, playMiniBtn, stopBtn, loopBtn, timeEl, masterFader,
  rulerTime, wavesGrid, loopRegionEl, playheadMarker,
  multitrack, totalDuration, loopEnabled, loopStart, loopEnd, masterVolume,
  waveScroll, waveCanvas,
  presenceRulerEl, presencePlayheadEl,
  footerTimeElapsed, footerTimeTotal, npScrubFill, footerWaveDrawFn,
  setLoopEnabled, setLoopStart, setLoopEnd, setMasterVolume,
} from "./state.js";
import { applyMix } from "./mixer.js";

const MIN_LOOP_SEC = 0.2;
// rulerTime is the canonical timeline reference for both click->time
// and time->pixel mapping. The wave-editor lays the ruler and the
// waveform body out so they should be horizontally aligned (both gutter
// 48 px on the left in studio mode), but using one element for both
// halves of the round-trip eliminates any subtle CSS drift -- clicking
// "1:00" on the ruler always lands a marker exactly under that tick,
// regardless of how the waves layer below happens to size itself.
function rulerRect() {
  return rulerTime?.getBoundingClientRect() || { left: 0, width: 1 };
}

function loopOverlayParent() {
  return document.querySelector(".waves-column") || rulerTime;
}

function ensureLoopRegionParent() {
  const parent = loopOverlayParent();
  if (parent && loopRegionEl.parentElement !== parent) {
    parent.appendChild(loopRegionEl);
  }
}

function timeFromClientX(clientX) {
  if (!totalDuration) return null;
  const rect = rulerRect();
  const x = clientX - rect.left;
  const frac = Math.max(0, Math.min(1, x / Math.max(1, rect.width)));
  return frac * totalDuration;
}

function setPlayheadTime(sec) {
  if (!multitrack || !totalDuration) return;
  const next = Math.max(0, Math.min(totalDuration, sec));
  multitrack.setTime(next);
  updatePlayheadMarker(next);
  updateFooterTimes(next);
  updatePresencePlayhead(next);
}

export function buildRuler(durationSec) {
  rulerTime.innerHTML = "";
  wavesGrid.innerHTML = "";
  const marker = document.createElement("div");
  marker.className = "playhead-marker";
  marker.setAttribute("aria-hidden", "true");
  marker.innerHTML =
    '<svg viewBox="0 0 10 10" width="10" height="10"><polygon points="0,0 10,0 5,8" fill="#e54e4e"></polygon></svg>';
  rulerTime.appendChild(marker);

  if (!durationSec || durationSec <= 0) return;
  const step = durationSec < 90 ? 15 : durationSec < 300 ? 30 : 60;
  for (let t = 0; t <= durationSec; t += step) {
    const leftPct = (t / durationSec) * 100;
    const tick = document.createElement("div");
    tick.className = "tick";
    tick.style.left = `${leftPct}%`;
    tick.innerHTML = `<span class="tick-label">${fmtTickLabel(t)}</span>`;
    rulerTime.appendChild(tick);

    const grid = document.createElement("div");
    grid.className = "grid-line";
    grid.style.left = `${leftPct}%`;
    wavesGrid.appendChild(grid);
  }
}

export function updatePlayheadMarker(currentSec) {
  if (!playheadMarker || !totalDuration) return;
  const m = rulerTime.querySelector(".playhead-marker");
  if (m) {
    // Position relative to the ruler itself (the marker is a ruler
    // child) so the playhead always sits exactly under the tick at
    // the matching time. Use percent instead of px: app-level CSS
    // zoom scales getBoundingClientRect() values, while left/width
    // styles are interpreted in unzoomed layout pixels.
    const pct = Math.max(0, Math.min(100, (currentSec / totalDuration) * 100));
    m.style.left = `${pct}%`;
  }
}

// Mirror the elapsed/total time into the transport-footer's two side
// labels (which used to show hardcoded "00:00.000" / "03:38.000") and
// drive the small scrub bar in the now-playing card. Driven from the
// same wavesurfer "timeupdate" event that already updates #t-time, so
// every label stays in sync without extra event plumbing.
export function updateFooterTimes(currentSec) {
  if (!totalDuration) return;
  if (footerTimeElapsed) footerTimeElapsed.textContent = fmtTime(currentSec);
  if (footerTimeTotal) footerTimeTotal.textContent = fmtTime(totalDuration);
  const pct = Math.max(0, Math.min(100, (currentSec / totalDuration) * 100));
  if (npScrubFill) npScrubFill.style.width = `${pct}%`;
  footerWaveDrawFn?.(pct / 100);
}

// Build the presence-panel ruler labels from the actual track duration.
// The HTML ships 8 placeholder <b> tags ("0:00 ... 3:38"); we replace
// each label's text with a tick at evenly-spaced fractions of the song.
export function buildPresenceRuler(durationSec) {
  if (!presenceRulerEl) return;
  const ticks = presenceRulerEl.querySelectorAll("b");
  if (!ticks.length) return;
  if (!durationSec || durationSec <= 0) {
    for (const t of ticks) t.textContent = "0:00";
    return;
  }
  // 8 ticks -- evenly distribute from 0 to duration.
  const n = ticks.length;
  for (let i = 0; i < n; i++) {
    const frac = i / (n - 1);
    ticks[i].textContent = fmtTickLabel(frac * durationSec);
  }
}

// Move the gold playhead line that overlays the presence-bars panel.
// Uses left% within the .presence-bars container, which spans the full
// duration -- matches the ruler ticks above it.
export function updatePresencePlayhead(currentSec) {
  if (!presencePlayheadEl) return;
  if (!totalDuration || totalDuration <= 0) {
    presencePlayheadEl.classList.add("hidden");
    return;
  }
  const pct = Math.max(0, Math.min(100, (currentSec / totalDuration) * 100));
  presencePlayheadEl.style.left = `${pct}%`;
  presencePlayheadEl.classList.remove("hidden");
}

export function updateLoopRegionVisual() {
  const regionBtn = document.getElementById("t-region-btn");
  const hasRegion = loopEnabled && totalDuration > 0 && loopEnd > loopStart;
  if (regionBtn) regionBtn.disabled = !hasRegion;
  if (!loopEnabled || !totalDuration) {
    loopRegionEl.classList.add("hidden");
    return;
  }
  ensureLoopRegionParent();
  // Keep the loop overlay in the same normalized timeline coordinate
  // system as the ruler ticks. Percentages avoid CSS zoom mismatch:
  // pointer coordinates and getBoundingClientRect() are visual pixels,
  // but style.left/style.width in px are unzoomed layout pixels.
  const startPct = Math.max(0, Math.min(100, (loopStart / totalDuration) * 100));
  const endPct = Math.max(0, Math.min(100, (loopEnd / totalDuration) * 100));
  loopRegionEl.style.left = `${startPct}%`;
  loopRegionEl.style.width = `${Math.max(0, endPct - startPct)}%`;
  loopRegionEl.classList.remove("hidden");
}

// Standard DAW transport state machine:
//   [stopped]  (paused at start)  ─Play→  [playing]
//        ↑                                  ↓ Play
//      Stop                                [paused]  (paused mid-track)
//                       Stop ↓
//                          [stopped]
//
// Play button is a Play/Pause toggle. Stop both pauses and returns the
// playhead to 0 (or loopStart if loop is on). Visual state is driven
// from the multitrack lifecycle events in player.js (mt.on play/pause/
// timeupdate) — click handlers only mutate the transport, never the
// button's CSS class. That way manual seeks (e.g. clicking the ruler)
// keep the button states in sync without extra plumbing.
// WKWebView (Tauri desktop) has small audio buffers. After a seek, all
// audio elements drop their buffers and issue new range requests simultaneously.
// Calling play() before they reach HAVE_FUTURE_DATA (readyState >= 3) causes
// choppiness. Wait for all elements to be ready, with a hard 1.5 s fallback so
// the user is never stuck. Desktop browsers buffer aggressively enough that
// this wait is skipped entirely (readyState is already >= 3 by the time play
// is pressed after a seek).
function _playWhenReady() {
  if (!multitrack) return;
  const inTauri = Boolean(window.__TAURI__?.core?.invoke);
  if (!inTauri) { multitrack.play(); return; }

  const audios = (multitrack.audios ?? [])
    .filter((a) => a instanceof HTMLMediaElement && a.src);
  const notReady = audios.filter((a) => a.readyState < 3);
  if (!notReady.length) { multitrack.play(); return; }

  let fired = false;
  const fire = () => { if (!fired && multitrack && !multitrack.isPlaying()) { fired = true; multitrack.play(); } };
  const waits = notReady.map((a) => new Promise((res) => {
    if (a.readyState >= 3) { res(); return; }
    const onReady = () => { a.removeEventListener("canplay", onReady); res(); };
    a.addEventListener("canplay", onReady);
  }));
  Promise.all(waits).then(fire);
  window.setTimeout(fire, 1500);
}

export function togglePlayPause() {
  if (!multitrack) return;
  if (multitrack.isPlaying()) {
    multitrack.pause();
    return;
  }
  const ctx = multitrack.audioContext;
  // Safari requires play() to be called synchronously within the user-gesture
  // handler. Resume the AudioContext fire-and-forget so the context becomes
  // live, then call play() immediately on the same tick.
  if (ctx && ctx.state === "suspended") {
    ctx.resume().catch(() => {});
  }
  // Snap playhead to loopStart on play (DAW convention).
  if (loopEnabled && totalDuration > 0) {
    multitrack.setTime(loopStart);
  }
  _playWhenReady();
}

export function stopTransport() {
  if (!multitrack) return;
  multitrack.pause();
  multitrack.setTime(loopEnabled ? loopStart : 0);
}

export function toggleLoop() {
  setLoopEnabled(!loopEnabled);
  loopBtn.classList.toggle("active", loopEnabled);
  updateLoopRegionVisual();
}

// Click-drag on the timeline ruler or waveform body to define the loop
// region. Drag direction doesn't matter -- start and end get sorted.
// Tiny drags are treated as clicks and seek the playhead instead.
function wireLoopDrag() {
  let dragging = false;
  let dragStartTime = 0;
  let activePointerId = null;
  let moved = false;

  const startDrag = (e, surface) => {
    if (e.button !== 0 || e.target.closest(".loop-region")) return;
    const t = timeFromClientX(e.clientX);
    if (t === null) return;
    dragging = true;
    activePointerId = e.pointerId;
    moved = false;
    dragStartTime = t;
    setLoopStart(t);
    setLoopEnd(t);
    setLoopEnabled(true);
    loopBtn.classList.add("active");
    updateLoopRegionVisual();
    surface.setPointerCapture(e.pointerId);
    e.preventDefault();
  };

  const moveDrag = (e) => {
    if (!dragging || e.pointerId !== activePointerId) return;
    const t = timeFromClientX(e.clientX);
    if (t === null) return;
    if (Math.abs(t - dragStartTime) >= MIN_LOOP_SEC) moved = true;
    if (t < dragStartTime) {
      setLoopStart(t);
      setLoopEnd(dragStartTime);
    } else {
      setLoopStart(dragStartTime);
      setLoopEnd(t);
    }
    updateLoopRegionVisual();
  };

  const finishDrag = (e) => {
    if (!dragging || e.pointerId !== activePointerId) return;
    dragging = false;
    activePointerId = null;
    const clicked = !moved || loopEnd - loopStart < MIN_LOOP_SEC;
    if (clicked) {
      setLoopEnabled(false);
      loopBtn.classList.remove("active");
      updateLoopRegionVisual();
      setPlayheadTime(dragStartTime);
    }
  };

  const wavesColumn = document.querySelector(".waves-column");
  const surfaces = [rulerTime, wavesColumn].filter(Boolean);
  for (const surface of surfaces) {
    surface.addEventListener("pointerdown", (e) => {
      if (surface === rulerTime && e.target !== rulerTime) return;
      startDrag(e, surface);
    });
    surface.addEventListener("pointermove", moveDrag);
    surface.addEventListener("pointerup", finishDrag);
    surface.addEventListener("pointercancel", finishDrag);
  }
}

// ─── Zoom ───
//
// Single CSS variable `--zoom` on .wave-canvas drives the visual width
// (canvas = 100% * zoom). Multitrack's pxPerSec is set to match so its
// internal canvases stay the exact same pixel width as the canvas; that
// way the bundle never adds its own internal horizontal scroll, which
// historically broke alignment with our ruler/loop overlay.
//
// All percentage-positioned children (ruler ticks, playhead, grid lines,
// loop region) automatically stretch with the canvas, so the loop drag
// math stays correct without any per-element width logic.

export function applyWaveZoom() {
  const wavesColumn = document.querySelector(".waves-column");
  if (wavesColumn) {
    const lanes = document.getElementById("lanes") || waveCanvas;
    lanes?.style.setProperty("--wave-playhead-h", `${wavesColumn.clientHeight}px`);
  }
  if (multitrack && totalDuration > 0 && waveScroll) {
    const baseWidth = waveScroll.clientWidth;
    if (baseWidth > 0) {
      const pxPerSec = baseWidth / totalDuration;
      try { multitrack.zoom(pxPerSec); } catch { /* ignore -- pre-canplay */ }
    }
  }
}

function wireZoomButtons() {
  if (waveScroll) {
    let rafId = null;
    const ro = new ResizeObserver(() => {
      if (!multitrack || totalDuration <= 0) return;
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => { rafId = null; applyWaveZoom(); });
    });
    ro.observe(waveScroll);
  }
  if (waveScroll) {
    waveScroll.addEventListener("wheel", (e) => {
      if (waveScroll.scrollWidth <= waveScroll.clientWidth) return;
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault();
        waveScroll.scrollLeft += e.deltaY;
      }
    }, { passive: false });
  }
  applyWaveZoom();
}

// ─── Wire transport buttons ───

export function wireTransportButtons() {
  playBtn.addEventListener("click", togglePlayPause);
  playMiniBtn?.addEventListener("click", togglePlayPause);
  stopBtn.addEventListener("click", stopTransport);
  loopBtn.addEventListener("click", toggleLoop);
  wireLoopDrag();
  wireZoomButtons();
  masterFader?.addEventListener("input", () => {
    setMasterVolume(parseFloat(masterFader.value));
    applyMix();
  });
  masterFader?.addEventListener("dblclick", () => {
    masterFader.value = "0.5";
    setMasterVolume(0.5);
    applyMix();
  });
}
