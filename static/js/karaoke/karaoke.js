// Karaoke Mode: a workspace view over the studio, entered from the Mode
// menu (modeUi.js), that shows the open song's lyrics big,
// a queue panel, and Key and Stems controls, plus a fullscreen stage.
//
// It is a view, not a second player. Every control reads and writes the
// studio's own state: the key through
// transport.setGlobalPitch / resetAllKeys, each stem through the mixer's mute
// (mixer.toggleStemMute, so the lane's M follows), the position through
// setPlayheadTime. Nothing is copied: one animation loop, running only while
// Karaoke Mode is open, reads all of it each frame and repaints what changed,
// so a change made in the transport or the mixer shows here at once and the
// other way round.
//
// The studio stays laid out underneath, untouched and inert: this view is a
// fixed layer over the lanes area (#lanes and the master drawer, above the
// footer), sized from their own boxes. Hiding the lanes instead would make
// everything that measures them see a zero size.

import { t, onLanguageChange } from "../i18n.js";
import { audioEngine, currentJobId, mixerState, trackIndex, totalDuration, keyChip } from "../state.js";
import {
  transport, setPlayheadTime, togglePlayPause, stopTransport, setGlobalPitch, getGlobalPitch, resetAllKeys,
} from "../transport.js";
import { toggleStemMute, refreshMixerVisuals, applyMix, persistMix } from "../mixer.js";
import { laneLabel, STEM_COLORS } from "../constants.js";
import { karaokeLyrics, vocalEnvelopeFor } from "../lyrics.js";
import { getCurrentTrackInfo, openTrackInStudio } from "../catalog.js";
import { loadQueue, getQueue, shiftQueue, removeFromQueue, onQueueChange, mountQueue } from "./karaokeQueue.js";
import { isRecording } from "../capture/recordUi.js";
import { layoutState, changeLayout, requestSplit, onLayoutChange } from "../modeStems.js";
import { splitFailedText, vocalSplitUnavailable, onVocalSplitAvailability } from "../vocalSplitState.js";
import { takeOpen as streamerTakeOpen } from "../streamer/streamerRecord.js";
import {
  transposedKey, semitoneLabel, clockShort, clockLong,
  sungLines, lyricFrame, timedWords, sungCount, karaokeStems, karaokeMutedByDefault, karaokeAlwaysOn,
} from "./karaokeModel.js";

const FS_IDLE_MS = 3000;
const SEEK_STEP = 5;

let root = null; // the windowed view, #karaoke
let fs = null; // the fullscreen stage, built on first use
let open = false;
let raf = 0;
const listeners = new Set();

// The song's mix in Karaoke Mode is Karaoke's own (mixMode.js, mixer.js): it
// has a saved record of its own for every song, apart from Practice's and
// Advanced's, so what is done here stays here and what is done there is not
// seen here. Only the voice is not remembered: it is off every time.
let shownJob = null;

// Lyrics of the shown song: sung lines, their words' timings, the vocals'
// level that places words (null until it arrives).
let lyr = { trackId: null, lines: [], words: [], plain: false, loading: false };
let lyrToken = 0;
let envelope = null;

// What was last painted, so a frame repaints only what changed.
const painted = {};

// ── Small DOM helpers ──

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function button(cls, label, text) {
  const b = el("button", cls, text);
  b.type = "button";
  if (label) b.setAttribute("aria-label", label);
  return b;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function icon(paths, { size = 20, fill = "none", stroke = "currentColor", width = 2 } = {}) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", fill);
  svg.setAttribute("stroke", stroke);
  svg.setAttribute("stroke-width", width);
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const p of paths) {
    const [tagName, attrs] = typeof p === "string" ? ["path", { d: p }] : p;
    const node = document.createElementNS(SVG_NS, tagName);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    svg.append(node);
  }
  return svg;
}

const ICONS = {
  fullscreen: ["M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"],
  exitFullscreen: ["M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"],
  minus: ["M5 12h14"],
  plus: ["M5 12h14M12 5v14"],
  play: [["path", { d: "M7 4.5v15l12-7.5z", fill: "currentColor", stroke: "none" }]],
  stop: [["rect", { x: 6, y: 6, width: 12, height: 12, rx: 1.5, fill: "currentColor", stroke: "none" }]],
  next: [["path", { d: "M5 5v14l10-7z", fill: "currentColor", stroke: "none" }], "M18 5v14"],
  pause: [["rect", { x: 6, y: 5, width: 4, height: 14, rx: 1, fill: "currentColor", stroke: "none" }], ["rect", { x: 14, y: 5, width: 4, height: 14, rx: 1, fill: "currentColor", stroke: "none" }]],
  wave: ["M4 10v4M8 7v10M12 4v16M16 8v8M20 10v4"],
  kebab: [["circle", { cx: 12, cy: 5, r: 1.7, fill: "currentColor", stroke: "none" }], ["circle", { cx: 12, cy: 12, r: 1.7, fill: "currentColor", stroke: "none" }], ["circle", { cx: 12, cy: 19, r: 1.7, fill: "currentColor", stroke: "none" }]],
  queue: ["M4 6h10M4 12h10M4 18h6M16 15l3 3 3-3M19 18V9"],
  mic: [["rect", { x: 9, y: 3, width: 6, height: 11, rx: 3 }], "M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8"],
  micOff: [["rect", { x: 9, y: 3, width: 6, height: 11, rx: 3 }], "M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8M3 3l18 18"],
  backing: [["circle", { cx: 9, cy: 8, r: 3 }], "M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6", ["circle", { cx: 17, cy: 9, r: 2.5 }], "M16.5 14.2c2.6.5 4.5 2.9 4.5 5.8"],
  drums: [["ellipse", { cx: 12, cy: 10, rx: 8, ry: 3 }], "M4 10v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6M7 3l4 5M17 3l-4 5"],
  bass: ["M14 10l6-6M18 2l4 4M17 3l1 1M20 6l1 1", "M13.5 10.5a3 3 0 0 0-3.5-.5l-1 .9a4 4 0 0 0-4.6 1C2.6 13.7 3 16.6 5 18.8s5.1 2.6 6.9.8a4 4 0 0 0 1-4.6l.9-1a3 3 0 0 0-.3-3.5z", "M8 16l2-2"],
  guitar: ["M14 10l5-5M18 3l3 3", "M13.5 10.5a3 3 0 0 0-3.5-.5l-1 .9a4 4 0 0 0-4.6 1C2.6 13.7 3 16.6 5 18.8s5.1 2.6 6.9.8a4 4 0 0 0 1-4.6l.9-1a3 3 0 0 0-.3-3.5z", ["circle", { cx: 9, cy: 15, r: 1.4 }]],
  piano: [["rect", { x: 3, y: 4, width: 18, height: 16, rx: 2 }], "M8 13v7M12 13v7M16 13v7M6.5 4v9h3V4M14.5 4v9h3V4"],
  other: ["M3 12h3l2-6 3 12 3-9 2 6 1-3h4"],
  // A music note: the backing track, what is left after the stems taken out.
  original: ["M9 18V6l10-2v12", ["circle", { cx: 6.5, cy: 18, r: 2.5 }], ["circle", { cx: 16.5, cy: 16, r: 2.5 }]],
};
const STEM_ICON = { lead_vocals: "mic", vocals: "mic", backing_vocals: "backing", original: "original", drums: "drums", bass: "bass", guitar: "guitar", piano: "piano", other: "other" };

