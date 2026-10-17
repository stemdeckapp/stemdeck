// Streamer Mode: a workspace view over the studio, entered from the Mode
// menu (modeUi.js, "Performance"), for recording a performance video: the
// camera's picture full frame (the stage), a take timeline, and the
// recording and output settings.
//
// Like Karaoke Mode (karaoke/karaoke.js), it is a view, not a second player:
// the time, the song and the mix are the studio's own (transport(),
// setPlayheadTime, state.js), read each frame by one animation loop that runs
// only while the view is open. As in Karaoke, the app's footer is hidden
// while it is open (streamer.css) and the view covers its place; Stop and
// Play / Pause sit in the mode header, and Space still plays.
//
// What a song keeps here is its scene (sceneStore.js): the aspect and the
// trim, edited only through the pure functions of streamerModel.js (one undo
// step per edit, a drag is one step). One renderer (stageRenderer.js) draws
// the live window and the recorded frames, so they cannot differ.
//
// Recording is streamerRecord.js, export streamerExport.js.

import { t, onLanguageChange } from "../i18n.js";
import { currentJobId, mixerState, trackIndex, totalDuration, audioEngine, playbackSpeed } from "../state.js";
import { transport, setPlayheadTime, togglePlayPause, stopTransport } from "../transport.js";
import { reloadKeepingPlace } from "../reloadPlace.js";
import { laneLabel, STEM_COLORS } from "../constants.js";
import { getSections, sectionDisplayName } from "../sections.js";
import { overviewPeaks, setPlaybackExclusion, playbackExclusion } from "../player.js";
import {
  choiceKey, parseChoice, serializeChoice, offeredChoices, effectiveChoice, excludedNames, sameNames,
  timelineLanes, BASE_STEMS,
} from "../partChoice.js";
import { isRecording } from "../capture/recordUi.js";
import { isKaraokeOpen, closeKaraoke } from "../karaoke/karaoke.js";
import {
  ASPECTS, defaultScene, withAspect, fitAspect, peakBars, clockTenths, clockHours,
  emptyHistory, pushHistory, undoHistory, redoHistory,
  trimRange, snapTime, withTrimPoint, pixelsPerSecond, tickStep, ticksEvery,
} from "./streamerModel.js";
import { getBeats, getBeatGridSnap } from "../beatgrid.js";
import { getTracks } from "../capture/takeLanes.js";
import { loadScene, saveScene, flushScene } from "./sceneStore.js";
import {
  SETTINGS_KEY, NONE, defaultRecordingSettings, parseRecordingSettings, serializeRecordingSettings,
  resolveDeviceId, isHeavy, applySettings, METER_SEGMENTS, METER_FLOOR_DB, meterFraction, levelDb, segmentZone,
  litSegments, holdPeak, newPeakState,
} from "./outputSettings.js";
import {
  startDevices, stopDevices, allowDevices, retryDevice, reconcile, cameraSource, cameraState,
  inputState, inputLevel, deviceLists, onDevicesChange, getStreamerMedia,
} from "./streamerDevices.js";
import { reasonMessage } from "../capture/capturePermission.js";
import { friendlyName, normaliseDevices, defaultInputDevice, fillInputSelect, channelText } from "../capture/trackInputs.js";
import {
  resolveInput, inputValue, autoPickStep, createLivePeaks, resetLivePeaks, addLivePeak, fillLiveBars, takeLaneState,
  videoLaneState, videoLaneNote, filmSpan, cameraUse,
} from "./inputModel.js";
import { loadFilmstrip, drawFilmstrip } from "../video/videoLane.js";
import { openRecordingSettings } from "../capture/recordingSettings.js";
import { storeGet, storeSet } from "../utils.js";
import { toggleStemMute, getMixMode, holdLaneSilent } from "../mixer.js";
import { isMixSwitching } from "../mixMode.js";
import {
  startRecording, stopRecording, drawOutput, recordState, takeOpen, onRecordChange, recordBlock,
  lastTake, streamerTrackId, markExport, takeInfo, forgetTake,
} from "./streamerRecord.js";
import { encoderNote } from "../videoEncoderNote.js";
import { renderRequest, exportTake, cancelExport, saveRawRecording, rawRecordingType, canRevealSaved, revealSaved, forgetSaved } from "./streamerExport.js";
import { shouldAutoStop, exportFileName, rawExtension, takeHoldStep, reviewStep } from "./recordModel.js";
import { renderStage } from "./stageRenderer.js";
import { reviewTier, reviewView, reviewNote, showsTake, takeVideoTime, syncStep, compactShows, fitRatio } from "./reviewModel.js";

const SAFE_KEY = "stemdeck.streamer.safeZones";
const ASPECT_LABEL = { "16:9": "streamer.aspect169", "9:16": "streamer.aspect916", "1:1": "streamer.aspect11" };
// The header's title while no song is open.
const NO_TITLE = String.fromCharCode(0x2014);
// The timeline's zoom step.
const ZOOM_STEP = 0.1;
// "I play": the part the musician plays, left out of the backing track they
// hear and export. Streamer's own choice per song (partChoice.js); the other
// modes keep theirs.
const PART_MODE = "streamer";

let root = null;
let open = false;
let raf = 0;
const listeners = new Set();

// The open song's scene (aspect and trim), the song it belongs to (null
// until loaded), and its undo history (per song, gone when the song changes).
let scene = defaultScene();
let sceneJob = null;
let sceneToken = 0;
let history = emptyHistory();
let safeZones = readSafeZones();
// The recording and output settings, per user (outputSettings.js).
let settings = defaultRecordingSettings();
// The take timeline's zoom (0 = the whole song), and a time to centre on
// once the new zoom is laid out.
let zoom = 0;
let zoomAnchor = null;

let shownJob = null;

// What was last painted, so a frame repaints only what changed.
const painted = {};
let ui = null;
// Texts to set again when the language changes: [element, key, attribute].
let texts = [];

function readSafeZones() {
  try { return localStorage.getItem(SAFE_KEY) === "1"; } catch { return false; }
}

function writeSafeZones() {
  try { localStorage.setItem(SAFE_KEY, safeZones ? "1" : "0"); } catch (err) {
    console.warn("[streamer] saving the safe zones switch failed:", err);
  }
}

// ── Small DOM helpers ──

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function button(cls) {
  const b = el("button", cls);
  b.type = "button";
  return b;
}

/** Set `key`'s text on `node` (text, or an attribute), now and on a language change. */
function tx(node, key, attr = "text") {
  texts.push([node, key, attr]);
  setText(node, key, attr);
  return node;
}

function setText(node, key, attr) {
  const v = t(key);
  if (attr === "text") node.textContent = v;
  else if (attr === "label") {
    node.setAttribute("aria-label", v);
    node.title = v;
  } else node.setAttribute(attr, v);
}

const SVG_NS = "http://www.w3.org/2000/svg";
function icon(paths, { size = 18, fill = "none", stroke = "currentColor", width = 1.8 } = {}) {
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
  camera: ["M3 8a2 2 0 0 1 2-2h2l1.5-2h7L17 6h2a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z", ["circle", { cx: 12, cy: 13, r: 3.5 }]],
  gear: [["circle", { cx: 12, cy: 12, r: 3 }], "M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"],
  caret: [["path", { d: "M6 9h12l-6 7z", fill: "currentColor", stroke: "none" }]],
  plus: ["M12 5v14M5 12h14"],
  minus: ["M5 12h14"],
  wave: ["M3 11v2M6 9v6M9 6v12M12 9v6M15 4v16M18 8v8M21 11v2"],
  scissors: [["circle", { cx: 6, cy: 6, r: 3 }], ["circle", { cx: 6, cy: 18, r: 3 }], "M20 4L8.1 15.9M14.5 14.5L20 20M8.1 8.1L12 12"],
  undo: ["M9 14L4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3"],
  redo: ["M15 14l5-5-5-5M20 9H9a5 5 0 0 0 0 10h3"],
  film: [["rect", { x: 3, y: 4, width: 18, height: 16, rx: 2 }], "M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"],
  mic: [["rect", { x: 9, y: 3, width: 6, height: 11, rx: 3 }], "M5 11a7 7 0 0 0 14 0M12 18v3"],
  webcam: [["rect", { x: 3, y: 6, width: 18, height: 13, rx: 2 }], ["circle", { cx: 12, cy: 12.5, r: 3 }], "M7 6l1.5-2h7L17 6"],
  output: [["rect", { x: 3, y: 4, width: 18, height: 16, rx: 2 }], "M15 4v16"],
  play: [["path", { d: "M7 4.5v15l12-7.5z", fill: "currentColor", stroke: "none" }]],
  pause: [["rect", { x: 6, y: 5, width: 4, height: 14, rx: 1, fill: "currentColor", stroke: "none" }], ["rect", { x: 14, y: 5, width: 4, height: 14, rx: 1, fill: "currentColor", stroke: "none" }]],
  stop: [["rect", { x: 6, y: 6, width: 12, height: 12, rx: 1.5, fill: "currentColor", stroke: "none" }]],
  speaker: [["path", { d: "M4 9h4l5-4v14l-5-4H4z", fill: "currentColor" }], "M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"],
};
function selectBox(cls, leadIcon) {
  const wrap = el("div", `sm-select ${cls || ""}`.trim());
  const sel = el("select", "sm-sel");
  if (leadIcon) {
    wrap.classList.add("sm-select-icon");
    const lead = icon(ICONS[leadIcon], { size: 18 });
    lead.classList.add("sm-sel-lead");
    wrap.append(lead);
  }
  const caret = icon(ICONS.caret, { size: 11 });
  caret.classList.add("sm-sel-caret");
  wrap.prepend(sel);
  wrap.append(caret);
  return { wrap, sel };
}

function option(sel, value, key) {
  const o = el("option");
  o.value = value;
  tx(o, key);
  sel.append(o);
  return o;
}

/** A switch: a button with role="switch", the design's 40 x 22 track. */
function makeSwitch(cls, labelKey, on, onChange) {
  const b = button(`sm-switch-row ${cls || ""}`.trim());
  b.setAttribute("role", "switch");
  const track = el("span", "sm-switch");
  track.append(el("span", "sm-knob"));
  b.append(track);
  if (labelKey) b.append(tx(el("span", "sm-switch-label"), labelKey));
  const set = (v) => b.setAttribute("aria-checked", String(Boolean(v)));
  set(on);
  b.addEventListener("click", () => {
    const next = b.getAttribute("aria-checked") !== "true";
    set(next);
    onChange?.(next);
  });
  return { b, set };
}

// ── Build ──

function buildView() {
  root = document.getElementById("streamer");
  if (!root) return false;
  root.replaceChildren();
  tx(root, "streamer.regionAria", "aria-label");
  // Focus moves here on open (R and Space are then Streamer's).
  root.tabIndex = -1;

  const center = el("div", "sm-center");
  const head = buildHead();
  const builder = buildBuilder();
  center.append(head.card, builder.panel);

  const side = buildSide();
  const scrim = el("div", "sm-scrim");
  scrim.addEventListener("click", () => toggleDrawer(false));

  const noTrack = el("div", "sm-no-track");
  noTrack.hidden = true;
  tx(noTrack, "streamer.noTrack");

  root.append(center, scrim, side.aside, noTrack);
  ui = { center, head, builder, side, noTrack };
  ui.canvas = builder.canvas;
  ui.ctx = builder.canvas.getContext("2d");
  const cs = getComputedStyle(document.documentElement);
  ui.font = getComputedStyle(root).fontFamily || "sans-serif";
  ui.accent = cs.getPropertyValue("--accent").trim() || "#f4b740";
  paintSafe();
  paintHistory();
  return true;
}

function buildHead() {
  const card = el("div", "sm-card sm-head");
  const row = el("div", "sm-head-row");
  const mark = icon(ICONS.camera, { size: 30, stroke: "var(--sm-yellow)", width: 1.7 });
  mark.classList.add("sm-head-icon");
  const title = tx(el("h2", "sm-head-title"), "streamer.title");
  const sep = el("span", "sm-head-sep");
  const sub = tx(el("span", "sm-head-sub"), "streamer.subtitle");
  const spacer = el("div", "sm-spacer");
  const aspect = selectBox("sm-aspect");
  tx(aspect.sel, "streamer.aspectAria", "aria-label");
  for (const a of ASPECTS) option(aspect.sel, a, ASPECT_LABEL[a]);
  aspect.sel.addEventListener("change", () => setAspect(aspect.sel.value));
  const safe = makeSwitch("sm-safe", "streamer.safeZones", safeZones, (on) => {
    safeZones = on;
    writeSafeZones();
    painted.scene = null;
  });
  // Narrow workspaces fold the output panel into a drawer opened from here.
  const output = button("sm-ib sm-output-btn");
  output.append(icon(ICONS.output, { size: 16 }), tx(el("span"), "streamer.outputToggle"));
  output.setAttribute("aria-expanded", "false");
  output.setAttribute("aria-controls", "streamerSide");
  output.addEventListener("click", () => toggleDrawer());
  // Stop and Play / Pause: the footer's, which is hidden while Streamer is
  // open.
  const tport = el("div", "sm-transport");
  const stop = button("sm-tp-btn sm-stop");
  stop.append(icon(ICONS.stop, { size: 14 }));
  tx(stop, "streamer.stop", "label");
  stop.addEventListener("click", () => {
    stopTransport();
    stopReview();
  });
  const play = button("sm-tp-btn sm-play");
  play.addEventListener("click", () => togglePlayPause());
  tport.append(stop, play);
  row.append(mark, title, sep, sub, spacer, aspect.wrap, safe.b, output, tport);
  card.append(row);
  return { card, aspect: aspect.sel, safe, output, stop, play };
}

