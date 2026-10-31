// Camera in the record flow, and "Make video" for a take that has one.
//
// What the user sees, inside #videoPanel (a slot in the recording panel):
//   - a Camera toggle for the next take and a live preview tile (which
//     camera, its size and the preview mirror are chosen in Settings >
//     Recording, static/js/capture/recordingSettings.js);
//   - while a take records with the camera on, a REC badge on the preview;
//   - a list of finished takes that have a camera file, each with a sync
//     nudge in milliseconds, Make video, progress and Download MP4.
//
// The take itself belongs to static/js/capture/takeRecorder.js: it creates
// it, tells us when audio frame 0 is stamped ("armed"), and awaits our
// participant before /finish so the camera parts are on the server first.
// Every touch of the capture layer goes through ./captureAdapter.js.

import { currentJobId, mixerState } from "../state.js";
import { t, applyTranslations, onLanguageChange } from "../i18n.js";
import { showError } from "../job.js";
import { currentMixLanes, triggerDownload } from "../player.js";
import { exportFxParam } from "../eqEngine.js";
import { exportMasterParam } from "../masterEngine.js";
import {
  requestCamera, renderCameraPermission, getRememberedCamera,
  onTakeEvent, registerTakeParticipant, uploadCameraPart,
} from "./captureAdapter.js";
import { getCameraQuality, getMirrorPreview, SETTINGS_EVENT } from "../capture/captureSettings.js";
import { startCameraRecording, recorderSupported } from "./cameraRecorder.js";
import {
  cameraConstraints, QUALITY, DEFAULT_QUALITY, clampOffsetMs, takeHasVideo,
  OFFSET_MIN_MS, OFFSET_MAX_MS, FRAME_RATE,
} from "./videoCodec.js";
import {
  saveCameraOffset, requestRender, renderStatus, waitForRender, renderDownloadUrl, cancelRender,
} from "./videoRender.js";

const FRAME_MS = Math.round(1000 / FRAME_RATE);

// How long the video is: the take's own span (the default) or the whole song.
// Remembered per browser, as a convenience only: the server's default is
// "take" when nothing is sent.
const SPAN_KEY = "stemdeck:video:span";
const SPANS = ["take", "song"];

function _storedSpan() {
  try {
    const v = window.localStorage.getItem(SPAN_KEY);
    return SPANS.includes(v) ? v : "take";
  } catch (err) {
    console.warn("[video] reading the video length choice failed:", err);
    return "take";
  }
}

function _storeSpan(value) {
  try {
    window.localStorage.setItem(SPAN_KEY, value);
  } catch (err) {
    console.warn("[video] saving the video length choice failed:", err);
  }
}

const ui = {};
let stream = null;
let active = null; // { jobId, takeId, rec } while a take films
let takesJobId = null;
let renderAbort = null;

function _el(tag, cls, attrs = {}) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  for (const [k, v] of Object.entries(attrs)) {
    if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  return el;
}

// ── Panel ───────────────────────────────────────────────────────────────

function _mountPoint() {
  let panel = document.getElementById("videoPanel");
  if (!panel) {
    // #recPanel provides the slot. Without it, sit at
    // the end of the track header so the feature is still reachable.
    panel = _el("div", "video-panel", { id: "videoPanel" });
    const host = document.getElementById("recPanel") || document.getElementById("transport");
    if (!host) return null;
    host.appendChild(panel);
  }
  return panel;
}

function _buildPanel(panel) {
  panel.textContent = "";
  panel.hidden = false;

  const row = _el("div", "vid-row");
  ui.toggle = _el("button", "vid-toggle", {
    type: "button", "aria-pressed": "false", "data-i18n-title": "video.cameraOnTitle",
  });
  ui.toggle.innerHTML =
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
    '<path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>';
  const label = _el("span", "vid-toggle-label", { "data-i18n": "video.camera" });
  label.textContent = "Camera";
  ui.toggle.appendChild(label);

  row.append(ui.toggle);

  ui.previewWrap = _el("div", "vid-preview-wrap", { hidden: true });
  ui.preview = _el("video", "vid-preview", {
    muted: true, playsinline: true, autoplay: true, "data-i18n-aria-label": "video.previewAria",
  });
  ui.preview.muted = true;
  ui.rec = _el("span", "vid-rec-badge", { hidden: true, "data-i18n": "video.recording" });
  ui.rec.textContent = "Recording video";
  ui.previewWrap.append(ui.preview, ui.rec);

  ui.status = _el("p", "vid-status", { "aria-live": "polite" });

  ui.takes = _el("div", "vid-takes", { hidden: true });

  panel.append(row, ui.previewWrap, ui.status, ui.takes);
  applyTranslations(panel);
  _applyMirror();
  _setDeviceControlsEnabled(true);
}