// ── State reads (the player's own; never copied) ──

function now() {
  const tx = transport();
  const v = tx?.getCurrentTime?.();
  return Number.isFinite(v) ? v : 0;
}

function isPlaying() {
  const tx = transport();
  return Boolean(tx?.isPlaying?.());
}

function presentLanes() {
  return new Set(Object.keys(trackIndex || {}));
}

function trackMeta() {
  const title = document.getElementById("title")?.textContent?.trim() || "";
  const artistEl = document.getElementById("np-artist");
  const artist = artistEl && !artistEl.hidden ? artistEl.textContent.trim() : "";
  const img = document.getElementById("np-thumb");
  const art = img && img.getAttribute("src") && !img.hidden && img.naturalWidth > 0 ? img.currentSrc || img.src : "";
  return { title: title === "—" ? "" : title, artist, art };
}

function keyText() {
  return keyChip?.textContent || "";
}

// ── Build ──

let ui = null;

function buildStage(variant) {
  const stage = el("div", `kk-stage kk-stage-${variant}`);
  const bg = el("div", "kk-bg");
  const bgArt = el("img", "kk-bg-art");
  bgArt.alt = "";
  bgArt.referrerPolicy = "no-referrer";
  bgArt.hidden = true;
  bg.append(el("div", "kk-bg-lights"), bgArt);
  stage.append(bg, el("div", "kk-veil"));
  return { stage, bgArt };
}

function buildArt(cls) {
  const box = el("div", cls);
  const img = el("img", "kk-art-img");
  img.alt = "";
  img.referrerPolicy = "no-referrer";
  img.hidden = true;
  box.append(img);
  return { box, img };
}

/** The lyric rows, shared by the windowed and the fullscreen stage. */
function buildLyrics(variant) {
  const wrap = el("div", `kk-lyrics kk-lyrics-${variant}`);
  const prev = el("div", "kk-prev");
  const intro = el("div", "kk-intro");
  intro.hidden = true;
  const introTitle = el("div", "kk-intro-title");
  const introCount = el("div", "kk-intro-count");
  const dots = el("div", "kk-dots");
  for (let i = 0; i < 3; i++) dots.append(el("span", "kk-dot"));
  intro.append(introTitle, introCount, dots);
  const current = el("div", "kk-current");
  current.setAttribute("aria-live", "off");
  const next = el("div", "kk-next");
  const empty = el("div", "kk-empty");
  empty.hidden = true;
  const emptyText = el("p", "kk-empty-text");
  const emptyBtn = button("kk-btn kk-empty-btn", null, "");
  emptyBtn.addEventListener("click", () => {
    if (fsActive()) exitFullscreen();
    document.querySelector(".rail-lyrics")?.click();
  });
  empty.append(emptyText, emptyBtn);
  wrap.append(prev, intro, current, next, empty);
  return { wrap, prev, intro, introTitle, introCount, dots: [...dots.children], current, next, empty, emptyText, emptyBtn, phraseKey: null, words: [] };
}