function buildBuilder() {
  const panel = el("div", "sm-builder");
  panel.id = "streamerBuilderPanel";

  const row = el("div", "sm-row");
  const col = el("div", "sm-preview-col");

  // The live window: always 16:9 on screen, the camera's picture inside it
  // at the scene's aspect, as a take records it.
  const preview = el("div", "sm-preview");
  preview.setAttribute("role", "img");
  tx(preview, "streamer.previewHint", "aria-label");
  const canvas = el("canvas", "sm-canvas");
  canvas.setAttribute("aria-hidden", "true");
  const rec = button("sm-rec");
  const pill = el("span", "sm-rec-pill");
  const ring = el("span", "sm-rec-ring");
  ring.append(el("span", "sm-rec-dot"));
  // While a take records: the input's level, so sound arriving shows at once.
  const recLevel = el("span", "sm-rec-level");
  recLevel.setAttribute("aria-hidden", "true");
  pill.append(ring, tx(el("span", "sm-rec-label"), "streamer.recReady"), recLevel);
  const recTime = el("span", "sm-rec-time", clockHours(0));
  rec.append(pill, recTime);
  rec.setAttribute("aria-pressed", "false");
  tx(rec, "streamer.recStart", "aria-label");
  // Record and Stop (also the R key).
  rec.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleRecording();
  });
  // Why the camera is not showing, over the preview, with Allow or Retry.
  const camState = el("div", "sm-cam-state");
  camState.hidden = true;
  camState.setAttribute("role", "status");
  const camText = el("p", "sm-cam-text");
  const camAct = button("sm-primary sm-cam-act");
  camAct.addEventListener("click", (e) => {
    e.stopPropagation();
    deviceAction("video");
  });
  camState.append(camText, camAct);
  preview.append(canvas, camState, rec);

  const strip = el("div", "sm-strip");
  const stripTitle = el("div", "sm-card sm-strip-title");
  stripTitle.append(icon(ICONS.scissors, { size: 18, width: 1.9 }), tx(el("span"), "streamer.timeline"));
  const hist = el("div", "sm-card sm-history");
  const undo = button("sm-history-btn sm-undo");
  undo.append(icon(ICONS.undo, { size: 15, width: 2 }));
  tx(undo, "streamer.undo", "label");
  undo.addEventListener("click", () => doUndo());
  const redo = button("sm-history-btn sm-redo");
  redo.append(icon(ICONS.redo, { size: 15, width: 2 }));
  tx(redo, "streamer.redo", "label");
  redo.addEventListener("click", () => doRedo());
  hist.append(undo, redo);
  const clock = el("div", "sm-card sm-clock");
  const clockNow = el("span", "sm-clock-now", clockTenths(0));
  const clockDur = el("span", "sm-clock-dur", `/ ${clockTenths(0)}`);
  clock.append(clockNow, clockDur);
  strip.append(stripTitle, hist, clock);
  const rv = buildReview();
  // The two windows side by side (or one, in a narrow workspace: the
  // review takes the live window's place while a take plays).
  const screens = el("div", "sm-screens");
  screens.append(preview, rv.box, rv.switchBox);
  col.append(screens, strip);
  row.append(col);

  const timeline = buildTimeline();
  panel.append(row, timeline.card);
  return { panel, row, col, screens, review: rv, preview, canvas, rec, recLabel: pill.querySelector(".sm-rec-label"), recTime, recLevel, camState, camText, camAct, clockNow, clockDur, undo, redo, timeline };
}

/** The take review window (the recorded take, played with the transport)
 * and, for a workspace with room for one window only, the Live | Take
 * switch over it. */
function buildReview() {
  const box = el("div", "sm-review");
  box.setAttribute("role", "img");
  tx(box, "streamer.review.aria", "aria-label");
  box.dataset.view = "none";
  const canvas = el("canvas", "sm-review-canvas");
  canvas.setAttribute("aria-hidden", "true");
  const tag = tx(el("span", "sm-review-tag"), "streamer.review.tag");
  const note = el("p", "sm-review-note");
  note.setAttribute("aria-live", "polite");
  box.append(canvas, tag, note);

  const switchBox = el("div", "sm-view-switch");
  switchBox.setAttribute("role", "group");
  tx(switchBox, "streamer.review.switchAria", "aria-label");
  switchBox.hidden = true;
  const live = tx(button("sm-view-btn sm-view-live"), "streamer.review.live");
  live.dataset.pick = "live";
  const take = tx(button("sm-view-btn sm-view-take"), "streamer.review.take");
  take.dataset.pick = "take";
  for (const b of [live, take]) {
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      review.pick = b.dataset.pick;
      painted.review = null;
    });
    b.addEventListener("pointerdown", (e) => e.stopPropagation());
  }
  switchBox.append(live, take);
  return { box, canvas, ctx: canvas.getContext("2d"), tag, note, switchBox, live, take };
}

function buildTimeline() {
  const card = el("div", "sm-card sm-tl");
  const zoomBox = el("div", "sm-zoom");
  const zOut = button("sm-ib sm-zoom-btn sm-zoom-out");
  zOut.append(icon(ICONS.minus, { size: 12, width: 2.6 }));
  tx(zOut, "streamer.zoomOut", "label");
  zOut.addEventListener("click", () => setZoom(zoom - ZOOM_STEP));
  const zTrack = el("div", "sm-zoom-track");
  zTrack.setAttribute("role", "slider");
  zTrack.tabIndex = 0;
  zTrack.setAttribute("aria-valuemin", "0");
  zTrack.setAttribute("aria-valuemax", "100");
  tx(zTrack, "streamer.zoom", "aria-label");
  const zFill = el("div", "sm-zoom-fill");
  const zThumb = el("div", "sm-zoom-thumb");
  zTrack.append(zFill, zThumb);
  wireZoomSlider(zTrack);
  const zIn = button("sm-ib sm-zoom-btn sm-zoom-in");
  zIn.append(icon(ICONS.plus, { size: 12, width: 2.6 }));
  tx(zIn, "streamer.zoomIn", "label");
  zIn.addEventListener("click", () => setZoom(zoom + ZOOM_STEP));
  const fit = tx(button("sm-ib sm-fit"), "streamer.fit");
  fit.addEventListener("click", () => setZoom(0));
  zoomBox.append(zOut, zTrack, zIn, fit);

  // One scrolling grid: a label column that stays put (sticky) and the
  // song's column, as wide as the zoom makes it. Everything in the song's
  // column (markers, ruler, lanes, trim, playhead) is placed from one scale.
  const scroll = el("div", "sm-tl-scroll");
  const grid = el("div", "sm-tl-grid");
  const mask = el("div", "sm-tl-mask");

  const markers = el("div", "sm-tl-markers");
  const ruler = el("div", "sm-tl-ruler");
  ruler.setAttribute("role", "slider");
  ruler.tabIndex = 0;
  ruler.setAttribute("aria-valuemin", "0");
  tx(ruler, "streamer.position", "aria-label");
  wireRuler(ruler);

  const videoLabel = el("div", "sm-tl-label sm-tl-label-video");
  videoLabel.append(icon(ICONS.film, { size: 14, width: 2 }), tx(el("span"), "streamer.trackVideo"));
  // The Video lane: the take's frames go in it (paintTakeBlock).
  const video = el("div", "sm-tl-video");
  video.append(el("span", "sm-tl-grip", "‹"), el("div", "sm-tl-video-body"), el("span", "sm-tl-grip", "›"));

  const audioLabels = el("div", "sm-tl-audio-labels");
  const audio = el("div", "sm-tl-audio");
  const lanes = [];
  for (let i = 0; i < 2; i++) {
    const label = el("div", "sm-tl-label sm-tl-label-stem");
    const name = el("span", "sm-tl-label-name");
    label.append(icon(ICONS.wave, { size: 14, width: 2 }), name);
    audioLabels.append(label);
    const c = el("canvas", `sm-tl-lane sm-tl-lane-${i}`);
    audio.append(c);
    lanes.push({ label, name, canvas: c, key: null });
  }
  // The take lane's mute (the take track's own, in Performance's mix).
  const takeMute = button("sm-tl-mute");
  takeMute.append(icon(ICONS.speaker, { size: 13, width: 2 }));
  tx(takeMute, "streamer.lane.muteTake", "label");
  takeMute.setAttribute("aria-pressed", "false");
  takeMute.hidden = true;
  takeMute.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleTakeMute();
  });
  lanes[1].label.append(takeMute);
  // "I play": the part left out of the backing track (top left of the card).
  const partBox = el("div", "sm-part");
  const partSel = selectBox("sm-part-select");
  partSel.sel.id = "streamerPart";
  const partLabel = tx(el("label", "sm-part-label"), "streamer.part.label");
  partLabel.htmlFor = "streamerPart";
  tx(partSel.sel, "streamer.part.aria", "title");
  partSel.sel.addEventListener("change", () => choosePart(partSel.sel.value));
  partBox.append(partLabel, partSel.wrap);
  // The take lane before a take: empty, with a hint (never the stem's own wave).
  const takeHint = tx(el("p", "sm-tl-note sm-tl-take-hint"), "streamer.lane.emptyHint");
  takeHint.hidden = true;
  audio.append(takeHint);
  // The take on the video lane: a block that grows while it records, then
  // the take's frames (its filmstrip) where it sits in the song.
  const takeBlock = el("div", "sm-tl-take");
  takeBlock.hidden = true;
  const film = el("canvas", "sm-tl-film");
  film.setAttribute("aria-hidden", "true");
  takeBlock.append(film);
  // What the lane cannot show, said in words (no video part, frames that
  // failed, a take filmed without the camera).
  const videoNote = el("p", "sm-tl-video-note");
  videoNote.hidden = true;
  video.append(takeBlock, videoNote);
  // A song without stems, and one without sections, say so in their rows.
  const noStems = tx(el("p", "sm-tl-note sm-tl-no-stems"), "streamer.noStems");
  noStems.hidden = true;
  audio.append(noStems);
  const noSections = tx(el("p", "sm-tl-note sm-tl-no-sections"), "streamer.noSections");
  noSections.hidden = true;
  // The kept range: a yellow box with an in and an out handle; what is
  // trimmed off is dimmed.
  const range = el("div", "sm-tl-range");
  const trimIn = el("span", "sm-tl-trim sm-tl-trim-l", "‹");
  const trimOut = el("span", "sm-tl-trim sm-tl-trim-r", "›");
  for (const [h, which, key] of [[trimIn, "in", "streamer.trimInAria"], [trimOut, "out", "streamer.trimOutAria"]]) {
    h.dataset.which = which;
    h.setAttribute("role", "slider");
    h.tabIndex = 0;
    tx(h, key, "aria-label");
    wireTrimHandle(h, which);
  }
  range.append(trimIn, trimOut);
  audio.append(range);
  const dims = el("div", "sm-tl-dims");
  const dimL = el("div", "sm-tl-dim");
  const dimR = el("div", "sm-tl-dim");
  dims.append(dimL, dimR);
  const tip = el("div", "sm-tl-tip");
  tip.hidden = true;

  const overlay = el("div", "sm-tl-overlay");
  const head = el("div", "sm-tl-playhead");
  const headTime = el("span", "sm-tl-playhead-time", clockTenths(0));
  head.append(headTime, el("span", "sm-tl-playhead-line"));
  overlay.append(head);

  grid.append(mask, markers, noSections, ruler, videoLabel, video, audioLabels, audio, dims, overlay, tip);
  scroll.append(grid);
  scroll.addEventListener("scroll", () => { painted.lanes = null; });
  // A plain wheel scrolls the zoomed song sideways.
  scroll.addEventListener("wheel", (e) => {
    if (zoom <= 0 || e.ctrlKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
    e.preventDefault();
    scroll.scrollLeft += e.deltaY;
  }, { passive: false });
  card.append(partBox, zoomBox, scroll);
  return {
    partSel: partSel.sel,
    takeHint, takeBlock, film, videoNote, takeMute,
    card, zoomBox, zTrack, zFill, zThumb, zOut, zIn, scroll, grid, mask, markers, ruler, video, audio, lanes,
    range, trimIn, trimOut, dimL, dimR, tip, head, headTime, noStems, noSections,
  };
}