function _applyMirror() {
  ui.preview.classList.toggle("vid-mirrored", getMirrorPreview());
}

function _quality() {
  const q = getCameraQuality();
  return QUALITY[q] ? q : DEFAULT_QUALITY;
}

function _setStatus(text) {
  ui.status.textContent = text || "";
  ui.status.hidden = !text;
}

function _setDeviceControlsEnabled(on) {
  ui.toggle.disabled = !on;
}

// ── Camera on / off ─────────────────────────────────────────────────────

function _stopStream() {
  if (stream) {
    for (const tr of stream.getTracks()) tr.stop();
  }
  stream = null;
  ui.preview.srcObject = null;
  ui.previewWrap.hidden = true;
  ui.toggle.setAttribute("aria-pressed", "false");
  ui.toggle.classList.remove("on");
  ui.toggle.setAttribute("data-i18n-title", "video.cameraOnTitle");
  ui.toggle.title = t("video.cameraOnTitle");
}

async function _startStream() {
  if (!recorderSupported()) {
    _setStatus(t("video.unsupported"));
    return false;
  }
  _setStatus("");
  // Release the old stream first: a camera held by one stream can refuse a
  // second open with other settings (NotReadableError, not verified per OS).
  _stopStream();
  const { stream: s, reason } = await requestCamera(cameraConstraints(_quality(), (await getRememberedCamera()) || ""));
  if (!s) {
    ui.status.hidden = false;
    await renderCameraPermission(ui.status, reason, t("video.blocked"));
    return false;
  }
  stream = s;
  const track = stream.getVideoTracks()[0];
  track?.addEventListener("ended", () => {
    // Unplugged or taken by the OS. A take in progress keeps what it has.
    if (stream && stream.getVideoTracks()[0] === track) {
      _stopStream();
      _setStatus(t("video.cameraLost"));
    }
  });
  ui.preview.srcObject = stream;
  ui.preview.play?.().catch((err) => console.warn("[video] preview play failed:", err));
  ui.previewWrap.hidden = false;
  ui.toggle.setAttribute("aria-pressed", "true");
  ui.toggle.classList.add("on");
  ui.toggle.setAttribute("data-i18n-title", "video.cameraOffTitle");
  ui.toggle.title = t("video.cameraOffTitle");
  return true;
}

function cameraOn() {
  return Boolean(stream && stream.getVideoTracks().some((tr) => tr.readyState === "live"));
}

/**
 * Record calls this before a video track's take: the camera on, ready to
 * film. True when it is live; false when it could not open (the camera box
 * in the panel then says why).
 */
export async function ensureCameraOn() {
  if (!ui.toggle) return false;
  if (cameraOn()) return true;
  return _startStream();
}

// ── Take lifecycle ──────────────────────────────────────────────────────

function _onTake(ev) {
  if (ev.type === "armed") {
    // With several tracks recording, the camera films the first one's take.
    if (ev.primary === false) return;
    if (!cameraOn() || active) return;
    const jobId = ev.jobId;
    const takeId = ev.takeId;
    const track = stream.getVideoTracks()[0];
    try {
      const rec = startCameraRecording({
        stream,
        jobId,
        takeId,
        perfMsAtFrame0: ev.perfMsAtFrame0,
        ctxTimeAtFrame0: ev.ctxTimeAtFrame0,
        bitsPerSecond: QUALITY[_quality()].bitsPerSecond,
        deviceLabel: track?.label || "",
        mirrorPreview: getMirrorPreview(),
        previewVideo: ui.preview,
        upload: (seq, blob) => uploadCameraPart(jobId, takeId, seq, blob),
      });
      active = { jobId, takeId, rec };
      ui.rec.hidden = false;
      ui.previewWrap.classList.add("recording");
      _setDeviceControlsEnabled(false);
    } catch (err) {
      console.warn("[video] camera recorder failed to start:", err);
      _setStatus(t("video.recorderFailed"));
    }
    return;
  }
  if ((ev.type === "discarded" || ev.type === "failed") && active?.takeId === ev.takeId) {
    active.rec.abort();
    _endActive();
  }
  if (ev.type === "finished" || ev.type === "discarded") {
    _refreshTakes(ev.jobId || currentJobId);
  }
}