function buildWindowed() {
  root = document.getElementById("karaoke");
  if (!root) return false;
  root.replaceChildren();
  root.setAttribute("aria-label", t("karaoke.regionAria"));

  const top = el("div", "kk-top");

  // Stage
  const { stage, bgArt } = buildStage("window");
  const inner = el("div", "kk-stage-inner");
  const head = el("div", "kk-stage-head");
  const art = buildArt("kk-art");
  const titles = el("div", "kk-titles");
  const title = el("div", "kk-title");
  const artist = el("div", "kk-artist");
  titles.append(title, artist);
  const fsBtn = button("kk-icon-btn kk-fs-btn", t("karaoke.fullscreenEnter"));
  fsBtn.append(icon(ICONS.fullscreen));
  fsBtn.addEventListener("click", () => enterFullscreen());
  head.append(art.box, titles, fsBtn);
  const lyrics = buildLyrics("window");
  const progress = buildProgress();
  inner.append(head, lyrics.wrap, progress.wrap);
  stage.append(inner);

  // Side panel: the queue alone. (Sections are edited on the studio's ribbon;
  // Karaoke does not list them.)
  const side = el("aside", "kk-side");
  const sideTitle = el("div", "kk-side-title");
  const queueList = el("div", "kk-list kk-queue");
  const queue = mountQueue(queueList, { onPlay: (id) => playQueued(id) });
  side.append(sideTitle, queueList);
  // Narrow workspaces fold the panel to a strip; its button opens it as a drawer.
  const strip = el("div", "kk-side-strip");
  const stripQueue = button("kk-icon-btn", null);
  stripQueue.append(icon(ICONS.queue));
  stripQueue.addEventListener("click", () => toggleDrawer());
  strip.append(stripQueue);

  top.append(stage, side, strip);

  // Controls: Transport, Key, Stems (the app's footer is hidden while open)
  const controls = el("div", "kk-controls");
  const play = buildTransportPanel();
  const key = buildKeyPanel();
  const stems = el("div", "kk-panel kk-stems");
  const stemsLabel = el("span", "kk-label");
  const stemRow = el("div", "kk-stem-row");
  // The voice form, Karaoke's own stem choice (modeStems.js): Backing vocals
  // in the voice (one Vocals card) or separate (Lead and Backing cards).
  const stemsHead = el("div", "kk-stems-head");
  const voice = buildVoiceToggle();
  stemsHead.append(stemsLabel, voice.root);
  stems.append(stemsHead, stemRow);
  controls.append(play.panel, key.panel, stems);

  // No song open: a quiet note over the whole view.
  const noTrack = el("div", "kk-no-track");
  noTrack.hidden = true;

  root.append(top, controls, noTrack);

  ui = {
    stage, bgArt, art, title, artist, fsBtn, lyrics, progress,
    side, sideTitle, queueList, queue, strip, stripQueue,
    play, key, stemsLabel, stemRow, noTrack, stemCards: new Map(), voice,
  };
  relabel();
  return true;
}

function buildProgress() {
  const wrap = el("div", "kk-progress");
  const bar = el("div", "kk-bar");
  bar.setAttribute("role", "slider");
  bar.tabIndex = 0;
  bar.setAttribute("aria-valuemin", "0");
  const track = el("div", "kk-bar-track");
  const fill = el("div", "kk-bar-fill");
  const thumb = el("div", "kk-bar-thumb");
  bar.append(track, fill, thumb);
  const times = el("div", "kk-times");
  const cur = el("span", "kk-time-cur");
  const dur = el("span", "kk-time-dur");
  times.append(cur, dur);
  wrap.append(bar, times);
  wireSeekBar(bar);
  bar.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      e.stopPropagation();
      setPlayheadTime(now() + (e.key === "ArrowLeft" ? -SEEK_STEP : SEEK_STEP));
    }
  });
  return { wrap, bar, fill, thumb, cur, dur };
}

function wireSeekBar(bar) {
  const seekAt = (clientX) => {
    if (!totalDuration) return;
    const r = bar.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (clientX - r.left) / (r.width || 1)));
    setPlayheadTime(f * totalDuration);
  };
  bar.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    bar.setPointerCapture(e.pointerId);
    seekAt(e.clientX);
    const move = (ev) => seekAt(ev.clientX);
    const up = () => {
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", up);
      bar.removeEventListener("pointercancel", up);
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up);
    bar.addEventListener("pointercancel", up);
  });
}

/** Stop, Play / Pause and Next song: the footer's transport, which is hidden
 * while Karaoke is open. Next plays the first song in the queue. */
function buildTransportPanel() {
  const panel = el("div", "kk-panel kk-transport");
  const label = el("span", "kk-label");
  const row = el("div", "kk-transport-row");
  const stop = button("kk-btn kk-sq", null);
  stop.append(icon(ICONS.stop, { size: 16 }));
  const play = button("kk-btn kk-play", null);
  const next = button("kk-btn kk-sq", null);
  next.append(icon(ICONS.next, { size: 16, width: 2.2 }));
  stop.addEventListener("click", () => stopTransport());
  play.addEventListener("click", () => togglePlayPause());
  next.addEventListener("click", () => playQueued());
  row.append(stop, play, next);
  panel.append(label, row);
  return { panel, label, stop, play, next };
}

function paintTransport() {
  const playing = isPlaying();
  const hasNext = getQueue().length > 0;
  const hasTrack = Boolean(painted.hasTrack);
  const sig = `${playing}|${hasNext}|${hasTrack}|${t("karaoke.transport")}`;
  if (painted.transport === sig) return;
  painted.transport = sig;
  const p = ui.play;
  p.play.replaceChildren(icon(playing ? ICONS.pause : ICONS.play, { size: 20 }));
  p.play.setAttribute("aria-label", t(playing ? "karaoke.pause" : "karaoke.play"));
  p.play.title = t(playing ? "karaoke.pause" : "karaoke.play");
  p.play.disabled = !hasTrack;
  p.stop.disabled = !hasTrack;
  p.next.disabled = !hasNext;
}

function buildKeyPanel() {
  const panel = el("div", "kk-panel kk-key");
  const label = el("span", "kk-label");
  const row = el("div", "kk-key-row");
  const name = el("span", "kk-key-name");
  const badge = el("span", "kk-badge");
  row.append(name, badge);
  const btns = el("div", "kk-key-btns");
  const down = button("kk-btn kk-sq", null);
  down.append(icon(ICONS.minus, { size: 16, width: 2.2 }));
  const up = button("kk-btn kk-sq", null);
  up.append(icon(ICONS.plus, { size: 16, width: 2.2 }));
  const reset = button("kk-btn kk-reset", null, "");
  down.addEventListener("click", () => setGlobalPitch(getGlobalPitch().semitones - 1));
  up.addEventListener("click", () => setGlobalPitch(getGlobalPitch().semitones + 1));
  reset.addEventListener("click", () => resetAllKeys());
  btns.append(down, up, reset);
  panel.append(label, row, btns);
  return { panel, label, name, badge, down, up, reset };
}

