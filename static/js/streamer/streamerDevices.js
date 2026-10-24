// Streamer Mode's camera and input: which devices there are, opening and
// releasing them, and why one could not be opened. The view (streamer.js)
// says each frame what it wants (`reconcile`); this module opens, switches
// and releases to match, so a device is held only while it is needed: the
// camera while one is chosen in Video Source, the input while Streamer is
// open. Closing Streamer releases both (the camera light
// goes off).
//
// Reuses the studio's capture code: requestMedia and its reasons
// (capturePermission.js), listDevices (captureDevices.js), openCapture and
// its level() meter (captureGraph.js), friendlyName (trackInputs.js).
//
// Nothing is asked of the browser on page load: getUserMedia runs only once
// the person allowed it here (the Allow button) or the browser says the
// permission is already granted.
//
// Phase 4 records from `getStreamerMedia()`.

import { requestMedia, checkCaptureSupport } from "../capture/capturePermission.js";
import { listDevices } from "../capture/captureDevices.js";
import { openCapture } from "../capture/captureGraph.js";
import { isRecording } from "../capture/recordUi.js";
import { audioEngine } from "../state.js";
import { cameraConstraints, NONE } from "./outputSettings.js";

// What is open, per kind. `reason` is why it is not (a capturePermission
// reason, or "unplugged" / "recording"); it stays until Retry, a device
// change or a new choice.
// `token` drops an open that lands after a release or a newer open.
const cam = { stream: null, video: null, deviceId: null, key: "", label: "", width: 0, height: 0, reason: null, pending: false, token: 0 };
const mic = { capture: null, deviceId: null, key: "", label: "", reason: null, pending: false, token: 0 };
// May getUserMedia run for this kind (allowed here, or granted already).
const mayAsk = { video: false, audio: false };
let lists = { video: [], audio: [], videoCount: 0, audioCount: 0 };
let listening = false;
const listeners = new Set();

function notify() {
  for (const cb of listeners) {
    try { cb(); } catch (err) { console.warn("[streamer] device listener failed:", err); }
  }
}

/** Called whenever a device, a list or a state changes. */
export function onDevicesChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

const realId = (d) => d.deviceId && d.deviceId !== "default" && d.deviceId !== "communications";

/** Read the devices again. Labels (and ids) show once permission is given. */
export async function refreshLists() {
  const [v, a] = await Promise.all([listDevices("videoinput"), listDevices("audioinput")]);
  lists = { video: v.filter(realId), audio: a.filter(realId), videoCount: v.length, audioCount: a.length };
  // A list with names means that kind is allowed already.
  if (v.some((d) => d.label)) mayAsk.video = true;
  if (a.some((d) => d.label)) mayAsk.audio = true;
  notify();
  return lists;
}

export function deviceLists() {
  return lists;
}

async function permissionState(name) {
  try {
    const p = await navigator.permissions?.query?.({ name });
    return p?.state || "unknown";
  } catch {
    return "unknown";
  }
}

const onDeviceChange = () => {
  // Plugged back in, or another one plugged: try again where that was the problem.
  for (const d of [cam, mic]) if (["unplugged", "no-device", "device-busy"].includes(d.reason)) d.reason = null;
  refreshLists();
};

/** Streamer opened: learn what is allowed, list the devices, follow plugs. */
export async function startDevices() {
  if (!listening) {
    navigator.mediaDevices?.addEventListener?.("devicechange", onDeviceChange);
    listening = true;
  }
  const [c, m] = await Promise.all([permissionState("camera"), permissionState("microphone")]);
  if (c === "granted") mayAsk.video = true;
  if (m === "granted") mayAsk.audio = true;
  if (c === "denied" && !cam.stream) cam.reason = "denied";
  if (m === "denied" && !mic.capture) mic.reason = "denied";
  await refreshLists();
}

/** Streamer closed: release everything, stop following plugs. */
export function stopDevices() {
  releaseCamera();
  releaseMic();
  if (listening) {
    navigator.mediaDevices?.removeEventListener?.("devicechange", onDeviceChange);
    listening = false;
  }
  notify();
}

/** The Allow button: getUserMedia may run now (the browser asks once). */
export function allowDevices() {
  mayAsk.video = true;
  mayAsk.audio = true;
  cam.reason = null;
  mic.reason = null;
  notify();
}