function buildSide() {
  const aside = el("aside", "sm-side");
  aside.id = "streamerSide";

  // Recording & Output: the camera and the input (streamerDevices.js), the
  // input meter, and the output settings (outputSettings.js, stored per
  // user): the resolution, frame rate and switches a take is recorded with.
  const rec = el("section", "sm-card sm-out");
  const recHead = tx(el("h3", "sm-out-head"), "streamer.output");
  const body = el("div", "sm-out-body");
  const field = (labelKey, id, sel) => {
    const label = tx(el("label", "sm-flabel"), labelKey);
    label.htmlFor = id;
    sel.id = id;
    return label;
  };
  // Under each device select: why it is not working, and Retry or Allow.
  const stateLine = (kind) => {
    const line = el("div", "sm-dev-state");
    line.hidden = true;
    line.setAttribute("role", "status");
    const text = el("span", "sm-dev-text");
    const act = button("sm-ib sm-dev-act");
    act.addEventListener("click", () => deviceAction(kind));
    line.append(text, act);
    return { line, text, act };
  };
  const vsrc = selectBox("", "webcam");
  vsrc.sel.addEventListener("change", () => setSetting({ videoDeviceId: vsrc.sel.value }));
  const vstate = stateLine("video");
  const asrc = selectBox("", "mic");
  asrc.sel.addEventListener("change", () => chooseInput(asrc.sel.value));
  const astate = stateLine("audio");
  const meterRow = el("div", "sm-meter-row");
  const meter = el("div", "sm-meter");
  meter.setAttribute("role", "meter");
  meter.setAttribute("aria-valuemin", String(METER_FLOOR_DB));
  meter.setAttribute("aria-valuemax", "0");
  meter.setAttribute("aria-valuenow", String(METER_FLOOR_DB));
  tx(meter, "streamer.inputLevel", "aria-label");
  // Two rows (the first two channels), each segment coloured by its zone.
  const meterRows = [];
  for (let r = 0; r < 2; r++) {
    const bar = el("div", "sm-meter-bar");
    const segs = [];
    for (let k = 0; k < METER_SEGMENTS; k++) {
      const seg = el("span", `sm-meter-seg sm-seg-${segmentZone(k)}`);
      segs.push(seg);
      bar.append(seg);
    }
    meter.append(bar);
    meterRows.push({ segs, lit: 0, peak: newPeakState() });
  }
  const level = el("span", "sm-meter-db", `${METER_FLOOR_DB} dB`);
  const inSet = button("sm-ib sm-in-settings");
  inSet.append(icon(ICONS.gear, { size: 16 }));
  tx(inSet, "streamer.inputSettings", "label");
  // The studio's own input settings (Settings, Recording tab).
  inSet.addEventListener("click", () => openRecordingSettings());
  meterRow.append(meter, level, inSet);
  const grid = el("div", "sm-out-grid");
  const res = selectBox();
  for (const [v, k] of [["1080", "streamer.res1080"], ["720", "streamer.res720"]]) option(res.sel, v, k);
  res.sel.addEventListener("change", () => setSetting({ resolution: res.sel.value }));
  const fps = selectBox();
  for (const n of [30, 60, 24]) {
    const o = el("option");
    o.value = String(n);
    o.dataset.fps = String(n);
    o.textContent = t("streamer.fpsValue", { n });
    fps.sel.append(o);
  }
  fps.sel.addEventListener("change", () => setSetting({ fps: Number(fps.sel.value) }));
  const resCol = el("div", "sm-out-col");
  resCol.append(field("streamer.resolution", "streamerRes", res.sel), res.wrap);
  const fpsCol = el("div", "sm-out-col");
  fpsCol.append(field("streamer.fps", "streamerFps", fps.sel), fps.wrap);
  grid.append(resCol, fpsCol);
  // 60 fps is allowed, with a warning about the computer's load.
  const heavy = tx(el("p", "sm-heavy"), "streamer.heavy");
  heavy.hidden = true;
  const switches = el("div", "sm-out-switches");
  const sw = {};
  for (const [key, k] of [["recordMixedAudio", "streamer.recMixed"], ["autoStopAtEnd", "streamer.recAutostop"]]) {
    sw[key] = makeSwitch("", k, settings[key], (on) => setSetting({ [key]: on }));
    sw[key].b.dataset.setting = key;
    switches.append(sw[key].b);
  }
  // The last take: Export MP4, its progress and Cancel, Show in folder, and
  // the raw recording when no MP4 can be made.
  const exp = el("div", "sm-export");
  exp.hidden = true;
  const expText = el("p", "sm-export-text");
  expText.setAttribute("role", "status");
  const expBar = el("progress", "sm-export-bar");
  expBar.max = 1;
  expBar.value = 0;
  expBar.hidden = true;
  const expRow = el("div", "sm-export-row");
  const expGo = button("sm-primary sm-export-go");
  expGo.addEventListener("click", () => runExport());
  const expCancel = tx(button("sm-ib sm-export-cancel"), "streamer.export.cancel");
  expCancel.addEventListener("click", () => cancelRunningExport());
  const expReveal = tx(button("sm-ib sm-export-reveal"), "streamer.export.reveal");
  expReveal.addEventListener("click", () => {
    revealSaved().catch((err) => {
      console.warn("[streamer] showing the file failed:", err);
      flashNote(t("streamer.export.revealFailed"), expReveal);
    });
  });
  const expRaw = tx(button("sm-ib sm-export-raw"), "streamer.export.saveRaw");
  expRaw.addEventListener("click", () => saveRaw());
  expRow.append(expGo, expCancel, expReveal, expRaw);
  exp.append(expText, expBar, expRow);
  body.append(
    field("streamer.videoSource", "streamerVideoSrc", vsrc.sel), vsrc.wrap, vstate.line,
    field("streamer.audioSource", "streamerAudioSrc", asrc.sel), asrc.wrap, astate.line,
    meterRow, grid, heavy, switches, exp,
  );
  rec.append(recHead, body);

  // Export to a file is the only way out for now (upload and streaming to
  // platforms are postponed: docs/plans/0.21.0/integrations-*.md).
  aside.append(rec);
  return {
    aside, fps: fps.sel, vsrc: vsrc.sel, asrc: asrc.sel, vstate, astate,
    meter, meterRows, level, res: res.sel, heavy, sw,
    exp, expText, expBar, expGo, expCancel, expReveal, expRaw,
  };
}

function relabel() {
  for (const [node, key, attr] of texts) setText(node, key, attr);
  if (!ui) return;
  for (const o of ui.side.fps.options) o.textContent = t("streamer.fpsValue", { n: o.dataset.fps });
  for (const k of Object.keys(painted)) delete painted[k];
}

// ── Aspect, safe zones, drawer ──

function paintSafe() {
  ui?.head.safe.set(safeZones);
}

function setAspect(aspect) {
  if (!ASPECTS.includes(aspect) || aspect === scene.aspect) return;
  commit(withAspect(scene, aspect));
}

function toggleDrawer(force) {
  if (!ui) return;
  const on = typeof force === "boolean" ? force : !root.classList.contains("sm-drawer-open");
  const was = root.classList.contains("sm-drawer-open");
  // The drawer opens under the mode header, wherever it ends (it wraps when narrow).
  const head = ui.head.card.getBoundingClientRect();
  root.style.setProperty("--sm-drawer-top", `${Math.round(head.bottom - root.getBoundingClientRect().top + 8)}px`);
  root.classList.toggle("sm-drawer-open", on);
  ui.head.output.setAttribute("aria-expanded", String(on));
  // Opened: focus goes into it. Closed with focus inside: back to Output.
  if (on && !was) ui.side.vsrc.focus({ preventScroll: true });
  else if (!on && was && ui.side.aside.contains(document.activeElement)) ui.head.output.focus({ preventScroll: true });
}

// ── State reads (the player's own; never copied) ──

function now() {
  const v = transport()?.getCurrentTime?.();
  return Number.isFinite(v) ? v : 0;
}

/** The song's title and artist, as the studio shows them (the export's file name). */
function trackMeta() {
  const title = document.getElementById("title")?.textContent?.trim() || "";
  const artistEl = document.getElementById("np-artist");
  const artist = artistEl && !artistEl.hidden ? artistEl.textContent.trim() : "";
  return { title: title === NO_TITLE ? "" : title, artist };
}

// ── Scene per song (aspect and trim), edits and undo ──

async function showSceneFor(job) {
  const token = ++sceneToken;
  if (sceneJob && sceneJob !== job) await flushScene(sceneJob);
  const next = await loadScene(job);
  if (token !== sceneToken) return;
  scene = next;
  sceneJob = job;
  history = emptyHistory();
  if (ui) ui.head.aspect.value = scene.aspect;
  // Shown, not edited: nothing is saved (sceneStore.js: a song only viewed
  // leaves no trace).
  afterChange({ save: false });
}

/** The scene is now `next`, made from `from` (the scene before the edit):
 * one undo step, saved for this song. */
function commit(next, from = scene) {
  if (!next || next === from) return false;
  // While a take is open: no new aspect or trim.
  if (takeOpen() && reshapes(from, next)) return false;
  history = pushHistory(history, from);
  scene = next;
  afterChange();
  return true;
}

/** Whether `next` changes what a take films the shape of: the aspect or the trim. */
function reshapes(from, next) {
  const trim = (sc) => JSON.stringify(sc.trim || null);
  return from.aspect !== next.aspect || trim(from) !== trim(next);
}

/** Repaint, and save after the scene changed (an edit, undo or redo). */
function afterChange({ save = true } = {}) {
  if (save) saveScene(sceneJob, scene);
  if (ui) ui.head.aspect.value = scene.aspect;
  painted.scene = null;
  painted.tlTrim = null;
  paintHistory();
}

function doUndo() {
  // While a take is open the scene's shape stays as it is being filmed.
  if (takeOpen()) return;
  const r = undoHistory(history, scene);
  if (!r.scene) return;
  history = r.history;
  scene = r.scene;
  afterChange();
}

function doRedo() {
  if (takeOpen()) return;
  const r = redoHistory(history, scene);
  if (!r.scene) return;
  history = r.history;
  scene = r.scene;
  afterChange();
}

function paintHistory() {
  if (!ui) return;
  ui.builder.undo.disabled = !history.past.length || takeOpen();
  ui.builder.redo.disabled = !history.future.length || takeOpen();
}

// ── Open / close ──

/** Open Streamer Mode. Refused (returns false) while a take records: its own
 * saved mix would change what is being recorded against. */
export function openStreamer() {
  if (open) return true;
  if (isRecording()) {
    flashNote(t("streamer.stopRecordingFirst"));
    return false;
  }
  if (!ui && !buildView()) return false;
  // One workspace view at a time.
  if (isKaraokeOpen()) closeKaraoke();
  open = true;
  root.hidden = false;
  document.documentElement.classList.add("streamer-open");
  for (const id of ["lanes", "master-drawer"]) document.getElementById(id)?.setAttribute("inert", "");
  placeLayer();
  window.addEventListener("resize", placeLayer);
  observeLayout();
  window.addEventListener("keydown", onKey, true);
  for (const k of Object.keys(painted)) delete painted[k];
  shownJob = undefined;
  startDevices().catch((err) => console.warn("[streamer] reading the devices failed:", err));
  loop();
  notify();
  // Focus goes into the view, and back to what opened it on close.
  opener = document.activeElement instanceof HTMLElement && !root.contains(document.activeElement) ? document.activeElement : null;
  focusView();
  return true;
}

let opener = null;

/** Focus the view itself (R and Space are then Streamer's). */
function focusView() {
  root?.focus({ preventScroll: true });
}

export function closeStreamer() {
  if (!open) return;
  // A take in progress stops first and is kept (on the Streamer track): it
  // can still be exported when Streamer opens again.
  if (takeOpen()) stopRecording();
  open = false;
  toggleDrawer(false);
  root.hidden = true;
  document.documentElement.classList.remove("streamer-open");
  // Karaoke may have opened over the lanes this moment: its inert stays.
  if (!isKaraokeOpen()) {
    for (const id of ["lanes", "master-drawer"]) document.getElementById(id)?.removeAttribute("inert");
  }
  window.removeEventListener("resize", placeLayer);
  layerObserver?.disconnect();
  windowsObserver?.disconnect();
  // The take's video and its source are let go.
  releaseReview();
  window.removeEventListener("keydown", onKey, true);
  cancelAnimationFrame(raf);
  raf = 0;
  // The camera light goes off, the input closes.
  stopDevices();
  flushScene(sceneJob);
  notify();
  // The whole song again (only playback changes; the choice stays saved).
  applyPart();
  const a = document.activeElement;
  // Left through the Mode menu: its item hides with the menu, so focus
  // goes back as if from the view itself.
  if (!a || a === document.body || root.contains(a) || a.closest?.(".mode-menu")) {
    const back = opener?.isConnected && opener.offsetParent && !opener.closest(".mode-menu") ? opener : modeBtn();
    back?.focus({ preventScroll: true });
  }
  opener = null;
}

/** A Streamer take is open (starting, recording or stopping). */
export function isStreamerTakeOpen() {
  return takeOpen();
}

export function isStreamerOpen() {
  return open;
}

/** The take review as it is now (for the browser tests): the view, the
 * layout tier, which picture the one window shows, and the video's time
 * against the take time the playhead asks for. */
export function reviewState() {
  const take = open ? reviewSource().take : null;
  const v = review.video;
  const song = transport()?.getCurrentTime?.() ?? 0;
  return {
    view: review.view,
    tier: review.tier,
    win: review.win,
    shows: ui?.builder.screens.dataset.shows || null,
    src: review.src,
    hasVideo: Boolean(v),
    paused: v ? v.paused : true,
    videoTime: v ? v.currentTime : null,
    videoDuration: v && Number.isFinite(v.duration) ? v.duration : null,
    takeTime: take && Number.isFinite(take.camera_start_sec) ? song - take.camera_start_sec : null,
    cameraStart: take?.camera_start_sec ?? null,
  };
}

/** Called with the new open state whenever it changes (the Mode menu). */
export function onStreamerChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

function notify() {
  // The saved mix in force is Streamer's while it is open (mixMode.js).
  document.dispatchEvent(new CustomEvent("stemdeck:streamer-changed", { detail: { open } }));
  for (const cb of listeners) {
    try { cb(open); } catch (err) { console.warn("[streamer] change listener failed:", err); }
  }
}

/** The Mode button: the way into Performance, where its notes and focus go. */
function modeBtn() {
  return document.getElementById("modeBtn");
}

let noteTimer = 0;
/** A short note beside `anchor` (the Mode button when none), gone by itself. */
function flashNote(text, anchor) {
  let note = document.getElementById("streamerNote");
  if (!note) {
    note = el("div", "sm-note");
    note.id = "streamerNote";
    note.setAttribute("role", "status");
    document.body.append(note);
  }
  note.textContent = text;
  note.hidden = false;
  const r = (anchor || modeBtn())?.getBoundingClientRect();
  if (r) {
    const w = note.offsetWidth;
    const beside = !anchor;
    const left = beside ? r.right + 8 : Math.min(window.innerWidth - w - 8, Math.max(8, r.left + r.width / 2 - w / 2));
    const top = beside ? r.top : r.bottom + 8 + note.offsetHeight > window.innerHeight ? r.top - note.offsetHeight - 8 : r.bottom + 8;
    note.style.left = `${Math.round(left)}px`;
    note.style.top = `${Math.round(top)}px`;
  }
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => { note.hidden = true; }, 3500);
}