function buildFullscreen() {
  fs = el("div", "kk-fs");
  fs.hidden = true;
  fs.setAttribute("role", "dialog");
  const { stage, bgArt } = buildStage("fs");
  const inner = el("div", "kk-fs-inner");
  const bar = el("div", "kk-fs-top");
  const art = buildArt("kk-fs-art");
  const titles = el("div", "kk-titles");
  const title = el("div", "kk-fs-title");
  const sub = el("div", "kk-fs-sub");
  titles.append(title, sub);
  const pill = el("span", "kk-pill");
  const exit = button("kk-icon-btn kk-fs-exit", null);
  exit.append(icon(ICONS.exitFullscreen, { size: 22 }));
  exit.addEventListener("click", () => exitFullscreen());
  bar.append(art.box, titles, pill, exit);
  const lyrics = buildLyrics("fs");
  const bottom = el("div", "kk-fs-bottom");
  const play = button("kk-fs-play", null);
  play.addEventListener("click", () => togglePlayPause());
  const cur = el("span", "kk-fs-time");
  const line = el("div", "kk-fs-line");
  const fill = el("div", "kk-fs-fill");
  line.append(fill);
  const dur = el("span", "kk-fs-time");
  bottom.append(play, cur, line, dur);
  inner.append(bar, lyrics.wrap, bottom);
  stage.append(inner);
  fs.append(stage);
  document.body.append(fs);
  let idleTimer = 0;
  const wake = () => {
    fs.classList.remove("kk-idle");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => fs.classList.add("kk-idle"), FS_IDLE_MS);
  };
  fs.addEventListener("pointermove", wake);
  fs.addEventListener("pointerdown", wake);
  fs.addEventListener("keydown", (e) => {
    wake();
    if (e.key === "Escape" && !document.fullscreenElement) {
      e.preventDefault();
      exitFullscreen();
    }
  });
  fs._wake = wake;
  ui.fs = { bgArt, art, title, sub, pill, exit, lyrics, play, cur, fill, dur };
  relabelFullscreen();
}

function relabel() {
  if (!ui) return;
  root?.setAttribute("aria-label", t("karaoke.regionAria"));
  ui.fsBtn.setAttribute("aria-label", t("karaoke.fullscreenEnter"));
  ui.fsBtn.title = t("karaoke.fullscreenEnter");
  ui.progress.bar.setAttribute("aria-label", t("karaoke.position"));
  ui.side.setAttribute("aria-label", t("karaoke.tabQueue"));
  ui.sideTitle.textContent = t("karaoke.tabQueue");
  ui.stripQueue.setAttribute("aria-label", t("karaoke.tabQueue"));
  ui.stripQueue.title = t("karaoke.tabQueue");
  ui.play.label.textContent = t("karaoke.transport");
  ui.play.stop.setAttribute("aria-label", t("karaoke.stop"));
  ui.play.stop.title = t("karaoke.stop");
  ui.play.next.setAttribute("aria-label", t("karaoke.next"));
  ui.play.next.title = t("karaoke.next");
  ui.key.label.textContent = t("karaoke.key");
  ui.key.badge.title = t("karaoke.transposeBadge");
  ui.key.down.setAttribute("aria-label", t("karaoke.keyDown"));
  ui.key.up.setAttribute("aria-label", t("karaoke.keyUp"));
  ui.key.reset.textContent = t("karaoke.keyReset");
  ui.stemsLabel.textContent = t("karaoke.stems");
  ui.noTrack.textContent = t("karaoke.noTrack");
  for (const L of [ui.lyrics, ui.fs?.lyrics].filter(Boolean)) {
    L.introTitle.textContent = t("karaoke.instrumental");
    L.emptyBtn.textContent = t("karaoke.openLyrics");
  }
  relabelFullscreen();
  // Text built per song (stems, queue, empty states) is rebuilt.
  for (const k of Object.keys(painted)) delete painted[k];
  if (ui.lyrics) ui.lyrics.phraseKey = null;
  if (ui.fs?.lyrics) ui.fs.lyrics.phraseKey = null;
}

function relabelFullscreen() {
  if (!ui?.fs) return;
  fs.setAttribute("aria-label", t("karaoke.regionAria"));
  ui.fs.exit.setAttribute("aria-label", t("karaoke.fullscreenExit"));
  ui.fs.exit.title = t("karaoke.fullscreenExit");
}

// ── Open / close ──

/** Open Karaoke Mode. Refused (returns false) while a take records: it mutes
 * the vocals, which would change what is being recorded against. */
export function openKaraoke() {
  if (open) return true;
  // A studio take or a Streamer take: Karaoke would mute the vocals under it.
  if (isRecording() || streamerTakeOpen()) {
    flashNote(t("karaoke.stopRecordingFirst"));
    return false;
  }
  if (!ui && !buildWindowed()) return false;
  open = true;
  root.hidden = false;
  document.documentElement.classList.add("karaoke-open");
  for (const id of ["lanes", "master-drawer"]) document.getElementById(id)?.setAttribute("inert", "");
  placeLayer();
  window.addEventListener("resize", placeLayer);
  layerObserver?.observe(document.getElementById("lanes"));
  const col = document.getElementById("lanes")?.closest(".daw-main-col");
  if (col) layerObserver?.observe(col);
  window.addEventListener("keydown", onKey, true);
  for (const k of Object.keys(painted)) delete painted[k];
  shownJob = null;
  loop();
  notify();
  repaintVoice();
  return true;
}

export function closeKaraoke() {
  if (!open) return;
  if (fsActive()) exitFullscreen();
  open = false;
  closeDrawer();
  root.hidden = true;
  document.documentElement.classList.remove("karaoke-open");
  for (const id of ["lanes", "master-drawer"]) document.getElementById(id)?.removeAttribute("inert");
  window.removeEventListener("resize", placeLayer);
  layerObserver?.disconnect();
  window.removeEventListener("keydown", onKey, true);
  cancelAnimationFrame(raf);
  raf = 0;
  notify();
}

export function isKaraokeOpen() {
  return open;
}

/** Called with the new open state whenever it changes (the Mode menu). */
export function onKaraokeChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

