// A video track's preview: a small window inside the app, over the studio,
// showing the track's camera take at the playhead, like an editor's program
// monitor. Dragged by its top bar, resized from its corner, closed with the X
// in the middle of the top bar (or Escape while it has focus). Its place and
// size are remembered per browser.
//
// The picture follows the transport: playing, the video plays along and is
// pulled back when it drifts; stopped or scrubbing, it shows the frame at the
// playhead. Silent: the take's sound is a lane of the mix already.

import { t, onLanguageChange } from "../i18n.js";
import { playbackSpeed } from "../state.js";
import { transport } from "../transport.js";
import { getTracks } from "../capture/takeLanes.js";
import { videoTakeOf } from "./videoLane.js";
import { onModeChange } from "../mode.js";

const RECT_KEY = "stemdeck.videoPreview.rect";
// Playing, the picture is put back when it is this far from the playhead.
const DRIFT_SEC = 0.15;
// Stopped, the frame shown is the playhead's within this much.
const STILL_SEC = 0.04;

let win = null;
let ui = null;
let trackId = null;
let raf = 0;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function readRect() {
  try {
    const r = JSON.parse(localStorage.getItem(RECT_KEY) || "null");
    if (r && [r.left, r.top, r.width, r.height].every(Number.isFinite)) return r;
  } catch (e) {
    console.warn("[video] reading the preview's place failed:", e);
  }
  return null;
}

function saveRect() {
  if (!win) return;
  const b = win.getBoundingClientRect();
  try {
    localStorage.setItem(RECT_KEY, JSON.stringify({ left: b.left, top: b.top, width: b.width, height: b.height }));
  } catch (e) {
    console.warn("[video] saving the preview's place failed:", e);
  }
}

/** Keep the window on screen, whatever was remembered or dragged. */
function clampToViewport() {
  const b = win.getBoundingClientRect();
  const maxLeft = Math.max(0, window.innerWidth - Math.min(b.width, window.innerWidth));
  const maxTop = Math.max(0, window.innerHeight - 40);
  win.style.left = `${Math.min(Math.max(0, b.left), maxLeft)}px`;
  win.style.top = `${Math.min(Math.max(0, b.top), maxTop)}px`;
}

function build() {
  win = el("div", "video-preview");
  win.setAttribute("role", "dialog");
  win.tabIndex = -1;
  const bar = el("div", "video-preview-bar");
  const title = el("span", "video-preview-title");
  const close = el("button", "video-preview-close", "×");
  close.type = "button";
  bar.append(title, close);
  const stage = el("div", "video-preview-stage");
  const video = el("video", "video-preview-video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  const empty = el("p", "video-preview-empty");
  stage.append(video, empty);
  win.append(bar, stage);
  document.body.append(win);
  ui = { bar, title, close, video, empty };

  const r = readRect();
  win.style.width = `${r?.width || 400}px`;
  win.style.height = `${r?.height || 262}px`;
  win.style.left = `${r?.left ?? window.innerWidth - 440}px`;
  win.style.top = `${r?.top ?? window.innerHeight - 420}px`;
  clampToViewport();

  close.addEventListener("click", (e) => { e.stopPropagation(); closePreview(); });
  win.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); closePreview(); }
  });
  for (const type of ["click", "pointerdown"]) win.addEventListener(type, (e) => e.stopPropagation());

  // Drag by the bar (not by its X).
  bar.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target === close) return;
    e.preventDefault();
    const b = win.getBoundingClientRect();
    const dx = e.clientX - b.left;
    const dy = e.clientY - b.top;
    bar.setPointerCapture(e.pointerId);
    const move = (ev) => {
      win.style.left = `${ev.clientX - dx}px`;
      win.style.top = `${ev.clientY - dy}px`;
      clampToViewport();
    };
    const up = () => {
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", up);
      bar.removeEventListener("pointercancel", up);
      saveRect();
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up);
    bar.addEventListener("pointercancel", up);
  });
  // Resized from its corner (CSS resize): remember the size it ends at.
  new ResizeObserver(() => { if (win && !win.hidden) saveRect(); }).observe(win);
  window.addEventListener("resize", () => { if (win && !win.hidden) clampToViewport(); });
  onLanguageChange(relabel);
}

function relabel() {
  if (!ui) return;
  const track = getTracks().find((tr) => tr.id === trackId);
  const label = t("video.previewTitle", { name: track?.name || "" });
  // Called every frame: only a change touches the page.
  if (ui.title.textContent === label && ui.close.title === t("video.previewClose")) return;
  ui.title.textContent = label;
  win.setAttribute("aria-label", label);
  ui.close.title = t("video.previewClose");
  ui.close.setAttribute("aria-label", t("video.previewClose"));
}

function showEmpty(key) {
  ui.video.hidden = true;
  ui.empty.hidden = false;
  ui.empty.textContent = t(key);
  if (!ui.video.paused) ui.video.pause();
}

function tick() {
  raf = 0;
  if (!win || win.hidden) return;
  const track = getTracks().find((tr) => tr.id === trackId);
  // The track was deleted: nothing left to preview.
  if (!track) { closePreview(); return; }
  relabel();
  const take = videoTakeOf(trackId);
  if (!take) {
    showEmpty("video.previewNoTake");
  } else {
    if (ui.video.dataset.src !== take.camera_url) {
      ui.video.dataset.src = take.camera_url;
      ui.video.src = take.camera_url;
    }
    const tx = transport();
    const song = tx?.getCurrentTime?.() ?? 0;
    const at = song - take.camera_start_sec;
    const dur = Number.isFinite(ui.video.duration) ? ui.video.duration : Infinity;
    if (at < 0 || at > dur) {
      showEmpty("video.previewNoPicture");
    } else {
      ui.video.hidden = false;
      ui.empty.hidden = true;
      const playing = !!tx?.isPlaying?.();
      const rate = playbackSpeed > 0 ? playbackSpeed : 1;
      if (ui.video.playbackRate !== rate) ui.video.playbackRate = rate;
      if (playing) {
        if (Math.abs(ui.video.currentTime - at) > DRIFT_SEC) ui.video.currentTime = at;
        if (ui.video.paused) ui.video.play().catch(() => {});
      } else {
        if (!ui.video.paused) ui.video.pause();
        if (Math.abs(ui.video.currentTime - at) > STILL_SEC) ui.video.currentTime = at;
      }
    }
  }
  raf = requestAnimationFrame(tick);
}

/** Open (or move to) the preview of a video track. */
export function openPreview(id) {
  if (!win) build();
  trackId = id;
  win.hidden = false;
  relabel();
  win.focus();
  if (!raf) raf = requestAnimationFrame(tick);
}

export function closePreview() {
  if (!win) return;
  win.hidden = true;
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
  ui.video.pause();
  ui.video.removeAttribute("src");
  delete ui.video.dataset.src;
  ui.video.load();
  trackId = null;
}

// Practice mode has no video tracks' tools: the preview closes with them.
onModeChange((m) => { if (m === "practice") closePreview(); });

/** The track the preview shows, or null when it is closed. */
export function previewTrackId() {
  return win && !win.hidden ? trackId : null;
}