// The layer covers the lanes, the master drawer and the footer (hidden while
// open): from the top of #lanes to the bottom of their column, as wide as #lanes.
const layerObserver = typeof ResizeObserver === "function" ? new ResizeObserver(() => placeLayer()) : null;

// The Scene Builder's size decides where the live and review windows go.
const windowsObserver = typeof ResizeObserver === "function" ? new ResizeObserver(() => placeWindows()) : null;

function observeLayout() {
  if (ui?.builder.panel) windowsObserver?.observe(ui.builder.panel);
  if (!layerObserver) return;
  const lanes = document.getElementById("lanes");
  if (lanes) layerObserver.observe(lanes);
  const col = lanes?.closest(".daw-main-col");
  if (col) layerObserver.observe(col);
}

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

// ── The frame ──

function loop() {
  raf = 0;
  if (!open) return;
  try {
    frame();
  } catch (err) {
    console.warn("[streamer] frame failed:", err);
  }
  raf = requestAnimationFrame(loop);
}

function frame() {
  const job = currentJobId;
  if (job !== shownJob) {
    shownJob = job;
    for (const k of Object.keys(painted)) delete painted[k];
    if (job) showSceneFor(job);
    // The last song's export result is not this one's (an export still
    // running says whose take it is: paintExport).
    if (!exporting) {
      exportNote = "";
      exportError = null;
    }
    painted.exp = null;
    if (job) checkLastTake(job);
    loadPart(job);
    // Another song: the review starts again from its note.
    releaseReview();
  }
  const hasTrack = Boolean(job && transport() && totalDuration > 0);
  if (painted.hasTrack !== hasTrack) {
    painted.hasTrack = hasTrack;
    root.classList.toggle("sm-empty-view", !hasTrack);
    ui.noTrack.hidden = hasTrack;
    // No song: the message covers the view and nothing under it is reachable.
    for (const node of [ui.center, ui.side.aside]) node.toggleAttribute("inert", !hasTrack);
    if (!hasTrack) toggleDrawer(false);
  }
  // A take filming one song while another opens stops (and is kept with its
  // song): once, not every frame while it is stopping.
  if (takeOpen() && recordState().jobId && recordState().jobId !== job) {
    if (!songChangeStop) {
      songChangeStop = true;
      stopRecording();
      flashNote(t("streamer.rec.songChanged"), ui.builder.rec);
    }
  } else if (!takeOpen()) songChangeStop = false;
  const tNow = hasTrack ? now() : 0;
  paintTransport(hasTrack);
  paintDevices();
  paintMeter();
  syncTakeHold(job);
  reviewTake(job);
  takeTick(tNow);
  feedLive(tNow);
  paintRecord();
  paintReview(hasTrack ? tNow : 0);
  // One window (a narrow workspace) showing the take: the live picture is not drawn.
  if (ui.builder.screens.dataset.shows !== "take") paintPreview();
  paintClock(tNow);
  paintTimeline(tNow);
  paintPart();
}

let songChangeStop = false;
let lastOutMs = -Infinity;
let hiddenTimer = 0;

/**
 * While a take is open: the output frame, drawn when the next one is due at
 * the take's frame rate (the canvas stream takes no more than that), and the
 * stop at the trim's out point. From the frame loop, or from a timer while
 * the window is hidden (no frames are drawn there).
 */
function takeTick(tNow) {
  if (!takeOpen()) return;
  const nowMs = performance.now();
  const fps = Number(settings.fps) || 30;
  if (nowMs - lastOutMs >= 1000 / fps - 4) {
    lastOutMs = nowMs;
    // The take films the camera's picture, without the live window's guides.
    drawOutput(drawTakeFrame);
  }
  const st = recordState();
  if (st.state === "recording" && shouldAutoStop({ t: tNow, range: trimRange(scene, totalDuration), duration: totalDuration, autoStop: settings.autoStopAtEnd })) {
    stopRecording();
  }
}

/** A hidden window draws no frames: a take keeps its picture and its
 * auto-stop going from a timer (about ten times a second) until it shows. */
function onVisibility() {
  clearInterval(hiddenTimer);
  hiddenTimer = 0;
  if (!document.hidden || !open) return;
  hiddenTimer = setInterval(() => {
    if (!document.hidden || !open || !takeOpen()) {
      clearInterval(hiddenTimer);
      hiddenTimer = 0;
      return;
    }
    try { takeTick(now()); } catch (err) { console.warn("[streamer] hidden take tick failed:", err); }
  }, 100);
}

function paintTransport(hasTrack) {
  const playing = Boolean(hasTrack && transport()?.isPlaying?.());
  const sig = `${playing}|${hasTrack}|${t("streamer.play")}`;
  if (painted.transport === sig) return;
  painted.transport = sig;
  const { play, stop } = ui.head;
  play.replaceChildren(icon(playing ? ICONS.pause : ICONS.play, { size: 16 }));
  const label = t(playing ? "streamer.pause" : "streamer.play");
  play.setAttribute("aria-label", label);
  play.title = label;
  play.disabled = !hasTrack;
  stop.disabled = !hasTrack;
}

// ── Live window ──

/** The stage's options at `w` x `h`; `editor` null for the take. */
function stageOptions(w, h, editor) {
  return {
    width: w,
    height: h,
    font: ui.font,
    labels: {
      camera: t("streamer.cameraPlaceholder"),
      actionSafe: t("streamer.actionSafe"),
      titleSafe: t("streamer.titleSafe"),
    },
    editor,
  };
}

/** One recorded frame: the camera's picture at the output size. */
function drawTakeFrame(ctx, w, h) {
  renderStage(ctx, cameraSource(), stageOptions(w, h, null));
}

/** The live window: the camera at the scene's aspect, pillarboxed or
 * letterboxed in the 16:9 window, with the safe zones when they are on. */
function paintPreview() {
  const { preview, canvas, rec } = ui.builder;
  const cw = preview.clientWidth;
  const ch = preview.clientHeight;
  if (!cw || !ch) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  // A live camera is a new picture every frame.
  const cam = cameraSource();
  const sig = [cw, ch, dpr, safeZones, scene.aspect, t("streamer.cameraPlaceholder"), cam ? cam.currentTime : -1].join("§");
  if (painted.scene === sig) return;
  painted.scene = sig;
  if (canvas.width !== Math.round(cw * dpr) || canvas.height !== Math.round(ch * dpr)) {
    canvas.width = Math.round(cw * dpr);
    canvas.height = Math.round(ch * dpr);
  }
  const ctx = ui.ctx;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#000000";
  ctx.fillRect(0, 0, cw, ch);
  const f = fitAspect(cw, ch, scene.aspect);
  ctx.save();
  ctx.translate(f.x, f.y);
  ctx.beginPath();
  ctx.rect(0, 0, f.w, f.h);
  ctx.clip();
  renderStage(ctx, cam, stageOptions(f.w, f.h, { safeZones }));
  ctx.restore();
  // The REC badge sits on the picture's own top left corner.
  rec.style.left = `${Math.round(f.x + 10)}px`;
  rec.style.top = `${Math.round(f.y + 10)}px`;
}

// ── Take review ──
// The second window: the take on the Video lane, its recorded picture played
// in step with the transport (reviewModel.js). Silent: the take's sound is
// already in the mix. Its <video> is built on first use and let go when the
// view closes.

const review = { video: null, src: "", engaged: false, pick: null, tier: "c", win: 0, view: "none", wasPlaying: false };

function reviewVideo() {
  if (!review.video) {
    const v = document.createElement("video");
    v.muted = true;
    v.playsInline = true;
    v.preload = "auto";
    v.addEventListener("seeked", () => { painted.review = null; });
    v.addEventListener("loadeddata", () => { painted.review = null; });
    v.addEventListener("error", () => console.warn("[streamer] the take's video failed to load:", v.error?.message || v.error?.code));
    review.video = v;
  }
  return review.video;
}

/** Let the video and its source go (closing the view, another song). */
function releaseReview() {
  const v = review.video;
  if (v) {
    v.pause();
    v.removeAttribute("src");
    v.load();
  }
  review.video = null;
  review.src = "";
  review.engaged = false;
  review.pick = null;
  // Playback already going (another song chosen while playing) waits for
  // the next press of Play.
  review.wasPlaying = Boolean(transport()?.isPlaying?.());
  painted.review = null;
}

/** Stop: the review goes back to its note until Play (or a seek). */
function stopReview() {
  review.engaged = false;
  review.pick = null;
  painted.review = null;
}

/** A seek from this view (the ruler, a section, the arrow keys). */
function seekTo(sec) {
  setPlayheadTime(sec);
  review.engaged = true;
  painted.review = null;
}

/** Which windows show, from the Scene Builder's size (reviewModel.reviewTier). */
function placeWindows() {
  if (!ui || !open) return;
  const { panel, row, screens } = ui.builder;
  if (!panel.clientWidth || !panel.clientHeight) return;
  const cs = getComputedStyle(panel);
  const px = (name, fallback) => {
    const v = parseFloat(cs.getPropertyValue(name));
    return Number.isFinite(v) ? v : fallback;
  };
  const stacked = getComputedStyle(row).flexDirection === "column";
  const { tier, win } = reviewTier({
    width: panel.clientWidth,
    height: panel.clientHeight,
    pairGap: parseFloat(getComputedStyle(screens).columnGap) || 12,
    colGap: px("--sm-col-gap", 12),
    stripH: px("--sm-strip-h", 40),
    gap: px("--sm-gap", 8),
    tlMin: px("--sm-tl-min", 217),
    stacked,
  });
  if (review.tier === tier && review.win === win) return;
  review.tier = tier;
  review.win = win;
  screens.dataset.tier = tier;
  panel.dataset.tier = tier;
  // The narrow band's own CSS sizes the column.
  if (stacked) panel.style.removeProperty("--sm-win-w");
  else panel.style.setProperty("--sm-win-w", `${win}px`);
  painted.review = null;
  painted.scene = null;
}

/** The take the review shows: the Video lane's (the last take, as the song's
 * lanes loaded it). { hasTake, pending, take } */
function reviewSource() {
  const lane = takeLane();
  if (lane.state !== "saved") return { hasTake: false, pending: false, take: null };
  const take = serverTake(lane.take);
  return { hasTake: true, pending: !take, take };
}

function paintReview(tNow) {
  const r = ui.builder.review;
  const playing = Boolean(transport()?.isPlaying?.());
  const recording = takeOpen();
  const src = reviewSource();
  const take = src.take;
  const hasVideo = Boolean(take?.has_camera && take.camera_url && Number.isFinite(take.camera_start_sec));
  // Play pressed (the transport starting) engages the review; playback that
  // was already going when the song changed does not (releaseReview).
  if (recording) review.engaged = false;
  else if (playing && !review.wasPlaying && src.hasTake) review.engaged = true;
  review.wasPlaying = playing;
  const view = reviewView({ hasTake: src.hasTake, pending: src.pending, hasVideo, recording, playing: playing && review.engaged, engaged: review.engaged });
  if (!showsTake(view)) review.pick = null;
  review.view = view;
  const shows = compactShows({ tier: review.tier, view, pick: review.pick });
  const viewSig = `${view}|${shows}|${review.tier}|${reviewNote(view) ? t(reviewNote(view)) : ""}`;
  if (painted.reviewView !== viewSig) {
    painted.reviewView = viewSig;
    r.box.dataset.view = view;
    ui.builder.screens.dataset.shows = shows;
    const key = reviewNote(view);
    r.note.hidden = !key;
    r.note.textContent = key ? t(key) : "";
    r.switchBox.hidden = review.tier !== "c" || !showsTake(view);
    for (const b of [r.live, r.take]) b.setAttribute("aria-pressed", String(b.dataset.pick === shows));
    painted.review = null;
    painted.scene = null;
  }
  const v = review.video;
  if (!showsTake(view) || !hasVideo) {
    if (v && !v.paused) v.pause();
    if (painted.review !== "blank") {
      painted.review = "blank";
      clearReviewCanvas();
    }
    return;
  }
  const video = reviewVideo();
  if (review.src !== take.camera_url) {
    review.src = take.camera_url;
    video.src = take.camera_url;
    painted.review = null;
  }
  const filmDur = films.get(take.camera_url)?.loaded?.meta?.duration;
  const dur = Number.isFinite(video.duration) ? video.duration : Number.isFinite(filmDur) ? filmDur : Infinity;
  const { at, edge } = takeVideoTime(tNow, take.camera_start_sec, dur);
  if (video.readyState >= 1) {
    const step = syncStep({ playing, inTake: edge === "in", current: video.currentTime, target: at, paused: video.paused, speed: playbackSpeed });
    if (Math.abs(video.playbackRate - step.rate) > 1e-3) video.playbackRate = step.rate;
    if (step.seek != null && !video.seeking) video.currentTime = step.seek;
    if (step.pause) video.pause();
    if (step.play) video.play().catch(() => {});
  }
  if (shows === "live") return;
  drawReview(video, edge);
}

function clearReviewCanvas() {
  const { canvas, ctx, box } = ui.builder.review;
  const cw = box.clientWidth;
  const ch = box.clientHeight;
  if (!cw || !ch) return;
  sizeCanvas(canvas, cw, ch);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
}

function sizeCanvas(canvas, cw, ch) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = Math.round(cw * dpr);
  const h = Math.round(ch * dpr);
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  return dpr;
}

/** The take's frame, letterboxed at its own aspect (what the export renders);
 * outside the take the first or last frame, dimmed. Only on a new frame. */