function notify() {
  // The saved mix in force is Karaoke's while it is open (mixMode.js).
  document.dispatchEvent(new CustomEvent("stemdeck:karaoke-changed", { detail: { open } }));
  for (const cb of listeners) {
    try { cb(open); } catch (err) { console.warn("[karaoke] change listener failed:", err); }
  }
}

let noteTimer = 0;
function flashNote(text) {
  let note = document.getElementById("karaokeNote");
  if (!note) {
    note = el("div", "kk-note");
    note.id = "karaokeNote";
    note.setAttribute("role", "status");
    document.body.append(note);
  }
  note.textContent = text;
  note.hidden = false;
  // Beside the Mode button, the way into Karaoke.
  const r = document.getElementById("modeBtn")?.getBoundingClientRect();
  if (r) {
    note.style.left = `${Math.round(r.right + 8)}px`;
    note.style.top = `${Math.round(r.top)}px`;
  }
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => { note.hidden = true; }, 3500);
}

// The layer covers the lanes, the master drawer and the footer (hidden while
// open): from the top of #lanes to the bottom of their column, as wide as #lanes.
const layerObserver = typeof ResizeObserver === "function" ? new ResizeObserver(() => placeLayer()) : null;

function placeLayer() {
  if (!open || !root) return;
  const lanes = document.getElementById("lanes");
  if (!lanes) return;
  const a = lanes.getBoundingClientRect();
  const bottom = (lanes.closest(".daw-main-col") || lanes).getBoundingClientRect().bottom;
  root.style.left = `${Math.round(a.left)}px`;
  root.style.top = `${Math.round(a.top)}px`;
  root.style.width = `${Math.round(a.width)}px`;
  root.style.height = `${Math.max(0, Math.round(bottom - a.top))}px`;
}

// ── The voice ──

/** The voice is off every time Karaoke is entered and on every song it plays:
 * Lead Vocals, or the one Vocals stem. Everything else is as the mode last
 * left it. Through the mixer's own state, so the lane's M shows it. */
function enforceVoiceOff() {
  if (!open) return;
  let changed = false;
  for (const [name, s] of Object.entries(mixerState)) {
    if (!s) continue;
    if (karaokeMutedByDefault(name) ? !s.muted : karaokeAlwaysOn(name) && (s.muted || s.soloed)) {
      s.muted = karaokeMutedByDefault(name);
      if (karaokeAlwaysOn(name)) s.soloed = false;
      changed = true;
    }
  }
  if (changed) {
    refreshMixerVisuals();
    applyMix();
    persistMix();
  }
}

// ── Lyrics loading ──