/** Retry after a failure (`kind` "video" or "audio"). */
export function retryDevice(kind) {
  const d = kind === "video" ? cam : mic;
  mayAsk[kind === "video" ? "video" : "audio"] = true;
  d.reason = null;
  d.key = "";
  notify();
}

// ── Camera ──

function videoElement() {
  if (cam.video) return cam.video;
  const v = document.createElement("video");
  v.muted = true;
  v.playsInline = true;
  v.autoplay = true;
  v.setAttribute("aria-hidden", "true");
  // Decoding needs it in the document; it is drawn into the canvas, never seen.
  v.className = "sm-cam-source";
  document.body.append(v);
  cam.video = v;
  return v;
}

function releaseCamera() {
  cam.token += 1; // an open in flight is dropped when it lands
  cam.pending = false;
  if (cam.stream) for (const tr of cam.stream.getTracks()) tr.stop();
  cam.stream = null;
  cam.deviceId = null;
  cam.key = "";
  cam.width = 0;
  cam.height = 0;
  if (cam.video) cam.video.srcObject = null;
}

async function openCamera(deviceId, settings, key) {
  const token = ++cam.token;
  cam.pending = true;
  cam.key = key;
  notify();
  let res;
  try {
    res = await requestMedia("video", cameraConstraints(settings, deviceId));
  } catch (err) {
    res = { stream: null, reason: "unknown" };
    console.warn("[streamer] the camera did not open:", err);
  }
  if (token !== cam.token) {
    res.stream?.getTracks().forEach((tr) => tr.stop());
    return;
  }
  cam.pending = false;
  if (!res.stream) {
    cam.reason = res.reason || "unknown";
    cam.key = "";
    notify();
    return;
  }
  const track = res.stream.getVideoTracks()[0];
  cam.stream = res.stream;
  cam.deviceId = deviceId;
  cam.reason = null;
  readCameraSettings(track);
  track.addEventListener("ended", () => {
    if (cam.stream !== res.stream) return;
    releaseCamera();
    cam.reason = "unplugged";
    notify();
  });
  const v = videoElement();
  v.srcObject = res.stream;
  v.play().catch((err) => console.warn("[streamer] the camera preview did not start:", err));
  mayAsk.video = true;
  refreshLists();
  notify();
}

function readCameraSettings(track) {
  const s = track?.getSettings?.() || {};
  cam.label = track?.label || "";
  cam.width = s.width || 0;
  cam.height = s.height || 0;
}

/** The camera's <video> when it has a frame to draw, else null (a renderer source). */
export function cameraSource() {
  const v = cam.video;
  return cam.stream && v && v.readyState >= 2 && v.videoWidth > 0 ? v : null;
}

/** The camera's state for the panel and the live window. */
export function cameraState() {
  return {
    live: Boolean(cam.stream),
    pending: cam.pending,
    reason: cam.reason,
    mayAsk: mayAsk.video,
    label: cam.label,
    width: cam.video?.videoWidth || cam.width,
    height: cam.video?.videoHeight || cam.height,
  };
}

// ── Input ──

function releaseMic() {
  mic.token += 1;
  mic.pending = false;
  try { mic.capture?.close(); } catch (err) { console.warn("[streamer] closing the input failed:", err); }
  mic.capture = null;
  mic.deviceId = null;
  mic.key = "";
}

async function openMic(deviceId, key, channelCount = 2) {
  const engine = audioEngine;
  if (!engine?.audioContext) return; // no song open yet: nothing to measure on
  const token = ++mic.token;
  mic.pending = true;
  mic.key = key;
  notify();
  let res;
  try {
    res = await openCapture(engine, { deviceId: deviceId || "", channelCount: Math.max(2, channelCount) });
  } catch (err) {
    res = { capture: null, reason: "unknown" };
    console.warn("[streamer] the input did not open:", err);
  }
  if (token !== mic.token) {
    res.capture?.close();
    return;
  }
  mic.pending = false;
  if (!res.capture) {
    mic.reason = res.reason || "unknown";
    // Opened on an engine that went away meanwhile (the song reloaded: a
    // take, the I play choice): not the input's fault; it opens again on
    // the new one (reconcile clears a reason from another engine).
    mic.reasonEngine = engine;
    mic.key = "";
    notify();
    return;
  }
  mic.capture = res.capture;
  mic.deviceId = deviceId;
  mic.label = res.capture.deviceLabel || "";
  mic.reason = null;
  res.capture.onDeviceLost(() => {
    if (mic.capture !== res.capture) return;
    releaseMic();
    mic.reason = "unplugged";
    notify();
  });
  mayAsk.audio = true;
  refreshLists();
  notify();
}