function drawReview(video, edge) {
  const { canvas, ctx, box } = ui.builder.review;
  const cw = box.clientWidth;
  const ch = box.clientHeight;
  if (!cw || !ch || video.readyState < 2) return;
  const sig = `${cw}|${ch}|${edge}|${video.currentTime}|${video.videoWidth}x${video.videoHeight}`;
  if (painted.review === sig) return;
  painted.review = sig;
  const dpr = sizeCanvas(canvas, cw, ch);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#000000";
  ctx.fillRect(0, 0, cw, ch);
  const f = fitRatio(cw, ch, video.videoWidth / video.videoHeight);
  ctx.drawImage(video, f.x, f.y, f.w, f.h);
  if (edge !== "in") {
    ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
    ctx.fillRect(f.x, f.y, f.w, f.h);
  }
}

// A drag of a trim handle in progress: a key that edits the scene (undo)
// ends it first, put back where it began, so no half-made step is left
// behind or undone twice.
let liveDrag = null;
function cancelLiveDrag() {
  const d = liveDrag;
  liveDrag = null;
  d?.cancel();
}

function paintClock(tNow) {
  const a = clockTenths(tNow);
  if (painted.clock !== a) {
    painted.clock = a;
    ui.builder.clockNow.textContent = a;
    ui.builder.timeline.headTime.textContent = a;
  }
  const d = `/ ${clockTenths(totalDuration || 0)}`;
  if (painted.clockDur !== d) {
    painted.clockDur = d;
    ui.builder.clockDur.textContent = d;
  }
}

// ── Recording and output settings, devices, the meter ──

async function loadSettings() {
  try {
    settings = parseRecordingSettings(await storeGet(SETTINGS_KEY, null));
  } catch (err) {
    console.warn("[streamer] reading the recording settings failed:", err);
    settings = defaultRecordingSettings();
  }
  painted.devices = null;
}

function setSetting(patch) {
  settings = applySettings(settings, patch);
  storeSet(SETTINGS_KEY, serializeRecordingSettings(settings));
  painted.devices = null;
}

/** Allow (no permission yet) or Retry (it failed) for one kind. */
function deviceAction(kind) {
  const st = kind === "video" ? cameraState() : inputState();
  if (!st.mayAsk && !st.reason) allowDevices();
  else retryDevice(kind);
  painted.devices = null;
}

/** The camera's name without the USB id Chromium adds on Windows. */
function cameraName(label) {
  return String(label || "").replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$/i, "") || t("streamer.cameraPlaceholder");
}

// The devices a take opened with: kept as they are until it ends (no select
// change closes the camera or the input mid-take).
let takeDevices = null;

/** What the view wants open now: the camera chosen in Video Source (none
 * for No camera) and the input, while Streamer is open. */
function wantedDevices() {
  if (takeOpen() && takeDevices) return takeDevices;
  const lists = deviceLists();
  const camera = settings.videoDeviceId !== NONE ? resolveDeviceId(settings.videoDeviceId, lists.video) : null;
  // The input the take records (the studio's choice until one is picked here).
  const input = recordingInput();
  const audio = input ? input.deviceId : null;
  const audioChannelCount = input ? Math.max(...input.channels) + 1 : 2;
  return { camera, audio, settings, audioChannelCount };
}

// The inputs as the studio lists them (trackInputs.normaliseDevices), from
// the browser's list, kept until that list changes.
let inputs = { raw: null, devices: [] };
function inputDevices() {
  const raw = deviceLists().audio;
  if (inputs.raw !== raw) inputs = { raw, devices: normaliseDevices(raw) };
  return inputs.devices;
}

/** The input and channels a take records from now, or null (no input). */
function recordingInput() {
  const devices = inputDevices();
  return resolveInput(settings, devices, defaultInputDevice(devices, "audio"));
}

/** The Audio Source select: no input, then the studio's list (a microphone,
 * each channel and pair of an interface), the input in use selected. */
let inputChoices = new Map();
function fillInputChoice(sel) {
  const devices = inputDevices();
  const cur = recordingInput();
  const sig = `${devices.map((d) => `${d.deviceId}:${d.name}:${d.channels}`).join("|")}|${t("streamer.noAudioInput")}|${t("rec.input.voiceGroup")}`;
  if (sel.dataset.sig !== sig) {
    sel.dataset.sig = sig;
    inputChoices = devices.length ? fillInputSelect(sel, devices, { withChannels: true }) : new Map();
    if (!devices.length) {
      sel.replaceChildren();
      // Before permission the names are hidden: the Settings input is used.
      const o = el("option", null, t("capture.defaultDevice"));
      o.value = "";
      sel.append(o);
    }
    const none = el("option", null, t("streamer.noAudioInput"));
    none.value = NONE;
    sel.prepend(none);
  }
  const want = cur ? inputValue(cur.deviceId, cur.channels) : NONE;
  sel.value = [...sel.options].some((o) => o.value === want) ? want : (cur ? "" : NONE);
}

function chooseInput(value) {
  if (value === NONE) {
    setSetting({ audioDeviceId: NONE, audioChannels: null });
    return;
  }
  const picked = inputChoices.get(value);
  if (picked) setSetting({ audioDeviceId: picked.device.deviceId, audioChannels: picked.channels });
  else setSetting({ audioDeviceId: "", audioChannels: null });
}

function fillDeviceSelect(sel, kind, devices, saved) {
  const video = kind === "video";
  const sig = `${devices.map((d) => `${d.deviceId}:${d.label}`).join("|")}|${t("capture.defaultDevice")}`;
  if (sel.dataset.sig !== sig) {
    sel.dataset.sig = sig;
    sel.replaceChildren();
    const add = (value, text) => {
      const o = el("option", null, text);
      o.value = value;
      sel.append(o);
    };
    add(NONE, t(video ? "streamer.noCamera" : "streamer.noAudioInput"));
    add("", t("capture.defaultDevice"));
    devices.forEach((d, i) => add(d.deviceId, video
      ? (d.label ? cameraName(d.label) : t("capture.cameraN", { n: i + 1 }))
      : (d.label ? friendlyName(d.label) : t("capture.inputN", { n: i + 1 }))));
  }
  const want = resolveDeviceId(saved, devices);
  const has = [...sel.options].some((o) => o.value === want);
  sel.value = has ? want : "";
}

/** The sentence for a device's state, or "" when it is fine. */
function deviceMessage(kind, st, selectedNone) {
  if (selectedNone) return "";
  if (st.reason === "recording") return t("streamer.inUse");
  if (st.reason === "unplugged") return t(kind === "video" ? "streamer.cam.unplugged" : "streamer.mic.unplugged");
  if (st.reason) return reasonMessage(st.reason, kind);
  if (!st.mayAsk) return t("streamer.allowHint");
  if (st.pending) return t(kind === "video" ? "streamer.cam.starting" : "streamer.mic.starting");
  return "";
}

function actionKey(st) {
  if (st.reason === "recording") return null;
  if (st.reason) return "streamer.retry";
  if (!st.mayAsk) return "streamer.allowDevices";
  return null;
}

function paintDeviceLine(line, kind, st, selectedNone, note = "") {
  const msg = note || deviceMessage(kind, st, selectedNone || (kind === "audio" && st.noEngine));
  line.line.hidden = !msg;
  line.line.classList.toggle("sm-dev-bad", Boolean(st.reason));
  if (line.text.textContent !== msg) line.text.textContent = msg;
  const key = note ? null : actionKey(st);
  line.act.hidden = !key || !msg;
  if (key) line.act.textContent = t(key);
}

function paintDevices() {
  reconcile(wantedDevices());
  const c = cameraState();
  const a = inputState();
  const lists = deviceLists();
  const sig = JSON.stringify([c, a, settings, lists.video.length, lists.audio.length, lists.video.map((d) => d.label), lists.audio.map((d) => d.label), t("streamer.retry")]);
  if (painted.devices === sig) return;
  painted.devices = sig;
  const side = ui.side;
  fillDeviceSelect(side.vsrc, "video", lists.video, settings.videoDeviceId);
  fillInputChoice(side.asrc);
  side.res.value = settings.resolution;
  side.fps.value = String(settings.fps);
  for (const [key, s2] of Object.entries(side.sw)) s2.set(settings[key]);
  side.heavy.hidden = !isHeavy(settings);
  paintDeviceLine(side.vstate, "video", c, settings.videoDeviceId === NONE);
  // One Allow is enough: it opens both. The input's line says it only when
  // the camera's does not.
  const askBoth = !c.mayAsk && !c.reason && !a.mayAsk && !a.reason && settings.videoDeviceId !== NONE;
  // No input chosen: the take still records, with the song only.
  const noInput = settings.audioDeviceId === NONE ? t("streamer.mic.none") : "";
  paintDeviceLine(side.astate, "audio", a, settings.audioDeviceId === NONE || askBoth, noInput);
  // Over the live window, while a camera is chosen and is not showing.
  const b = ui.builder;
  const msg = settings.videoDeviceId !== NONE && !c.live ? deviceMessage("video", c, false) : "";
  b.camState.hidden = !msg;
  b.camText.textContent = msg;
  const key = actionKey(c);
  b.camAct.hidden = !key;
  if (key) b.camAct.textContent = t(key);
  painted.scene = null;
}

/** The meter: the input's first two channels, each held and falling back,
 * segments lit by the dB scale; only changed segments are touched. */
let autoPick = null;
let meterInput = null;
function paintMeter() {
  const side = ui.side;
  const live = inputState().live;
  const nowMs = performance.now();
  // The meter shows the channels the take records (a second row: the pair's
  // other half, else the same channel); the same input the take lane draws.
  if (!meterInput || painted.meterSettings !== settings || painted.meterInputs !== inputs.devices) {
    painted.meterSettings = settings;
    painted.meterInputs = inputs.devices;
    meterInput = recordingInput();
  }
  const chans = meterInput?.channels || [0];
  autoPickInput(live);
  let top = 0;
  side.meterRows.forEach((row, c) => {
    holdPeak(row.peak, live ? inputLevel(chans[c] ?? chans[0]) : 0, nowMs);
    top = Math.max(top, row.peak.value);
    const lit = litSegments(meterFraction(row.peak.value));
    if (lit === row.lit) return;
    const from = Math.min(lit, row.lit);
    const to = Math.max(lit, row.lit);
    for (let k = from; k < to; k++) row.segs[k].classList.toggle("on", k < lit);
    row.lit = lit;
  });
  const recFrac = takeOpen() ? Math.round(meterFraction(top) * 50) / 50 : 0;
  if (painted.recLevel !== recFrac) {
    painted.recLevel = recFrac;
    ui.builder.recLevel.style.transform = `scaleX(${recFrac})`;
  }
  const db = levelDb(top);
  if (painted.meterDb !== db) {
    painted.meterDb = db;
    side.level.textContent = `${db} dB`;
    side.meter.setAttribute("aria-valuenow", String(db));
  }
}

/** An input not picked by hand that stays silent while another channel of
 * the same device carries a signal moves to it, once (the studio's rule). */
function autoPickInput(live) {
  const cap = live ? streamerMedia().capture : null;
  if (!cap || !meterInput?.auto || meterInput.kind === "voice" || takeOpen()) {
    autoPick = null;
    return;
  }
  const step = autoPickStep(autoPick, meterInput.channels, (ch) => cap.channelLevel([ch]), cap.deviceChannels || 1);
  autoPick = step.state;
  if (step.pick < 0) return;
  setSetting({ audioDeviceId: meterInput.deviceId, audioChannels: [step.pick] });
  flashNote(t("streamer.mic.autoInput", { input: channelText([step.pick]) }), ui.side.asrc);
}

/** Phase 4's seam: the camera and input streams Streamer has open (or null). */
export function streamerMedia() {
  return getStreamerMedia();
}

// ── Recording and export ──

// The Performance track's take plays with the backing, so the musician can
// review it (recordModel.takeHoldStep). While a NEW take records, the last
// one is held silent (mixer.holdLaneSilent: never a mute, never saved into
// any mode's record) and let go when the take ends.
let heldTake = null;

/** Hold or let go of the last take for the take now open (or not). From the
 * frame loop and from every recorder state change (the view may be closed). */
function syncTakeHold(job) {
  const open2 = takeOpen();
  const id = open2 ? streamerTrackId(recordState().jobId || job) : null;
  const step = takeHoldStep(heldTake, { open: open2, trackId: id });
  if (step.release) holdLaneSilent(step.release, false);
  if (step.hold) holdLaneSilent(step.hold, true);
  heldTake = step.held;
}

// A take just saved: { job, take }, until its track plays unmuted in
// Performance's mix (reviewStep). A mix saved muted by an earlier version,
// which muted this track every frame, is unmuted the same way.
let reviewPending = null;

function reviewTake(job) {
  const p = reviewPending;
  if (!p) return;
  const id = streamerTrackId(p.job);
  // The song reloads after the take with a new engine: done once that engine
  // is ready (its start put the playhead at 0). Twenty seconds without a new
  // engine count as done (nothing reloaded).
  const eng = audioEngine;
  if (eng && eng !== p.engine && p.waiting !== eng) {
    p.waiting = eng;
    Promise.resolve(eng.ready).then(() => { if (reviewPending === p && p.waiting === eng) p.readyEngine = eng; }, () => {});
  }
  const reloaded = (p.readyEngine && p.readyEngine === audioEngine) || performance.now() - p.at > 20000;
  const step = reviewStep({
    pending: p,
    job,
    mode: getMixMode(),
    switching: isMixSwitching(),
    open: takeOpen(),
    loaded: Boolean(id && trackIndex[id] !== undefined && mixerState[id]),
    peaksReady: Boolean(reloaded && overviewPeaks()?.data?.[p.take]),
    muted: Boolean(id && mixerState[id]?.muted),
  });
  if (step === "wait") return;
  const from = p.from;
  reviewPending = null;
  if (step === "unmute") toggleStemMute(id);
  painted.takeMute = null;
  // The song's reload after the take put the playhead at 0: back to where
  // the take began, so Play plays it with the backing at once.
  if (Number.isFinite(from) && step !== "drop" && job && !transport()?.isPlaying?.()) setPlayheadTime(from);
}