async function loadLyrics() {
  const info = getCurrentTrackInfo();
  const token = ++lyrToken;
  lyr = { trackId: info?.id ?? null, lines: [], words: [], plain: false, loading: Boolean(info) };
  envelope = null;
  if (!info) return;
  const res = await karaokeLyrics(info);
  if (token !== lyrToken) return;
  const lines = sungLines(res.lines);
  lyr = { trackId: info.id, lines, words: timedWords(lines, null), plain: res.plain, loading: false };
  resetPhrases();
  if (!lines.length) return;
  for (let tries = 0; tries < 3 && token === lyrToken; tries++) {
    const env = await vocalEnvelopeFor(info.id);
    if (token !== lyrToken) return;
    if (env) {
      envelope = env;
      lyr.words = timedWords(lyr.lines, envelope);
      resetPhrases();
      return;
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

function resetPhrases() {
  if (ui?.lyrics) ui.lyrics.phraseKey = null;
  if (ui?.fs?.lyrics) ui.fs.lyrics.phraseKey = null;
}

// ── The frame ──

function loop() {
  raf = 0;
  if (!open) return;
  try {
    frame();
  } catch (err) {
    console.warn("[karaoke] frame failed:", err);
  }
  raf = requestAnimationFrame(loop);
}

function frame() {
  const job = currentJobId;
  if (job !== shownJob) {
    shownJob = job;
    for (const k of Object.keys(painted)) delete painted[k];
    loadLyrics();
  }

  const hasTrack = Boolean(job && transport() && totalDuration > 0);
  if (painted.hasTrack !== hasTrack) {
    painted.hasTrack = hasTrack;
    root.classList.toggle("kk-empty-view", !hasTrack);
    ui.noTrack.hidden = hasTrack;
  }
  const tNow = hasTrack ? now() : 0;
  paintHeader();
  paintProgress(tNow);
  paintLyrics(ui.lyrics, tNow);
  paintSide();
  paintTransport();
  paintKey();
  paintStems();
  if (fsActive()) paintFullscreen(tNow);
}

function paintHeader() {
  const meta = trackMeta();
  const sig = `${meta.title}|${meta.artist}|${meta.art}`;
  if (painted.header === sig) return;
  painted.header = sig;
  ui.title.textContent = meta.title;
  ui.artist.textContent = meta.artist;
  setArt(ui.art.img, ui.art.box, meta.art);
  setBg(ui.bgArt, meta.art);
  if (ui.fs) {
    setArt(ui.fs.art.img, ui.fs.art.box, meta.art);
    setBg(ui.fs.bgArt, meta.art);
    ui.fs.title.textContent = meta.title;
  }
}

function setArt(img, box, src) {
  if (src) {
    if (img.getAttribute("src") !== src) img.src = src;
    img.hidden = false;
  } else {
    img.removeAttribute("src");
    img.hidden = true;
  }
  box.classList.toggle("kk-art-fallback", !src);
}

function setBg(img, src) {
  if (src) {
    if (img.getAttribute("src") !== src) img.src = src;
    img.hidden = false;
  } else {
    img.removeAttribute("src");
    img.hidden = true;
  }
}

function paintProgress(tNow) {
  const dur = totalDuration || 0;
  const pct = dur ? Math.max(0, Math.min(100, (tNow / dur) * 100)) : 0;
  const p = ui.progress;
  p.fill.style.width = `${pct}%`;
  p.thumb.style.left = `${pct}%`;
  const cur = clockLong(tNow);
  if (painted.cur !== cur) {
    painted.cur = cur;
    p.cur.textContent = cur;
    p.bar.setAttribute("aria-valuenow", String(Math.floor(tNow)));
    p.bar.setAttribute("aria-valuetext", cur);
  }
  const durText = clockLong(dur);
  if (painted.dur !== durText) {
    painted.dur = durText;
    p.dur.textContent = durText;
    p.bar.setAttribute("aria-valuemax", String(Math.floor(dur)));
  }
}

/** The lyric rows at `tNow`: the phrase's words built once per phrase, their
 * colour moved per frame by the media clock (so speed and seeking follow). */
function paintLyrics(L, tNow) {
  if (!L) return;
  const lines = lyr.lines;
  const loaded = !lyr.loading;
  if (loaded && !lines.length) {
    const state = lyr.plain ? "plain" : "none";
    if (L.phraseKey !== state) {
      L.phraseKey = state;
      L.prev.textContent = "";
      L.current.replaceChildren();
      L.next.replaceChildren();
      L.intro.hidden = true;
      L.empty.hidden = !lyr.trackId;
      L.emptyText.textContent = t(lyr.plain ? "karaoke.noSyncedLyrics" : "karaoke.noLyrics");
    }
    return;
  }
  L.empty.hidden = true;
  if (!lines.length) {
    if (L.phraseKey !== "loading") {
      L.phraseKey = "loading";
      L.prev.textContent = "";
      L.current.replaceChildren();
      L.next.replaceChildren();
      L.intro.hidden = true;
    }
    return;
  }
  const f = lyricFrame(lines, tNow);
  const key = f.state === "intro" ? "intro" : `p${f.phrase}`;
  if (L.phraseKey !== key) {
    L.phraseKey = key;
    L.prev.textContent = f.state === "lyric" ? f.prev : "";
    L.intro.hidden = f.state !== "intro";
    L.current.hidden = f.state === "intro";
    L.words = [];
    L.current.replaceChildren();
    if (f.state === "lyric") {
      for (const i of f.current) {
        const row = el("div", "kk-line");
        // A word keeps its own trailing space (none between CJK characters),
        // as the Lyrics tab lays them out.
        const spans = (lyr.words[i] || []).map((w) => {
          const span = el("span", "kk-w", w.text);
          row.append(span);
          return { el: span, start: w.start };
        });
        if (!spans.length) row.textContent = lines[i].text;
        L.words.push(...spans);
        L.current.append(row);
      }
    }
    L.next.replaceChildren(...f.next.map((text) => el("div", "kk-next-line", text)));
  }
  if (f.state === "intro") {
    const cd = t("karaoke.vocalsIn", { time: clockShort(Math.ceil(f.firstAt - tNow)) });
    if (L.introCount.textContent !== cd) L.introCount.textContent = cd;
    // On the beat: the song's tempo when known, else two a second.
    const bpm = parseFloat(document.getElementById("t-bpm")?.textContent || "") || 120;
    const beat = Math.floor((tNow * bpm) / 60) % 3;
    L.dots.forEach((d, i) => d.classList.toggle("on", i === beat));
    return;
  }
  for (const w of L.words) {
    const on = w.start <= tNow + 1e-6;
    if (w.on !== on) {
      w.on = on;
      w.el.classList.toggle("on", on);
    }
  }
}

function paintSide() {
  ui.queue.render();
}

// ── Playing the queue ──

// The song being brought up from the queue, so a second click or a song
// ending meanwhile cannot start another on top of it.
let bringing = null;
let mixLoadedFor = null;

/** Wait until `id` is open and its audio and mix are ready to play, or give up. */
async function whenPlayable(id, ms = 45000) {
  const t0 = performance.now();
  while (performance.now() - t0 < ms) {
    if (!open || bringing !== id) return false;
    if (currentJobId === id && totalDuration > 0 && audioEngine?.ready && mixLoadedFor === id) {
      const ok = await audioEngine.ready;
      if (!open || bringing !== id) return false;
      if (currentJobId === id) return ok !== false;
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  return false;
}

/** Play `id` (a click on a queue row) or, without one, the next in the queue. */
async function playQueued(id = null) {
  if (!open) return;
  const next = id ?? shiftQueue();
  if (!next) return;
  removeFromQueue(next);
  bringing = next;
  mixLoadedFor = null;
  try {
    await openTrackInStudio(next);
    if (await whenPlayable(next) && !isPlaying()) togglePlayPause();
  } catch (err) {
    console.warn("[karaoke] could not play the next song:", err);
  } finally {
    if (bringing === next) bringing = null;
  }
}

function toggleDrawer() {
  ui.side.classList.toggle("kk-drawer-open");
}

function closeDrawer() {
  ui?.side.classList.remove("kk-drawer-open");
}

function keyName() {
  const { semitones } = getGlobalPitch();
  const k = transposedKey(keyText(), semitones);
  if (!k) return "—";
  return t(k.minor ? "karaoke.keyMinor" : "karaoke.keyMajor", { root: k.root });
}

function paintKey() {
  const g = getGlobalPitch();
  const name = keyName();
  const sig = `${name}|${g.semitones}|${g.available}|${t("karaoke.keyReset")}`;
  if (painted.key === sig) return;
  painted.key = sig;
  const k = ui.key;
  k.name.textContent = name;
  k.badge.textContent = semitoneLabel(g.semitones);
  k.badge.classList.toggle("zero", g.semitones === 0);
  k.down.disabled = !g.available || g.semitones <= g.min;
  k.up.disabled = !g.available || g.semitones >= g.max;
  k.reset.disabled = !g.available;
}

// ── The voice form (Karaoke's stem choice) ──

function buildVoiceToggle() {
  const root = el("div", "kk-voice");
  root.setAttribute("role", "radiogroup");
  const label = el("span", "kk-voice-label");
  label.id = "kkVoiceLabel";
  root.setAttribute("aria-labelledby", label.id);
  const seg = el("div", "kk-voice-seg");
  const btns = {};
  for (const form of ["combined", "split"]) {
    const b = button("kk-voice-btn", null);
    b.dataset.form = form;
    b.setAttribute("role", "radio");
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      chooseVoice(form);
    });
    b.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      e.stopPropagation();
      const other = form === "split" ? "combined" : "split";
      btns[other].focus();
      chooseVoice(other);
    });
    seg.append(b);
    btns[form] = b;
  }
  root.append(label, seg);
  return { root, label, btns };
}

let voiceBusy = false;

async function chooseVoice(form) {
  const state = layoutState();
  if (!state.jobId || state.mode !== "karaoke" || voiceBusy) return;
  const present = new Set(state.present);
  const splitDone = present.has("lead_vocals") && present.has("backing_vocals");
  const now = state.voice || (state.shown.split ? "split" : "combined");
  if (form === now && (form === "combined" || splitDone)) return;
  voiceBusy = true;
  repaintVoice();
  const why = form === "split" && !splitDone ? await requestSplit() : await changeLayout({ voice: form });
  voiceBusy = false;
  repaintVoice();
  if (why === "recording") flashNote(t("karaoke.stopRecordingFirst"));
  // Why, from the server (#832), not only that it did not work.
  else if (why === "failed") flashNote(splitFailedText(layoutState().splitReason));
}

function repaintVoice() {
  if (!ui || !open) return;
  painted.voice = null;
  paintVoice();
}

function paintVoice() {
  const v = ui.voice;
  const state = layoutState();
  const present = new Set(state.present);
  const hasVoice = present.has("vocals") || (present.has("lead_vocals") && present.has("backing_vocals"));
  const running = state.splitState === "running" || voiceBusy;
  const form = state.voice || (state.shown.split ? "split" : "combined");
  // Separate needs the split; when this install cannot make it, and the song
  // has no pair yet, it is shown off with the reason (#832).
  const splitDone = present.has("lead_vocals") && present.has("backing_vocals");
  const blocked = !splitDone && Boolean(vocalSplitUnavailable());
  const sig = `${state.jobId}|${hasVoice}|${running}|${form}|${blocked}|${t("karaoke.voiceForm")}`;
  if (painted.voice === sig) return;
  painted.voice = sig;
  v.root.hidden = !state.jobId || !hasVoice;
  v.label.textContent = t("karaoke.voiceForm");
  v.root.title = t("karaoke.voiceFormTitle");
  for (const [f, b] of Object.entries(v.btns)) {
    const on = f === form;
    b.textContent = f === "split" && running ? t("karaoke.voiceFormRunning") : t(f === "split" ? "karaoke.voiceFormSplit" : "karaoke.voiceFormCombined");
    b.setAttribute("aria-checked", String(on));
    b.classList.toggle("on", on);
    b.tabIndex = on ? 0 : -1;
    b.disabled = running || (f === "split" && blocked);
    if (f === "split" && blocked) b.title = t("split.unavailable");
    else b.removeAttribute("title");
  }
}

function paintStems() {
  const names = karaokeStems(presentLanes());
  const sig = names.map((n) => `${n}:${mixerState[n]?.muted ? 0 : 1}`).join("|") + `|${t("karaoke.stems")}`;
  if (painted.stems === sig) return;
  const rebuild = painted.stemNames !== names.join("|") || painted.stemsLang !== t("karaoke.stems");
  painted.stems = sig;
  if (rebuild) {
    painted.stemNames = names.join("|");
    painted.stemsLang = t("karaoke.stems");
    ui.stemRow.replaceChildren();
    ui.stemCards.clear();
    for (const name of names) ui.stemRow.append(stemCard(name));
  }
  for (const name of names) {
    const card = ui.stemCards.get(name);
    if (!card) continue;
    const on = !mixerState[name]?.muted;
    const label = cardLabel(name);
    card.root.classList.toggle("on", on);
    card.root.classList.toggle("off", !on);
    card.toggle.setAttribute("aria-checked", String(on));
    card.toggle.setAttribute("aria-label", t(on ? "karaoke.stemOn" : "karaoke.stemOff", { name: label }));
    if (card.isLead) {
      card.iconBox.replaceChildren(icon(on ? ICONS.mic : ICONS.micOff, { size: 22, width: 1.9 }));
    }
  }
}

/** A card's name: Original is what plays under the singer, so Karaoke calls it
 * the backing track, the same words as Streamer's lane. Elsewhere it stays
 * Original. */
function cardLabel(name) {
  return name === "original" ? t("streamer.backingTrack") : laneLabel(name);
}

function stemCard(name) {
  const root = el("div", "kk-stem");
  root.dataset.stem = name;
  root.style.setProperty("--kk-stem", STEM_COLORS[name] || "#9aa3ad");
  const iconBox = el("span", "kk-stem-icon");
  iconBox.append(icon(ICONS[STEM_ICON[name] || "other"], { size: 22, width: 1.9 }));
  const label = el("span", "kk-stem-label", cardLabel(name));
  const toggle = button("kk-switch", null);
  toggle.setAttribute("role", "switch");
  toggle.append(el("span", "kk-knob"));
  const fixed = karaokeAlwaysOn(name);
  const flip = () => {
    if (fixed || !mixerState[name]) return;
    toggleStemMute(name);
    painted.stems = null;
  };
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    flip();
  });
  // The whole card is the target; the switch inside is what assistive
  // technology and the keyboard reach.
  root.addEventListener("click", () => flip());
  // The card is a size container; its body lays out by the card's width.
  const body = el("div", "kk-stem-body");
  body.append(iconBox, label, toggle);
  root.append(body);
  if (fixed) {
    root.classList.add("fixed");
    toggle.disabled = true;
    toggle.hidden = true;
  }
  ui.stemCards.set(name, { root, toggle, iconBox, isLead: name === "lead_vocals" || name === "vocals" });
  return root;
}