function _endActive() {
  active = null;
  if (document.getElementById("recPanel")?.hidden) _stopStream();
  ui.rec.hidden = true;
  ui.previewWrap.classList.remove("recording");
  _setDeviceControlsEnabled(true);
}

async function _participant(takeId) {
  if (!active || active.takeId !== takeId) return null;
  const { rec } = active;
  _setStatus(t("video.uploading"));
  try {
    const block = await rec.stop();
    _setStatus("");
    return block;
  } catch (err) {
    console.warn("[video] camera upload failed:", err);
    _setStatus("");
    showError(t("video.uploadFailed"), String(err?.message || err));
    return null;
  } finally {
    _endActive();
  }
}

// ── Takes with video: nudge, Make video, download ───────────────────────

async function _refreshTakes(jobId) {
  if (!ui.takes) return;
  renderAbort?.abort();
  renderAbort = null;
  takesJobId = jobId || null;
  ui.takes.textContent = "";
  ui.takes.hidden = true;
  if (!jobId) return;
  let job;
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
    if (!res.ok) return;
    job = await res.json();
  } catch (err) {
    console.warn("[video] reading the job's takes failed:", err);
    return;
  }
  if (takesJobId !== jobId) return; // another track opened meanwhile
  const takes = (Array.isArray(job?.takes) ? job.takes : []).filter(takeHasVideo);
  // A take is named with its recording track ("Track 1 · Take 2"), and its
  // track tells the render whether this take is what the track lane plays.
  const tracks = new Map((Array.isArray(job?.tracks) ? job.tracks : []).map((tr) => [tr.id, tr]));
  for (const take of takes) {
    const track = tracks.get(take.track_id);
    take.displayName = track ? t("video.takeOnTrack", { track: track.name, take: take.label || take.id }) : (take.label || take.id);
    take.isActiveOnTrack = !!track && track.active_take_id === take.id;
  }
  if (!takes.length) return;
  const head = _el("div", "vid-takes-head", { "data-i18n": "video.takesTitle" });
  head.textContent = t("video.takesTitle");
  const note = _el("p", "vid-note", { "data-i18n": "video.copyright" });
  note.textContent = t("video.copyright");
  ui.takes.append(head);
  renderAbort = new AbortController();
  for (const take of takes) ui.takes.appendChild(_takeRow(jobId, take, renderAbort.signal, job.title));
  ui.takes.append(note);
  ui.takes.hidden = false;
}

// Same name the server puts in Content-Disposition (_download_name in
// app/api/takes.py: song slug, then take slug), so the desktop save dialog
// and a browser download agree.
function _slug(text) {
  return String(text || "").replace(/[^a-zA-Z0-9]+/g, "_").replace(/_{2,}/g, "_").slice(0, 80).replace(/^_+|_+$/g, "");
}