/** The take lane's mute: the take track's own mute in Performance's mix. */
function toggleTakeMute() {
  const job = currentJobId;
  const id = job && streamerTrackId(job);
  if (!id || takeOpen() || !mixerState[id] || trackIndex[id] === undefined) return;
  if (getMixMode() !== "streamer" || isMixSwitching()) return;
  toggleStemMute(id);
  painted.takeMute = null;
}

const BLOCK_KEY = {
  "tempo": "streamer.rec.tempo",
  "no-engine": "streamer.rec.noSong",
  "unsupported": "streamer.rec.unsupported",
  "busy": "streamer.rec.busy",
};

async function toggleRecording() {
  if (takeOpen()) {
    stopRecording();
    return;
  }
  const badge = ui.builder.rec;
  const job = currentJobId;
  if (!job || !totalDuration) {
    flashNote(t("streamer.rec.noSong"), badge);
    return;
  }
  const block = recordBlock();
  if (block) {
    flashNote(t(BLOCK_KEY[block] || "streamer.rec.failed"), badge);
    return;
  }
  const media = streamerMedia();
  // The last take is held silent from the "start" state change on
  // (syncTakeHold); a track the start finds on the server, from the next frame.
  const res = await startRecording({
    camera: cameraUse({ live: cameraState().live }),
    remove: part.job === job ? part.remove : "",
    jobId: job,
    capture: inputState().live ? media.capture : null,
    channels: recordingInput()?.channels || [0],
    settings,
    aspect: scene.aspect,
    range: trimRange(scene, totalDuration),
    trackName: t("streamer.trackName"),
    draw: drawTakeFrame,
  });
  if (!res.ok) flashNote(t(BLOCK_KEY[res.reason] || "streamer.rec.failed"), badge);
  else if (!inputState().live) flashNote(t("streamer.rec.noMic"), badge);
}

let wasOpen = false;
/** The REC badge, the lock while a take is open, and the export block. */
function paintRecord() {
  const st = recordState();
  const open2 = takeOpen();
  if (open2 !== wasOpen) {
    wasOpen = open2;
    // While recording: no new canvas size, camera or input.
    root.classList.toggle("sm-locked", open2);
    ui.head.aspect.disabled = open2;
    ui.side.res.disabled = open2;
    ui.side.fps.disabled = open2;
    ui.side.vsrc.disabled = open2;
    ui.side.asrc.disabled = open2;
    painted.part = null;
    takeDevices = open2 ? { ...wantedDevices() } : null;
    // A new take: the last one's export error is history.
    if (open2) {
      exportError = null;
      exportNote = "";
    }
    paintHistory();
    painted.devices = null;
    if (!open2 && st.hidden) flashNote(t("streamer.rec.hidden"), ui.builder.rec);
    else if (!open2 && st.stopReason === "transport") flashNote(t("streamer.rec.stopped"), ui.builder.rec);
  }
  // Back from a hidden window during a take: say the picture froze there.
  if (open2 && st.hidden && !document.hidden && !painted.hiddenWarned) {
    painted.hiddenWarned = true;
    flashNote(t("streamer.rec.hidden"), ui.builder.rec);
  }
  if (!open2) painted.hiddenWarned = false;
  const b = ui.builder;
  const recording = st.state === "recording" || st.state === "starting" || st.state === "stopping";
  const time = clockHours(st.elapsedSec);
  const sig = `${st.state}|${time}|${t("streamer.recReady")}`;
  if (painted.rec !== sig) {
    painted.rec = sig;
    b.rec.classList.toggle("recording", recording);
    b.rec.setAttribute("aria-pressed", String(recording));
    b.recLabel.textContent = t(recording ? "streamer.recOn" : "streamer.recReady");
    b.rec.setAttribute("aria-label", t(recording ? "streamer.recStop" : "streamer.recStart"));
    b.rec.title = b.rec.getAttribute("aria-label");
    b.recTime.textContent = time;
  }
  paintExport(st);
}

// The export in progress: { jobId, takeId, abort, raw }.
let exporting = null;
let exportNote = "";
let exportError = null; // { code, raw }

function paintExport(st) {
  const s = ui.side;
  const job = currentJobId;
  const take = job ? lastTake(job) : null;
  const busy = Boolean(exporting);
  const elsewhere = busy && exporting.jobId !== job;
  const shot = take ? takeInfo(take) : null;
  const asRecorded = shot && shot.aspect !== scene.aspect ? shot.aspect : "";
  const sig = `${st.state}|${take}|${busy}|${elsewhere}|${exportNote}|${exportError?.code || ""}|${asRecorded}|${t("streamer.export.go")}`;
  if (painted.exp === sig) return;
  painted.exp = sig;
  // Always there: the one way out of Streamer (export to a file), waiting
  // for a take when there is none.
  s.exp.hidden = false;
  if (takeOpen()) {
    s.expText.textContent = t(st.state === "stopping" ? "streamer.rec.saving" : "streamer.rec.recording");
    s.expBar.hidden = true;
    for (const btn of [s.expGo, s.expCancel, s.expReveal, s.expRaw]) btn.hidden = true;
    return;
  }
  s.expGo.hidden = busy;
  s.expGo.disabled = !take;
  s.expGo.textContent = t(exportError && exportError.code !== "no-encoder" ? "streamer.export.retry" : "streamer.export.go");
  s.expCancel.hidden = !busy;
  s.expBar.hidden = !busy;
  s.expReveal.hidden = !(st.state === "exported" && canRevealSaved());
  s.expRaw.hidden = exportError?.code !== "no-encoder";
  // A take recorded at another aspect than the scene's now is exported as
  // it was recorded (never stretched), and says so.
  const ready = asRecorded ? t("streamer.export.readyAsRecorded", { aspect: asRecorded }) : t("streamer.export.ready");
  s.expText.textContent = elsewhere
    ? t("streamer.export.otherSong", { note: exportNote })
    : exportNote || (take ? ready : t("streamer.export.none"));
}

async function runExport() {
  const job = currentJobId;
  const takeId = job ? lastTake(job) : null;
  if (!takeId || exporting || takeOpen()) return;
  const meta = trackMeta();
  const abort = new AbortController();
  exporting = { jobId: job, takeId, abort };
  exportError = null;
  exportNote = t("streamer.export.rendering", { pct: 0 });
  markExport("export", { jobId: job, takeId });
  forgetSaved();
  painted.exp = null;
  const bar = ui.side.expBar;
  bar.value = 0;
  try {
    // The take's own shape: the aspect, size and rate it was recorded at
    // (the scene may have changed since); never stretched.
    const shot = recordedShape(takeId);
    const body = renderRequest({
      trackId: streamerTrackId(job),
      settings: { ...settings, resolution: shot.resolution, fps: shot.fps },
      aspect: shot.aspect,
      // The part left out while it was recorded: the export's backing is the same.
      remove: shot.remove ?? (part.job === job ? part.remove : ""),
      range: trimRange(scene, totalDuration),
    });
    const res = await exportTake({
      jobId: job,
      takeId,
      body,
      filename: exportFileName(meta.title, meta.artist),
      signal: abort.signal,
      onProgress: (f) => {
        bar.value = f;
        exportNote = t("streamer.export.rendering", { pct: Math.round(f * 100) });
        painted.exp = null;
      },
      onTransferStart: () => {
        exportNote = t("streamer.export.saving");
        painted.exp = null;
      },
    });
    // Not saved (the save dialog was cancelled): back to ready, and Show in
    // folder does not point at an older file.
    markExport(res.saved ? "done" : "cancel");
    // Saved, with what made the video ("Encoded with NVIDIA GPU." or the
    // GPU fallback sentence) when the server said.
    const encoded = res.saved ? encoderNote(res, t) : "";
    exportNote = !res.saved
      ? t("streamer.export.notSaved")
      : encoded ? t("streamer.export.savedWith", { note: encoded }) : t("streamer.export.saved");
  } catch (err) {
    console.warn("[streamer] export failed:", err);
    if (err?.code === "cancelled") {
      markExport("cancel");
      exportNote = t("streamer.export.cancelled");
    } else if (err?.code === "gone") {
      // The take was deleted (in Advanced): nothing to export any more.
      markExport("fail");
      forgetTake(job, takeId);
      exportNote = t("streamer.export.takeGone");
    } else {
      markExport("fail");
      exportError = { code: err?.code || "failed" };
      // The server's reason, when it gave one, after the sentence.
      const reason = exportReason(err);
      exportNote = err?.code === "no-encoder" ? t("streamer.export.noEncoder")
        : reason ? t("streamer.export.failedWhy", { reason }) : t("streamer.export.failed");
    }
  } finally {
    exporting = null;
    painted.exp = null;
  }
}

/** What `takeId` was recorded at, else the current scene and settings. */
function recordedShape(takeId) {
  return takeInfo(takeId) || { aspect: scene.aspect, resolution: settings.resolution, fps: settings.fps, remove: null };
}

/** A short, readable reason from a failed export (the server's detail), or "". */
function exportReason(err) {
  const text = String(err?.message || "").replace(/\s+/g, " ").trim();
  if (!text || text === "render failed" || /^(failed|none)$/i.test(text)) return "";
  return text.length > 160 ? `${text.slice(0, 157)}...` : text;
}

/** The song's last take is still on the server (it can be deleted in
 * Advanced): when not, it is not offered for export. */
async function checkLastTake(job) {
  const takeId = lastTake(job);
  if (!takeId) return;
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(job)}`, { cache: "no-store" });
    if (!res.ok) return;
    const takes = (await res.json())?.takes;
    if (!Array.isArray(takes)) return;
    const ids = takes.map((tk) => tk?.id ?? tk?.take_id);
    if (!ids.includes(takeId) && lastTake(job) === takeId && !takeOpen()) {
      forgetTake(job, takeId);
      painted.exp = null;
    }
  } catch (err) {
    console.warn("[streamer] checking the last take failed:", err);
  }
}

function cancelRunningExport() {
  if (!exporting) return;
  const { jobId, takeId, abort } = exporting;
  abort.abort();
  cancelExport(jobId, takeId);
}

function saveRaw() {
  const job = currentJobId;
  const takeId = job ? lastTake(job) : null;
  if (!takeId) return;
  const meta = trackMeta();
  // Named with the extension of what the browser actually recorded.
  rawRecordingType(job, takeId)
    .then((type) => saveRawRecording(job, takeId, exportFileName(meta.title, meta.artist, rawExtension(type))))
    .then((saved) => {
      exportNote = saved === false ? t("streamer.export.notSaved") : t("streamer.export.rawSaved");
      painted.exp = null;
    })
    .catch((err) => {
      console.warn("[streamer] saving the raw recording failed:", err);
      exportNote = t("streamer.export.failed");
      painted.exp = null;
    });
}

// ── Take timeline ──

/** The two lanes: the backing track (every stem but the part played) and the
 * part the musician plays (none when nothing is removed). */
function laneSpecs() {
  const present = part.job === currentJobId ? part.present : [];
  const remove = part.job === currentJobId ? part.remove : "";
  const { backing, part: played } = timelineLanes(remove, present);
  const out = [];
  if (backing.length) out.push({ key: `b:${backing.join(",")}`, names: backing, label: t("streamer.backingTrack"), color: "#9aa3ad" });
  else out.push(null);
  // The take lane: the musician's part (or their take over the whole song),
  // never the stem file's wave: empty, forming live, then the take's own.
  const label = played ? t("streamer.lane.yourPart", { name: partName(played) }) : t("streamer.lane.yourTake");
  const color = (played && STEM_COLORS[played]) || ui.accent;
  const lane = takeLane();
  if (lane.state === "live" || (lane.state === "saved" && !lane.peaksReady && live.take === lane.take && live.take)) {
    out.push({ key: `l:${live.bucket}:${live.take || ""}`, live: true, label, color });
  } else if (lane.state === "saved" && lane.peaksReady) {
    out.push({ key: `s:${lane.take}`, names: [lane.take], label, color, take: true });
  } else {
    out.push({ key: "e", empty: true, label, color });
  }
  return out;
}

// The live waveform of the take recording now: the input's peak per stretch
// of the song, at the playhead (inputModel.js). One store, reused per take.
const livePeaks = createLivePeaks(3000);
let liveBars = new Float32Array(0);
const live = { take: null, bucket: -1, active: false };

/** The take lane's state: { state, take, info, peaksReady }. */
function takeLane() {
  const job = currentJobId;
  const take = job ? lastTake(job) : null;
  const info = take ? takeInfo(take) : null;
  const remove = part.job === job ? part.remove : "";
  const state = takeLaneState({ recording: takeOpen(), lastTake: take, takePart: info ? info.remove : null, part: remove });
  const peaksReady = Boolean(take && overviewPeaks()?.data?.[take]);
  return { state, take, info, peaksReady };
}

/** Each frame of a take: the input's level into the live waveform. */
function feedLive(tNow) {
  const st = recordState();
  if (!takeOpen()) {
    if (live.active) {
      live.active = false;
      // The take that was just recorded keeps its live wave until its own
      // peaks arrive with the song's reload.
      live.take = st.takeId || lastTake(currentJobId) || live.take;
      painted.lanes = null;
    }
    return;
  }
  if (!live.active) {
    live.active = true;
    live.take = null;
    live.bucket = -1;
    resetLivePeaks(livePeaks, totalDuration);
    painted.lanes = null;
  }
  if (st.state !== "recording") return;
  const cap = streamerMedia().capture;
  const chans = recordingInput()?.channels || [0];
  const peak = cap && !cap.closed ? cap.channelLevel(chans) : 0;
  // A quiet floor so the formed part reads even where it is soft.
  const v = peak > 0 ? Math.max(0.04, meterFraction(peak)) : 0;
  const b = addLivePeak(livePeaks, tNow, v);
  if (b !== live.bucket) live.bucket = b;
}

// ── I play: the part left out of the backing ──

// The open song's choice: { job, remove, present (stems with a file), loaded }.
let part = { job: null, remove: "", present: [], loaded: false };
let partToken = 0;

/** The song's stems on disk and Streamer's saved choice for it. */
async function loadPart(job) {
  const token = ++partToken;
  part = { job, remove: "", present: [], loaded: false };
  painted.part = null;
  painted.lanes = null;
  if (!job) {
    applyPart();
    return;
  }
  let present = [];
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(job)}`, { cache: "no-store" });
    const data = res.ok ? await res.json() : null;
    present = (data?.stems || []).filter((s) => s?.url && BASE_STEMS.includes(s.name)).map((s) => s.name);
  } catch (err) {
    console.warn("[streamer] reading the song's stems failed:", err);
  }
  let stored = null;
  try { stored = await storeGet(choiceKey(job, PART_MODE), null); } catch (err) {
    console.warn("[streamer] reading the part choice failed:", err);
  }
  if (token !== partToken) return;
  part = { job, remove: effectiveChoice(parseChoice(stored), present), present, loaded: true, record: stored };
  painted.part = null;
  painted.lanes = null;
  applyPart();
}