// ── Fullscreen ──

let fsFallback = false;

function fsActive() {
  return Boolean(fs && !fs.hidden);
}

function enterFullscreen() {
  if (!open) return;
  if (!fs) buildFullscreen();
  fs.hidden = false;
  resetPhrases();
  // The artwork and title go into the stage built just now.
  painted.header = null;
  painted.fsPlay = null;
  paintHeader();
  paintFullscreen(now());
  fs._wake();
  const req = fs.requestFullscreen?.bind(fs) || fs.webkitRequestFullscreen?.bind(fs);
  fsFallback = !req;
  fs.classList.toggle("kk-fs-fallback", fsFallback);
  if (req) {
    Promise.resolve()
      .then(() => req())
      .catch((err) => {
        // Refused (an iframe, a WebView without it): the same stage as a layer
        // over the window, closed with Esc or the exit button.
        console.warn("[karaoke] fullscreen refused, using the presentation layer:", err);
        fsFallback = true;
        fs.classList.add("kk-fs-fallback");
      });
  }
  fs.tabIndex = -1;
  fs.focus({ preventScroll: true });
}

function exitFullscreen() {
  if (!fs || fs.hidden) return;
  if (document.fullscreenElement === fs) {
    document.exitFullscreen?.().catch(() => {});
  }
  fs.hidden = true;
  fs.classList.remove("kk-fs-fallback", "kk-idle");
  fsFallback = false;
  ui?.fsBtn.focus({ preventScroll: true });
}