function _takeRow(jobId, take, signal, songTitle) {
  const row = _el("div", "vid-take", { "data-take-id": take.id });
  const name = _el("span", "vid-take-name");
  name.textContent = take.displayName || take.label || take.id; // user text

  const nudge = _el("div", "vid-nudge", { role: "group", "data-i18n-title": "video.nudgeTitle" });
  const nudgeLabel = _el("span", "vid-nudge-label", { "data-i18n": "video.nudge" });
  nudgeLabel.textContent = t("video.nudge");
  const minus = _el("button", "vid-step", { type: "button", "data-i18n-title": "video.stepDown", "data-i18n-aria-label": "video.stepDown" });
  minus.textContent = "−";
  const input = _el("input", "vid-offset num", {
    type: "number", step: "1", min: String(OFFSET_MIN_MS), max: String(OFFSET_MAX_MS),
    inputmode: "numeric", "data-i18n-aria-label": "video.nudgeAria",
  });
  input.value = String(clampOffsetMs(take.camera_offset_ms ?? 0));
  const plus = _el("button", "vid-step", { type: "button", "data-i18n-title": "video.stepUp", "data-i18n-aria-label": "video.stepUp" });
  plus.textContent = "+";
  const unit = _el("span", "vid-unit", { "data-i18n": "video.ms" });
  unit.textContent = t("video.ms");
  nudge.append(nudgeLabel, minus, input, unit, plus);

  const spanWrap = _el("label", "vid-span", { "data-i18n-title": "video.spanTitle" });
  spanWrap.title = t("video.spanTitle");
  const spanLabel = _el("span", "vid-span-label", { "data-i18n": "video.span" });
  spanLabel.textContent = t("video.span");
  const span = _el("select", "vid-select vid-span-select", { "data-i18n-aria-label": "video.span" });
  span.setAttribute("aria-label", t("video.span"));
  for (const value of SPANS) {
    const key = value === "take" ? "video.spanTake" : "video.spanSong";
    const opt = _el("option", "", { value, "data-i18n": key });
    opt.textContent = t(key);
    span.appendChild(opt);
  }
  span.value = _storedSpan();
  span.addEventListener("change", () => _storeSpan(span.value));
  spanWrap.append(spanLabel, span);

  const make = _el("button", "vid-make", { type: "button", "data-i18n": "video.make" });
  make.textContent = t("video.make");
  const progress = _el("progress", "vid-progress", { max: "1", value: "0", hidden: true, "data-i18n-aria-label": "video.progressAria" });
  const state = _el("span", "vid-state", { "aria-live": "polite" });
  // A whole-song render of a long take runs for minutes: it can be stopped.
  const stop = _el("button", "vid-stop", { type: "button", hidden: true, "data-i18n": "video.stop" });
  stop.textContent = t("video.stop");
  const download = _el("button", "vid-download", { type: "button", hidden: true, "data-i18n": "video.download" });
  download.textContent = t("video.download");

  const step = (d) => { input.value = String(clampOffsetMs(Number(input.value) + d)); };
  minus.addEventListener("click", () => step(-FRAME_MS));
  plus.addEventListener("click", () => step(FRAME_MS));
  input.addEventListener("change", () => { input.value = String(clampOffsetMs(input.value)); });

  let doneStatus = null;
  let stoppedByUser = false;
  const setBusy = (busy) => {
    stop.hidden = !busy;
    stop.disabled = false;
    make.disabled = busy;
    span.disabled = busy;
    input.disabled = busy;
    minus.disabled = busy;
    plus.disabled = busy;
    progress.hidden = !busy;
  };
  const showDone = (st) => {
    doneStatus = st;
    progress.hidden = true;
    state.textContent = t("video.ready");
    download.hidden = false;
  };
  const follow = async () => {
    setBusy(true);
    download.hidden = true;
    stoppedByUser = false;
    try {
      const st = await waitForRender(jobId, take.id, {
        signal,
        onProgress: (p) => {
          progress.value = p;
          state.textContent = p > 0 ? t("video.making", { percent: Math.round(p * 100) }) : t("video.queued");
        },
      });
      // Stopped: the server may still hold an earlier video, but it is not
      // the one just asked for, so nothing is offered.
      if (stoppedByUser) state.textContent = t("video.stopped");
      else showDone(st);
    } catch (err) {
      if (err?.name === "AbortError") return;
      if (stoppedByUser) {
        state.textContent = t("video.stopped");
        return;
      }
      console.warn("[video] render failed:", err);
      state.textContent = t("video.failed", { reason: String(err?.message || err) });
    } finally {
      setBusy(false);
    }
  };

  make.addEventListener("click", async () => {
    const offset = clampOffsetMs(input.value);
    input.value = String(offset);
    setBusy(true);
    state.textContent = t("video.queued");
    download.hidden = true;
    try {
      if (offset !== clampOffsetMs(take.camera_offset_ms ?? 0)) {
        await saveCameraOffset(jobId, take.id, offset);
        take.camera_offset_ms = offset;
      }
      const lanes = currentMixLanes();
      // The lane that plays this take: its track, while this is the track's
      // active take. That lane is left out of the backing (the take rides on
      // take_gain), and its level as heard is the take's level.
      const own = (n) => n === take.id || (take.isActiveOnTrack && n === take.track_id);
      const idx = lanes.names.findIndex(own);
      const laneName = take.isActiveOnTrack ? take.track_id : take.id;
      // Muted or soloed away means 0; a take no lane plays (an earlier take of
      // its track) is heard at unity.
      const takeGain = idx >= 0 ? lanes.gains[idx] : (mixerState[laneName] && take.isActiveOnTrack ? 0 : 1);
      const names = [];
      const gains = [];
      const pitches = [];
      lanes.names.forEach((n, i) => {
        if (own(n)) return;
        names.push(n);
        gains.push(Number(lanes.gains[i].toFixed(3)));
        pitches.push(lanes.pitches?.[i] ?? 0);
      });
      // The backing lanes' EQs plus the take's own track, whose EQ the server
      // puts on the take.
      const fx = exportFxParam(take.track_id ? [...names, take.track_id] : names);
      await requestRender(jobId, take.id, {
        cameraOffsetMs: offset, mix: { names, gains, pitches, lanes: lanes.lanes }, takeGain, span: span.value, fx,
        master: exportMasterParam(),
      });
    } catch (err) {
      console.warn("[video] render request failed:", err);
      state.textContent = t("video.failed", { reason: String(err?.message || err) });
      setBusy(false);
      // 409: a render for this take is already running; follow that one.
      if (err?.status !== 409) return;
    }
    await follow();
  });

  stop.addEventListener("click", async () => {
    stop.disabled = true;
    stoppedByUser = true;
    try {
      await cancelRender(jobId, take.id);
    } catch (err) {
      console.warn("[video] stopping the render failed:", err);
      stoppedByUser = false;
      stop.disabled = false;
    }
  });

  download.addEventListener("click", () => {
    const name = [_slug(songTitle), _slug(take.label)].filter(Boolean).join("_") || "take";
    triggerDownload(renderDownloadUrl(jobId, take.id, doneStatus), `${name}.mp4`);
  });

  row.append(name, nudge, spanWrap, make, progress, stop, state, download);

  // A render started earlier (or before a reload) is picked up where it is.
  renderStatus(jobId, take.id).then((st) => {
    if (signal.aborted) return;
    if (st.status === "done") showDone(st);
    else if (st.status === "queued" || st.status === "running") follow();
  }).catch((err) => console.warn("[video] render status read failed:", err));

  return row;
}