/** The musician chose what they play: saved for this song in Streamer, and
 * heard now. Refused while a take is open (the take hears what it began with). */
function choosePart(value) {
  if (takeOpen()) {
    flashNote(t("streamer.rec.stopFirst"), ui.builder.timeline.partSel);
    painted.part = null;
    return;
  }
  const job = part.job;
  if (!job || job !== currentJobId || !part.loaded) return;
  const remove = effectiveChoice({ remove: value }, part.present);
  if (remove === part.remove) return;
  // The rest of the record (the voice form a lead/backing split froze,
  // modeStems.js) is kept: only "I play" changes here.
  const record = serializeChoice({ ...parseChoice(part.record), remove });
  part = { ...part, remove, record };
  storeSet(choiceKey(job, PART_MODE), record);
  painted.part = null;
  painted.lanes = null;
  // The backing file is made now, so the export does not wait for it.
  if (remove) fetch(`/api/jobs/${encodeURIComponent(job)}/backing/${remove}.wav`, { method: "HEAD" }).catch(() => {});
  applyPart();
}

/**
 * Make what is heard match the choice: while Streamer is open, the open
 * song's playback leaves the part out (player.js setPlaybackExclusion, read
 * when the song loads, so the song reloads); closed, nothing is left out.
 * Only the open song is ever affected, and only playback: no mode's saved
 * mix changes.
 */
function applyPart() {
  const job = currentJobId;
  const want = open && job && part.job === job && part.loaded ? excludedNames(part.remove) : new Set();
  const have = playbackExclusion();
  const haveForJob = have.jobId === job ? have.names : new Set();
  if (sameNames(want, haveForJob)) {
    // Another song's leftover choice is dropped without a reload.
    if (have.jobId && have.jobId !== job) setPlaybackExclusion(null, new Set());
    return;
  }
  setPlaybackExclusion(job, want);
  if (!job) return;
  // The place (playhead, play state, loop) is kept across the reload by the
  // same helper the modes use (reloadPlace.js). It runs on a timer, not the
  // frame loop: a close reloads too, after the loop has stopped.
  reloadKeepingPlace(job);
}

/** A part the musician plays, as "I play" names it: Keys and Voice, the
 * instrument families, not the piano and vocals stem names the rest of the
 * app shows. */
function partName(stem) {
  if (stem === "piano") return t("streamer.part.keys");
  if (stem === "vocals") return t("streamer.part.voice");
  return laneLabel(stem);
}

function paintPart() {
  const sel = ui.builder.timeline.partSel;
  const offered = part.job === currentJobId ? offeredChoices(part.present) : [""];
  const sig = `${offered.join(",")}|${part.remove}|${part.loaded}|${takeOpen()}|${t("streamer.part.none")}`;
  if (painted.part === sig) return;
  painted.part = sig;
  sel.replaceChildren(...offered.map((c) => {
    const o = el("option", null, c ? partName(c) : t("streamer.part.none"));
    o.value = c;
    return o;
  }));
  sel.value = part.remove;
  sel.disabled = takeOpen() || !part.loaded || offered.length < 2;
  sel.title = part.loaded && offered.length < 2 ? t("streamer.part.noStems") : t("streamer.part.aria");
}

/** The label column and its gap: the left edge of the song's column. */
function labelCol() {
  const tl = ui.builder.timeline;
  return (tl.mask.offsetWidth || 82) + 8;
}

function timelineScale() {
  const tl = ui.builder.timeline;
  const view = Math.max(1, tl.scroll.clientWidth - labelCol());
  const dur = totalDuration || 0;
  return { dur, view, pps: dur ? pixelsPerSecond(dur, view, zoom) : 0 };
}

function setZoom(z) {
  const next = Math.round(Math.max(0, Math.min(1, z)) * 100) / 100;
  if (next === zoom) return;
  zoom = next;
  zoomAnchor = now();
  painted.tlScale = null;
}

function wireZoomSlider(track) {
  const at = (clientX) => {
    const r = track.getBoundingClientRect();
    setZoom((clientX - r.left) / (r.width || 1));
  };
  track.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    track.setPointerCapture(e.pointerId);
    at(e.clientX);
    const move = (ev) => at(ev.clientX);
    const up = () => {
      track.removeEventListener("pointermove", move);
      track.removeEventListener("pointerup", up);
      track.removeEventListener("pointercancel", up);
    };
    track.addEventListener("pointermove", move);
    track.addEventListener("pointerup", up);
    track.addEventListener("pointercancel", up);
  });
  track.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -ZOOM_STEP, ArrowDown: -ZOOM_STEP, ArrowRight: ZOOM_STEP, ArrowUp: ZOOM_STEP }[e.key];
    if (step === undefined && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "Home") setZoom(0);
    else if (e.key === "End") setZoom(1);
    else setZoom(zoom + step);
  });
}

function wireRuler(ruler) {
  const seekAt = (clientX) => {
    const { dur, pps } = timelineScale();
    if (!dur || !pps) return;
    const r = ruler.getBoundingClientRect();
    seekTo(Math.max(0, Math.min(dur, (clientX - r.left) / pps)));
  };
  ruler.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    ruler.setPointerCapture(e.pointerId);
    seekAt(e.clientX);
    const move = (ev) => seekAt(ev.clientX);
    const up = () => {
      ruler.removeEventListener("pointermove", move);
      ruler.removeEventListener("pointerup", up);
      ruler.removeEventListener("pointercancel", up);
    };
    ruler.addEventListener("pointermove", move);
    ruler.addEventListener("pointerup", up);
    ruler.addEventListener("pointercancel", up);
  });
  ruler.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    e.stopPropagation();
    seekTo(now() + (e.key === "ArrowLeft" ? -5 : 5));
  });
}

/** Where a trim point snaps: the song's ends and its sections' starts and
 * ends first; else its beats, while the beat grid's Snap is on. */
function snapTrim(time, dur, reach) {
  const inSong = (v) => Number.isFinite(v) && v >= 0 && v <= dur;
  const marks = [0, dur];
  for (const s of getSections()) marks.push(s.start, s.end);
  const snapped = snapTime(time, marks.filter(inSong), reach);
  if (snapped !== time || !getBeatGridSnap()) return snapped;
  return snapTime(time, getBeats().filter(inSong), reach);
}

/** Drag the in or out handle; it snaps to sections, then beats (Alt: no snapping). One
 * undo step per drag; the arrows move it a tenth (Shift: a second). */
function wireTrimHandle(handle, which) {
  let drag = null;
  const showTip = (time) => {
    const tip = ui.builder.timeline.tip;
    tip.hidden = false;
    tip.textContent = t(which === "in" ? "streamer.trimIn" : "streamer.trimOut", { time: clockTenths(time) });
    const { pps } = timelineScale();
    tip.style.left = `${labelCol() + time * pps}px`;
  };
  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    // The trim a take is filming to stays put until it ends.
    if (takeOpen()) return;
    const { dur, pps } = timelineScale();
    if (!dur || !pps) return;
    handle.setPointerCapture(e.pointerId);
    // Where on the handle it was taken: the point does not jump to the pointer.
    const audio = ui.builder.timeline.audio.getBoundingClientRect();
    const point = trimRange(scene, dur)[which];
    drag = { base: scene, dur, pps, offset: (e.clientX - audio.left) / pps - point };
    showTip(point);
    const pointerId = e.pointerId;
    liveDrag = {
      cancel: () => {
        if (!drag) return;
        scene = drag.base;
        drag = null;
        painted.tlTrim = null;
        ui.builder.timeline.tip.hidden = true;
        try { handle.releasePointerCapture(pointerId); } catch { /* already released */ }
      },
    };
  });
  handle.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const audio = ui.builder.timeline.audio.getBoundingClientRect();
    let time = (e.clientX - audio.left) / drag.pps - drag.offset;
    if (!e.altKey) time = snapTrim(time, drag.dur, 8 / drag.pps);
    scene = withTrimPoint(drag.base, which, time, drag.dur);
    painted.tlTrim = null;
    showTip(trimRange(scene, drag.dur)[which]);
  });
  const end = () => {
    if (!drag) return;
    const d = drag;
    drag = null;
    liveDrag = null;
    ui.builder.timeline.tip.hidden = true;
    if (scene !== d.base) {
      const result = scene;
      scene = d.base;
      commit(result, d.base);
    }
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);
  handle.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    e.stopPropagation();
    if (takeOpen()) return;
    const dur = totalDuration || 0;
    const step = (e.shiftKey ? 1 : 0.1) * (e.key === "ArrowLeft" ? -1 : 1);
    commit(withTrimPoint(scene, which, trimRange(scene, dur)[which] + step, dur));
  });
}