function onFullscreenChange() {
  // Leaving real fullscreen (Esc, the browser's own control) leaves the stage.
  if (fs && !fs.hidden && !fsFallback && document.fullscreenElement !== fs) {
    fs.hidden = true;
    fs.classList.remove("kk-idle");
  }
}

function paintFullscreen(tNow) {
  const f = ui.fs;
  if (!f) return;
  const meta = trackMeta();
  const sub = meta.artist;
  if (f.sub.textContent !== sub) f.sub.textContent = sub;
  if (f.title.textContent !== meta.title) f.title.textContent = meta.title;
  const lead = karaokeStems(presentLanes()).find((n) => n === "lead_vocals" || n === "vocals");
  const parts = [keyName()];
  if (lead) parts.push(t(mixerState[lead]?.muted ? "karaoke.stemOff" : "karaoke.stemOn", { name: laneLabel(lead) }));
  const pill = parts.join("  ·  ");
  if (f.pill.textContent !== pill) f.pill.textContent = pill;
  const playing = isPlaying();
  if (painted.fsPlay !== playing) {
    painted.fsPlay = playing;
    f.play.replaceChildren(icon(playing ? ICONS.pause : ICONS.play, { size: 16 }));
    f.play.setAttribute("aria-label", t(playing ? "karaoke.pause" : "karaoke.play"));
  }
  const dur = totalDuration || 0;
  f.fill.style.width = `${dur ? Math.max(0, Math.min(100, (tNow / dur) * 100)) : 0}%`;
  const cur = clockLong(tNow);
  if (f.cur.textContent !== cur) f.cur.textContent = cur;
  const durText = clockLong(dur);
  if (f.dur.textContent !== durText) f.dur.textContent = durText;
  paintLyrics(f.lyrics, tNow);
}

// ── Keys (only while Karaoke Mode is open) ──

function isTyping(target) {
  return target instanceof HTMLElement
    && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

function onKey(e) {
  if (!open || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return;
  // A dialog or menu over the view keeps its own keys.
  if (e.target instanceof Element && e.target.closest("[role=dialog]:not(.kk-fs), [role=menu], .settings-overlay")) return;
  // Space or Enter on one of this view's own buttons presses that button
  // only: the studio's Space would also start or stop playback.
  if ((e.code === "Space" || e.key === "Enter") && e.target instanceof Element
      && e.target.closest("#karaoke button, #karaoke [role=switch], .kk-fs button")) {
    e.stopPropagation();
    return;
  }
  if (e.code === "ArrowLeft" || e.code === "ArrowRight") {
    if (e.target instanceof Element && e.target.closest(".kk-bar, [role=slider], [role=tablist]")) return;
    e.preventDefault();
    e.stopPropagation();
    setPlayheadTime(now() + (e.code === "ArrowLeft" ? -SEEK_STEP : SEEK_STEP));
  } else if (e.code === "KeyF") {
    e.preventDefault();
    e.stopPropagation();
    if (fsActive()) exitFullscreen();
    else enterFullscreen();
  } else if (e.code === "Escape" && fsActive() && fsFallback) {
    e.preventDefault();
    e.stopPropagation();
    exitFullscreen();
  }
}

// ── Init ──

export function initKaraoke() {
  // A song's mix loaded while Karaoke is open, or Karaoke's record replaced
  // another mode's: the voice goes off.
  document.addEventListener("stemdeck:mix-loaded", (e) => {
    mixLoadedFor = e.detail?.id ?? null;
    enforceVoiceOff();
    repaintVoice();
  });
  // The voice form is painted on change, never per frame.
  onLayoutChange(repaintVoice);
  onLanguageChange(repaintVoice);
  onVocalSplitAvailability(repaintVoice);
  // A song ended while Karaoke is open: the next one in the queue plays.
  document.addEventListener("stemdeck:track-ended", () => {
    if (open && !bringing) playQueued();
  });
  loadQueue();
  onQueueChange(() => { if (ui) ui.queue.invalidate(); });
  document.addEventListener("stemdeck:mix-switched", () => enforceVoiceOff());
  document.addEventListener("stemdeck:lyrics-changed", (e) => {
    if (open && e.detail?.id && e.detail.id === lyr.trackId) loadLyrics();
  });
  document.addEventListener("fullscreenchange", onFullscreenChange);
  onLanguageChange(() => relabel());
}