/** Peak (0..1) of channel `c` of the input (the meter's two rows), or 0. */
export function inputLevel(c) {
  const cap = mic.capture;
  if (!cap || cap.closed) return 0;
  return cap.channelLevel([Math.min(c, cap.deviceChannels - 1)]);
}

export function inputState() {
  return {
    live: Boolean(mic.capture),
    pending: mic.pending,
    reason: mic.reason,
    mayAsk: mayAsk.audio,
    label: mic.label,
    noEngine: !audioEngine?.audioContext,
  };
}

// ── Want and have ──

/**
 * Open, switch or release to match what the view wants:
 * `camera`: the device id to show ("" the default) or null for none;
 * `audio`: the same for the input; `settings` sizes the camera.
 * While a studio take records, nothing is opened here ("recording").
 */
export function reconcile({ camera, audio, settings, audioChannelCount = 2 }) {
  const busy = isRecording();
  // Camera
  if (camera === null || camera === NONE) {
    if (cam.stream || cam.key) releaseCamera();
    if (cam.reason === "recording") cam.reason = null;
  } else if (busy) {
    if (cam.stream || cam.key) releaseCamera();
    cam.reason = "recording";
  } else {
    if (cam.reason === "recording") cam.reason = null;
    const key = `${camera}|${settings.resolution}|${settings.fps}`;
    if (cam.stream && cam.deviceId === camera && cam.key !== key) {
      // Same camera, another size or rate: ask the track, keep the stream.
      cam.key = key;
      const c = cameraConstraints(settings, "");
      const track = cam.stream.getVideoTracks()[0];
      track?.applyConstraints?.(c).then(() => { readCameraSettings(track); notify(); })
        .catch((err) => console.warn("[streamer] the camera kept its size:", err));
    } else if ((cam.stream && cam.deviceId !== camera) || (!cam.stream && cam.key && cam.key !== key)) {
      // Another camera, or another choice while one was opening.
      releaseCamera();
    }
    if (!cam.stream && !cam.key && !cam.reason && mayAsk.video && checkCaptureSupport().ok) openCamera(camera, settings, key);
    else if (!checkCaptureSupport().ok && !cam.reason) cam.reason = checkCaptureSupport().reason;
  }
  // Input
  if (audio === null || audio === NONE) {
    if (mic.capture || mic.key) releaseMic();
    if (mic.reason === "recording") mic.reason = null;
  } else if (busy) {
    if (mic.capture || mic.key) releaseMic();
    mic.reason = "recording";
  } else {
    if (mic.reason === "recording") mic.reason = null;
    const key = `input:${audio}`; // never empty: an empty key means nothing is opening
    if (mic.reason && mic.reasonEngine && mic.reasonEngine !== audioEngine) {
      mic.reason = null;
      mic.reasonEngine = null;
    }
    // The song reloaded (a new engine, a new AudioContext): the input opens
    // again on the new one, or the meter and a take would hear nothing.
    if (mic.capture && (mic.capture.closed || mic.capture.ctx !== audioEngine?.audioContext)) releaseMic();
    if ((mic.capture && mic.deviceId !== audio) || (!mic.capture && mic.key && mic.key !== key)) releaseMic();
    if (!mic.capture && !mic.key && !mic.reason && mayAsk.audio && checkCaptureSupport().ok) openMic(audio, key, audioChannelCount);
    else if (!checkCaptureSupport().ok && !mic.reason) mic.reason = checkCaptureSupport().reason;
  }
}

/**
 * Phase 4's seam: what Streamer has open now. `camera` and `audio` are
 * MediaStreams (or null); `capture` is the studio's capture graph on the
 * input (captureGraph.openCapture), which records takes.
 */
export function getStreamerMedia() {
  return {
    camera: cam.stream,
    audio: mic.capture?.stream || null,
    capture: mic.capture,
  };
}