// ── Init ────────────────────────────────────────────────────────────────

export async function initVideoTake() {
  const panel = _mountPoint();
  if (!panel) return;
  _buildPanel(panel);

  ui.toggle.addEventListener("click", async () => {
    if (cameraOn()) {
      _stopStream();
      return;
    }
    ui.toggle.disabled = true;
    try {
      await _startStream();
    } finally {
      ui.toggle.disabled = Boolean(active);
    }
  });
  const reopen = async () => {
    if (cameraOn() && !active) await _startStream();
  };
  // Settings > Recording changed the camera, its size or the mirror.
  document.addEventListener(SETTINGS_EVENT, (e) => {
    const kind = e.detail?.kind;
    if (kind === "mirror") _applyMirror();
    else if (kind === "videoinput" || kind === "quality") {
      reopen().catch((err) => console.warn("[video] camera reopen failed:", err));
    }
  });
  window.addEventListener("pagehide", () => _stopStream());
  // Closing the recording panel turns the camera off, as it does the
  // microphone: the camera light must not stay on with no visible control.
  // A take that is filming keeps the camera until it stops.
  const host = document.getElementById("recPanel");
  if (host) {
    new MutationObserver(() => {
      if (host.hidden && !active && stream) _stopStream();
    }).observe(host, { attributes: true, attributeFilter: ["hidden"] });
  }

  onLanguageChange(() => {
    applyTranslations(panel);
    _refreshTakes(takesJobId);
  });
  document.addEventListener("trackopen", (e) => _refreshTakes(e.detail?.id));

  await onTakeEvent(_onTake);
  await registerTakeParticipant(_participant);
  if (currentJobId) _refreshTakes(currentJobId);
}