function paintTimeline(tNow) {
  const tl = ui.builder.timeline;
  const { dur, view, pps } = timelineScale();
  const x = (time) => Math.max(0, Math.min(dur, time)) * pps;
  const contentW = Math.max(view, Math.round(dur * pps));

  // The scale: the song column's width and the zoom control.
  const scaleSig = `${dur}|${view}|${zoom}`;
  if (painted.tlScale !== scaleSig) {
    painted.tlScale = scaleSig;
    tl.grid.style.setProperty("--sm-tl-w", `${contentW}px`);
    const pct = `${Math.round(zoom * 100)}%`;
    tl.zFill.style.width = pct;
    tl.zThumb.style.left = pct;
    tl.zTrack.setAttribute("aria-valuenow", String(Math.round(zoom * 100)));
    tl.zOut.disabled = zoom <= 0;
    tl.zIn.disabled = zoom >= 1;
    painted.sections = null;
    painted.lanes = null;
    painted.tlTrim = null;
  }
  if (zoomAnchor !== null) {
    tl.scroll.scrollLeft = Math.max(0, x(zoomAnchor) - view / 2);
    zoomAnchor = null;
  }
  // While playing, the view follows the playhead.
  const headX = x(tNow);
  if (zoom > 0 && transport()?.isPlaying?.()) {
    const left = tl.scroll.scrollLeft;
    if (headX < left || headX > left + view - 24) tl.scroll.scrollLeft = Math.max(0, headX - 40);
  }
  tl.head.style.left = `${headX}px`;
  // The time label stays whole at the song's two ends.
  let labelLeft = Math.max(-34, -headX);
  if (headX + labelLeft + 68 > contentW) labelLeft = contentW - headX - 68;
  tl.headTime.style.left = `${labelLeft}px`;
  const ruler = tl.ruler;
  const tInt = String(Math.floor(tNow));
  if (painted.rulerNow !== tInt) {
    painted.rulerNow = tInt;
    ruler.setAttribute("aria-valuenow", tInt);
    ruler.setAttribute("aria-valuetext", clockTenths(tNow));
  }

  // Ruler ticks and section markers: on a new song, scale or set of sections.
  const sections = getSections();
  const secSig = `${dur}|${pps}|${sections.map((s) => `${s.id}:${s.start}:${s.end}:${s.color}:${s.kind || ""}:${s.name || ""}`).join(",")}|${t("streamer.jumpTo")}`;
  if (painted.sections !== secSig) {
    painted.sections = secSig;
    ruler.setAttribute("aria-valuemax", String(Math.floor(dur)));
    const ticks = dur ? ticksEvery(dur, tickStep(pps)) : [];
    ruler.replaceChildren(...ticks.flatMap((tk, i, all) => {
      const label = el("span", "sm-tick-label", tk.label);
      label.style.left = `${x(tk.t)}px`;
      if (i === 0) label.classList.add("first");
      else if (i === all.length - 1 && contentW - x(tk.t) < 30) label.classList.add("last");
      const mark = el("span", "sm-tick");
      mark.style.left = `${x(tk.t)}px`;
      return [label, mark];
    }));
    // Where each label starts, for hiding the ones under the label column.
    tl.edges = [...ruler.querySelectorAll(".sm-tick-label")].map((lab) => {
      const w = lab.offsetWidth;
      const at = parseFloat(lab.style.left);
      const shift = lab.classList.contains("first") ? 0 : lab.classList.contains("last") ? 0.9 : 0.5;
      return { el: lab, left: at - w * shift };
    });
    tl.markers.replaceChildren(...sections.flatMap((s) => {
      const name = sectionDisplayName(s, sections);
      const at = clockTenths(s.start).slice(0, 5);
      const b = button("sm-marker");
      // A marker at 0:00 keeps its whole pin right of the label column.
      b.style.left = `${Math.max(7, x(s.start))}px`;
      b.style.setProperty("--sm-sec", s.color || "#4a7fff");
      b.setAttribute("aria-label", t("streamer.jumpTo", { name, time: at }));
      const label = el("span", "sm-marker-text");
      label.append(el("span", "sm-marker-name", name), el("span", "sm-marker-time", at));
      b.append(el("span", "sm-marker-pin"), label);
      b.addEventListener("click", () => seekTo(s.start));
      const stem = el("span", "sm-marker-stem");
      stem.style.left = `${x(s.start)}px`;
      stem.style.setProperty("--sm-sec", s.color || "#4a7fff");
      return [b, stem];
    }));
    for (const b of tl.markers.querySelectorAll(".sm-marker")) tl.edges.push({ el: b, left: parseFloat(b.style.left) - 7 });
    tl.noSections.hidden = !dur || sections.length > 0;
    painted.tlEdges = null;
  }
  // A ruler label or section marker that would sit partly under the label
  // column (scrolled past) is hidden whole, never shown cut in half.
  const scrolled = tl.scroll.scrollLeft;
  if (painted.tlEdges !== scrolled && tl.edges) {
    painted.tlEdges = scrolled;
    for (const e of tl.edges) e.el.style.visibility = scrolled > 0 && e.left < scrolled - 0.5 ? "hidden" : "";
  }

  // Trim: the kept box, its handles and the dimmed parts.
  const range = trimRange(scene, dur);
  const trimSig = `${range.in}|${range.out}|${pps}|${contentW}`;
  if (painted.tlTrim !== trimSig) {
    painted.tlTrim = trimSig;
    const a = x(range.in);
    const b = x(range.out);
    tl.range.style.left = `${a}px`;
    tl.range.style.width = `${Math.max(0, b - a)}px`;
    tl.dimL.style.width = `${a}px`;
    tl.dimR.style.left = `${b}px`;
    tl.dimR.style.width = `${Math.max(0, contentW - b)}px`;
    for (const [h, v] of [[tl.trimIn, range.in], [tl.trimOut, range.out]]) {
      h.setAttribute("aria-valuemin", "0");
      h.setAttribute("aria-valuemax", String(Math.floor(dur)));
      h.setAttribute("aria-valuenow", String(Math.round(v * 10) / 10));
      h.setAttribute("aria-valuetext", clockTenths(v));
    }
  }

  // Stem lanes: only the part in view is drawn (a whole song at full zoom
  // would be wider than a canvas may be).
  const src = overviewPeaks();
  const specs = laneSpecs();
  const left = tl.scroll.scrollLeft;
  const h = tl.audio.clientHeight;
  // Each lane redraws only when what it shows changed (the live lane as its
  // waveform grows a bucket, about every 75 ms of a 4-minute song).
  const base = `${view}x${h}|${left}|${pps}`;
  if (!painted.lanes || painted.lanesSrc !== src) {
    painted.lanesSrc = src;
    for (const lane of tl.lanes) lane.sig = null;
  }
  painted.lanes = "drawn";
  tl.lanes.forEach((lane, i) => {
    const sp = specs[i] || null;
    const sig = `${sp ? `${sp.key}:${sp.label}:${sp.color}` : "-"}|${base}`;
    if (lane.sig === sig) return;
    lane.sig = sig;
    drawLane(lane, sp, src, left, view, pps, dur);
  });
  const hint = Boolean(specs[1]?.empty && dur);
  if (tl.takeHint.hidden === hint) tl.takeHint.hidden = !hint;
  const noStems = !dur || !part.loaded || Boolean(specs[0]);
  if (tl.noStems.hidden !== noStems) tl.noStems.hidden = noStems;
  paintTakeBlock(x, dur);
  paintTakeMute();
}

/** The take lane's mute button: shown with a saved take on a loaded track,
 * pressed when the take track is muted, off while a take records. */
function paintTakeMute() {
  const btn = ui.builder.timeline.takeMute;
  const job = currentJobId;
  const id = job && streamerTrackId(job);
  const show = Boolean(id && takeLane().state === "saved" && mixerState[id] && trackIndex[id] !== undefined);
  const muted = Boolean(show && mixerState[id].muted);
  const sig = `${show}|${muted}|${takeOpen()}`;
  if (painted.takeMute === sig) return;
  painted.takeMute = sig;
  btn.hidden = !show;
  btn.disabled = takeOpen();
  btn.classList.toggle("off", muted);
  btn.setAttribute("aria-pressed", String(muted));
}

// Each take's filmstrip, asked once ("loading", "ok" with `loaded`, or
// "failed"); a failure is asked once more a few seconds later (the server
// may still be writing the camera part).
const films = new Map();
const FILM_RETRY_MS = 4000;

function filmFor(take) {
  const key = take?.camera_url;
  if (!key) return null;
  let f = films.get(key);
  if (f && f.status === "failed" && f.tries < 2 && performance.now() - f.at > FILM_RETRY_MS) f = null;
  if (f) return f;
  const tries = (films.get(key)?.tries || 0) + 1;
  f = { status: "loading", loaded: null, tries, at: performance.now() };
  films.set(key, f);
  const entry = loadFilmstrip(take);
  const done = (loaded) => {
    f.status = "ok";
    f.loaded = loaded;
    painted.takeBlock = null;
  };
  if (entry instanceof Promise) {
    entry.then(done).catch((err) => {
      console.warn("[streamer] the take's frames failed:", err);
      f.status = "failed";
      f.at = performance.now();
      painted.takeBlock = null;
    });
  } else done(entry);
  return f;
}

/** The server's record of the take lane's take (the Performance track's
 * active take, as the song's lanes loaded it), or null. */
function serverTake(take) {
  const id = currentJobId && streamerTrackId(currentJobId);
  if (!id || !take) return null;
  const active = getTracks().find((tr) => tr.id === id)?.active;
  return active && (active.id ?? active.take_id) === take ? active : null;
}

/** The take on the video lane: growing while it records, then its frames
 * from the camera part's first frame, or a note saying what is missing. */
function paintTakeBlock(x, dur) {
  const tl = ui.builder.timeline;
  const block = tl.takeBlock;
  const lane = takeLane();
  const take = lane.state === "saved" ? serverTake(lane.take) : null;
  const f = take?.has_camera ? filmFor(take) : null;
  const state = videoLaneState({ lane: lane.state, take, film: f?.status ?? null });
  let span = null;
  if (state === "recording") {
    if (recordState().state === "recording" && livePeaks.from >= 0) span = { from: (livePeaks.from / livePeaks.peaks.length) * dur, to: now() };
  } else if (state === "film") {
    span = filmSpan(take.camera_start_sec, f.loaded.meta.duration, dur);
  } else if (state === "loading" || state === "failed" || state === "pending") {
    // Where the take sits, from the server's record, else this browser's.
    const meta = f?.loaded?.meta;
    if (take?.has_camera && meta) span = filmSpan(take.camera_start_sec, meta.duration, dur);
    else if (Number.isFinite(lane.info?.from) && Number.isFinite(lane.info?.to)) span = { from: lane.info.from, to: lane.info.to };
  }
  const noteKey = videoLaneNote(state, lane.info?.camera ?? null);
  const sig = `${state}|${span ? `${Math.round(x(span.from))}|${Math.round(x(span.to))}` : "-"}|${lane.take}|${noteKey}|${t(noteKey || "streamer.trackVideo")}`;
  if (painted.takeBlock === sig) return;
  painted.takeBlock = sig;
  tl.video.dataset.state = state;
  tl.videoNote.hidden = !noteKey;
  tl.videoNote.textContent = noteKey ? t(noteKey) : "";
  block.hidden = !span;
  if (!span) return;
  block.classList.toggle("live", state === "recording");
  block.classList.toggle("failed", state === "failed");
  const left = x(span.from);
  // At least a few pixels: a half-second take still shows where it is.
  const width = Math.max(6, x(span.to) - left);
  block.style.left = `${left}px`;
  block.style.width = `${width}px`;
  tl.film.hidden = state !== "film";
  if (state === "film") {
    const from = Math.max(0, span.from - take.camera_start_sec);
    drawFilmstrip(tl.film, f.loaded, from, span.to - span.from);
  }
}

/** One lane: `spec` { names, label, color } (the peaks of `names`), or null. */
function drawLane(lane, spec, src, left, view, pps, dur) {
  lane.key = spec?.key ?? null;
  lane.label.hidden = !spec;
  lane.name.textContent = spec ? spec.label : "";
  lane.label.title = spec ? spec.label : "";
  const color = spec?.color || "#9aa3ad";
  lane.label.style.setProperty("--sm-lane", color);
  const c = lane.canvas;
  const w = Math.max(0, Math.min(view, dur * pps - left) - 32);
  c.style.left = `${left + 16}px`;
  c.style.width = `${w}px`;
  const h = c.clientHeight;
  if (!w || !h) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  c.width = Math.round(w * dpr);
  c.height = Math.round(h * dpr);
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!spec || spec.empty || !dur || !pps) return;
  const n = Math.max(1, Math.floor(w / 3));
  const from = (left + 16) / pps / dur;
  const to = (left + 16 + w) / pps / dur;
  let bars;
  if (spec.live) {
    // The live take: the buckets recorded so far (none drawn past them).
    if (liveBars.length !== n) liveBars = new Float32Array(n);
    bars = fillLiveBars(livePeaks, liveBars, from, to);
  } else {
    const names = (spec.names || []).filter((nm) => src?.data?.[nm]);
    if (!names.length) return;
    bars = peakBars(src.data, names, n, from, to);
  }
  // A take's lane draws only where the take has sound: no bars before or
  // after it, where a stem lane draws its quiet floor.
  drawBars(ctx, bars, n, w, h, color, Boolean(spec.live || spec.take));
}

/** One lane's bars: the same drawing for a stem, a saved take and a take
 * forming live (where a bar of 0 is not recorded yet and draws nothing). */
function drawBars(ctx, bars, n, w, h, color, skipEmpty) {
  const step = w / n;
  ctx.fillStyle = color;
  for (let i = 0; i < n; i++) {
    if (skipEmpty && !(bars[i] > 0)) continue;
    const bh = Math.min(h, ((3 + bars[i] * 20) / 26) * h);
    ctx.fillRect(i * step, (h - bh) / 2, Math.max(1, step - 1), bh);
  }
}

// ── Keys (only while Streamer Mode is open) ──

function isTyping(target) {
  return target instanceof HTMLElement
    && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

/** Streamer has the keys when focus is in it, or nowhere (after a click on
 * the view's empty parts). Not in the library, a dialog or another menu. */
function streamerHasFocus(e) {
  const a = document.activeElement;
  if (root.contains(e.target) || root.contains(a)) return true;
  return !a || a === document.body || a === document.documentElement;
}

function onKey(e) {
  if (!open || e.defaultPrevented) return;
  const mod = e.ctrlKey || e.metaKey;
  const typing = isTyping(e.target);
  // Delete and Backspace never reach the studio's "trash the open song"
  // (catalog.js) while Streamer is open, wherever focus is (the rail, the
  // library, a dialog), except where they edit text.
  if ((e.key === "Delete" || e.key === "Backspace") && !typing) {
    e.preventDefault();
    e.stopPropagation();
    return;
  }
  if (e.target instanceof Element && e.target.closest("[role=dialog], .settings-overlay, .mode-menu")) return;
  const ours = streamerHasFocus(e);

  // Undo and redo of the scene.
  if (mod && !e.altKey && !typing && ours && (e.code === "KeyZ" || e.code === "KeyY")) {
    e.preventDefault();
    e.stopPropagation();
    cancelLiveDrag();
    if (e.code === "KeyY" || e.shiftKey) doRedo();
    else doUndo();
    return;
  }
  // Escape closes the Output drawer, from any control in it (a select too).
  if (e.key === "Escape" && !mod && root.classList.contains("sm-drawer-open")) {
    e.preventDefault();
    e.stopPropagation();
    toggleDrawer(false);
    ui.head.output.focus({ preventScroll: true });
    return;
  }
  if (mod || e.altKey || typing) return;
  // R records or stops (Streamer's own key; nothing in the studio uses R).
  if (e.code === "KeyR" && ours && !e.repeat) {
    e.preventDefault();
    e.stopPropagation();
    toggleRecording();
    return;
  }
  // Space or Enter on one of this view's own controls presses that control
  // only: the studio's Space would also start or stop playback.
  if ((e.code === "Space" || e.key === "Enter") && e.target instanceof Element
      && e.target.closest("#streamer button, #streamer select, #streamer [role=switch], #streamer [role=slider], #streamer [role=button]")) {
    e.stopPropagation();
  }
}

// ── Init ──

export function initStreamer() {
  // Karaoke opened (the Mode menu): one view at a time.
  document.addEventListener("stemdeck:karaoke-changed", (e) => {
    if (e.detail?.open && open) closeStreamer();
  });
  window.addEventListener("pagehide", () => { flushScene(sceneJob); });
  loadSettings();
  onRecordChange((state) => {
    painted.rec = null;
    painted.exp = null;
    syncTakeHold(currentJobId);
    if (state === "saved") {
      const st = recordState();
      if (st.jobId && st.takeId) reviewPending = { job: st.jobId, take: st.takeId, from: takeInfo(st.takeId)?.from ?? null, engine: audioEngine, at: performance.now() };
    }
    if (state === "failed") flashNote(t("streamer.rec.failed"), ui?.builder.rec);
  });
  onDevicesChange(() => {
    painted.devices = null;
  });
  onLanguageChange(() => relabel());
  document.addEventListener("visibilitychange", onVisibility);
}
