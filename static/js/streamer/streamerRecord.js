// Streamer Mode's recorder: one take of the camera and the input.
//
// Reuse, not a second recording path. A Streamer take is a studio take
// (capture/takeRecorder.js) on a track of its own ("Streamer", a video
// track): the microphone through the same capture graph and recorder
// worklet, the take clock, the upload in parts and /finish. The picture is
// that take's camera part, recorded by the studio's camera recorder
// (video/cameraRecorder.js) from an offscreen canvas at the output size:
// the camera covering it, drawn each frame by the one stage renderer
// (stageRenderer.js) without the live window's guides, and captured with
// canvas.captureStream(fps). The canvas fixes the output's shape and size
// (the aspect and resolution chosen), whatever the camera delivers. So the
// picture and the sound share the sync block the studio's camera takes use,
// and the server renders the MP4 exactly as it renders a camera take
// (app/api/takes.py, fit "fill").
//
// The take is announced as not primary, so the studio's own camera
// (video/videoTake.js) never films it.
//
// The view (streamer.js) asks: start(), stop(), state, and calls draw()
// every frame while a take is open.

import { startTake, onTakeEvent, registerTakeParticipant } from "../capture/takeRecorder.js";
import { createTrack, uploadPart } from "../capture/takeUpload.js";
import { openSilentCapture } from "../capture/captureGraph.js";
import { recordBlockReason } from "../capture/takeMath.js";
import { requestTakesReload } from "../capture/takeLanes.js";
import { startCameraRecording, recorderSupported } from "../video/cameraRecorder.js";
import { audioEngine, playbackSpeed } from "../state.js";
import { transport, togglePlayPause, setPlayheadTime } from "../transport.js";
import { nextRecordState, isTakeOpen, startPoint, outputPixels, videoBitrate } from "./recordModel.js";
import { resolutionOf } from "./outputSettings.js";

const TRACK_KEY = (jobId) => `stemdeck:streamer:track:${jobId}`;
const LAST_TAKE_KEY = (jobId) => `stemdeck:streamer:take:${jobId}`;
// What a take was recorded at (aspect, resolution, fps): its export uses it.
const TAKE_INFO_KEY = (takeId) => `stemdeck:streamer:takeinfo:${takeId}`;

const rec = {
  state: "idle",
  reason: null, // why the last start or take failed
  jobId: null,
  trackId: null,
  takeId: null,
  session: null,
  camera: null, // the camera recorder on the canvas stream
  canvas: null,
  ctx: null,
  stream: null,
  size: null,
  fps: 30,
  silent: null, // a capture with no input, when there is no microphone
  recorder: null, // the take's recorder on the capture
  startedAtMs: 0,
  hiddenWhileRecording: false,
  aborted: false, // closed while "starting": the take must not begin
  recordedAt: null, // { aspect, resolution, fps } of the open take
};
const listeners = new Set();

// The few keys this module keeps in localStorage, read once each: the view
// asks for the last take and the Streamer track every frame.
const kept = new Map();
function readKept(key) {
  if (kept.has(key)) return kept.get(key);
  let v = null;
  try { v = localStorage.getItem(key); } catch { v = null; }
  kept.set(key, v);
  return v;
}
function writeKept(key, value) {
  kept.set(key, value ?? null);
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch { /* kept for this visit */ }
}

function set(event, extra = {}) {
  rec.state = nextRecordState(rec.state, event);
  Object.assign(rec, extra);
  for (const cb of listeners) {
    try { cb(rec.state); } catch (err) { console.warn("[streamer] recorder listener failed:", err); }
  }
}

export function onRecordChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function recordState() {
  return {
    state: rec.state,
    reason: rec.reason,
    jobId: rec.jobId,
    takeId: rec.takeId,
    elapsedSec: rec.state === "recording" ? (performance.now() - rec.startedAtMs) / 1000 : 0,
    hidden: rec.hiddenWhileRecording,
    stopReason: rec.stopReason,
  };
}

export function takeOpen() {
  return isTakeOpen(rec.state);
}

/** The canvas the take films, at the output size, while a take is open. */
export function outputCanvas() {
  return isTakeOpen(rec.state) ? rec.canvas : null;
}

/** The last take recorded for `jobId` (a reload keeps it), or null. */
export function lastTake(jobId) {
  if (rec.jobId === jobId && rec.takeId && (rec.state === "saved" || rec.state.startsWith("export"))) return rec.takeId;
  return readKept(LAST_TAKE_KEY(jobId)) || null;
}

export function streamerTrackId(jobId) {
  return readKept(TRACK_KEY(jobId)) || null;
}

/** Why recording cannot start now, or null: the studio's own rules
 * (takeMath.recordBlockReason: an engine, tempo 1) and the browser's. */
export function recordBlock() {
  const block = recordBlockReason({ hasEngine: Boolean(audioEngine), rate: playbackSpeed });
  if (block) return block;
  if (!recorderSupported() || typeof HTMLCanvasElement.prototype.captureStream !== "function") return "unsupported";
  return null;
}

/** The Streamer track of this song: the one this browser kept, else a
 * video track of that name (another browser's), else a new one. */
async function ensureTrack(jobId, name) {
  const kept = streamerTrackId(jobId);
  let tracks = [];
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
    tracks = (res.ok ? (await res.json())?.tracks : null) || [];
  } catch (err) {
    console.warn("[streamer] reading the song's tracks failed:", err);
  }
  const found = (kept && tracks.find((tr) => tr.id === kept))
    || tracks.filter((tr) => tr.kind === "video" && tr.name === name).pop();
  if (found) {
    writeKept(TRACK_KEY(jobId), found.id);
    return found.id;
  }
  const track = await createTrack(jobId, { name, kind: "video" });
  writeKept(TRACK_KEY(jobId), track.id);
  return track.id;
}

/**
 * Start a take. `capture` is Streamer's open input (streamerDevices), or
 * null for a take without a microphone (the camera and the backing only).
 * `draw(ctx, width, height)` paints one output frame. Resolves
 * { ok: true } or { ok: false, reason }.
 */
export async function startRecording({ jobId, capture, settings, aspect, range, trackName, draw, remove = "", channels = null, camera = null }) {
  if (isTakeOpen(rec.state) || rec.state === "exporting") return { ok: false, reason: "busy" };
  const block = recordBlock();
  if (block) return { ok: false, reason: block };
  const engine = audioEngine;
  set("start", { reason: null, jobId, takeId: null, session: null, hiddenWhileRecording: false, aborted: false });
  // What the take is recorded at, and the part left out of what it hears.
  rec.recordedAt = { aspect, resolution: String(settings.resolution), fps: Number(settings.fps) || 30, remove: remove || "" };
  // How the take was filmed: "on" or "off" (Video Source: No camera, or no
  // camera open). A take of an older build may say "hidden" (its Camera
  // layer was hidden). The Video lane says so.
  if (["on", "off", "hidden"].includes(camera)) rec.recordedAt.camera = camera;
  // Closed while starting (stopRecording before a session exists): stop here.
  const goOn = () => {
    if (rec.aborted) throw Object.assign(new Error("aborted"), { code: "aborted" });
  };
  try {
    const trackId = await ensureTrack(jobId, trackName);
    goOn();
    if (audioEngine !== engine) throw Object.assign(new Error("the song changed"), { code: "engine" });
    let cap = capture && capture.ctx === engine.audioContext && !capture.closed ? capture : null;
    if (!cap) {
      const res = await openSilentCapture(engine);
      if (res.capture) rec.silent = res.capture;
      goOn();
      if (!res.capture) throw Object.assign(new Error("no capture"), { code: res.reason || "unknown" });
      cap = res.capture;
    }
    // The channels the musician chose (Input 1, Input 2 or both), as many as
    // the device delivered; the silent capture has one.
    const max = Math.max(1, cap.deviceChannels || cap.channels || 1);
    const want = cap.silent ? [0] : (channels || [0]).filter((c) => c < max);
    rec.recorder = cap.createRecorder(want.length ? want : [0]);

    // The picture: the camera at the output size, drawn before the first frame
    // is captured.
    const size = outputPixels(aspect, settings.resolution);
    const canvas = document.createElement("canvas");
    canvas.width = size.width;
    canvas.height = size.height;
    rec.canvas = canvas;
    rec.ctx = canvas.getContext("2d");
    rec.size = size;
    rec.fps = Number(settings.fps) || 30;
    draw(rec.ctx, size.width, size.height);
    rec.stream = canvas.captureStream(rec.fps);

    // From the start of the range, or where the playhead stands inside it.
    if (engine.isPlaying()) togglePlayPause();
    const from = startPoint(transport()?.getCurrentTime?.() ?? 0, range);
    setPlayheadTime(from);
    // Where the take sits in the song (the take lane and its video block).
    rec.recordedAt.from = from;
    rec.recordedAt.to = from;
    rec.trackId = trackId;
    rec.session = startTake({
      engine,
      capture: cap,
      jobId,
      label: trackName,
      trackId,
      recorder: rec.recorder,
      // Not primary: the studio's camera (videoTake.js) does not film it.
      primary: false,
      startSongSec: from,
      startPlayback: () => { if (!engine.isPlaying()) togglePlayPause(); },
      stopPlayback: () => { if (audioEngine === engine && engine.isPlaying()) togglePlayPause(); },
      onAutoStop: (why) => {
        // The transport ended the take: a seek, a speed change, a pause, or
        // the song's end (which is expected, not worth a word).
        const at = transport()?.getCurrentTime?.() ?? 0;
        const dur = transport()?.getDuration?.() ?? 0;
        if (rec.recordedAt) rec.recordedAt.to = at;
        rec.stopReason = why === "transport" && dur && at >= dur - 1 ? "end" : why;
      },
    });
    rec.startedAtMs = performance.now();
    rec.stopReason = null;
    watch(rec.session);
    return { ok: true };
  } catch (err) {
    cleanup();
    if (err?.code === "aborted") {
      rec.aborted = false;
      set("abort", { reason: null });
      return { ok: false, reason: "aborted" };
    }
    console.warn("[streamer] recording did not start:", err);
    set("fail", { reason: err?.code || "failed" });
    return { ok: false, reason: rec.reason };
  }
}

/** Stop the take (it is kept: a Streamer take is exported, not reviewed). */
export async function stopRecording() {
  // Still starting (no session yet): the start stops at its next step.
  if (rec.state === "starting" && !rec.session) {
    rec.aborted = true;
    return;
  }
  if (!rec.session || !isTakeOpen(rec.state)) return;
  rec.stopReason = rec.stopReason || "user";
  if (rec.recordedAt && rec.state === "recording") rec.recordedAt.to = transport()?.getCurrentTime?.() ?? rec.recordedAt.to;
  set("stop");
  await rec.session.stop();
}

/** Draw one output frame (the view calls this every frame while recording). */
export function drawOutput(draw) {
  if (!rec.ctx || !isTakeOpen(rec.state)) return;
  draw(rec.ctx, rec.size.width, rec.size.height);
}

// A hidden window draws no frames (requestAnimationFrame stops), so the
// picture freezes there while the sound goes on: the view warns.
document.addEventListener("visibilitychange", () => {
  if (document.hidden && isTakeOpen(rec.state)) rec.hiddenWhileRecording = true;
});

/** The take ended (kept, failed or discarded): the recorder lets go of the
 * canvas, the capture it opened and the recorder worklet. */
function cleanup() {
  try { rec.camera?.abort(); } catch (err) { console.warn(err); }
  rec.camera = null;
  for (const tr of rec.stream?.getTracks() || []) tr.stop();
  rec.stream = null;
  rec.canvas = null;
  rec.ctx = null;
  try { rec.recorder?.dispose(); } catch (err) { console.warn(err); }
  rec.recorder = null;
  try { rec.silent?.close(); } catch (err) { console.warn(err); }
  rec.silent = null;
}

async function watch(session) {
  try {
    await session.ready;
  } catch { /* fail() below reports it */ }
  if (rec.session === session && session.state === "recording") set("started");
  while (rec.session === session && ["starting", "recording", "stopping"].includes(session.state)) {
    await new Promise((r) => setTimeout(r, 120));
    if (session.state === "stopping" && rec.state === "recording") set("stop");
  }
  if (rec.session !== session) return;
  if (session.state === "review") {
    await session.keep();
    const takeId = session.takeId;
    writeKept(LAST_TAKE_KEY(rec.jobId), takeId);
    writeKept(TAKE_INFO_KEY(takeId), JSON.stringify(rec.recordedAt));
    takeInfos.set(takeId, rec.recordedAt);
    cleanup();
    set("saved", { takeId });
    // The studio shows the new take on its track (as after Record).
    requestTakesReload(rec.jobId);
  } else {
    const reason = session.error?.status === 409 ? "too-many" : "failed";
    cleanup();
    set("fail", { reason });
  }
}

const takeInfos = new Map();

/**
 * What take `takeId` was recorded at: { aspect, resolution, fps }, or null
 * when this browser does not know (a take recorded elsewhere).
 */
export function takeInfo(takeId) {
  if (!takeId) return null;
  if (takeInfos.has(takeId)) return takeInfos.get(takeId);
  let v = null;
  try { v = JSON.parse(readKept(TAKE_INFO_KEY(takeId)) || "null"); } catch { v = null; }
  const ok = v && typeof v === "object" && ["16:9", "9:16", "1:1"].includes(v.aspect)
    && ["720", "1080", "2160"].includes(String(v.resolution)) && [24, 30, 60].includes(Number(v.fps));
  // A take recorded at 4K (offered before 0.21.0 dropped it) exports at
  // 1080p, the same aspect: the server renders 720 and 1080 only.
  // `remove`: the part left out ("" the whole song; null for a take recorded
  // before Streamer had the choice).
  const removed = typeof v?.remove === "string" && ["", "drums", "bass", "guitar", "piano", "vocals"].includes(v.remove) ? v.remove : null;
  const span = (k) => (Number.isFinite(v?.[k]) && v[k] >= 0 ? v[k] : null);
  const camera = ["on", "off", "hidden"].includes(v?.camera) ? v.camera : null;
  const info = ok ? { aspect: v.aspect, resolution: resolutionOf(v.resolution), fps: Number(v.fps), remove: removed, from: span("from"), to: span("to"), camera } : null;
  takeInfos.set(takeId, info);
  return info;
}

/** Export started, finished or failed (streamerExport.js drives the render). */
export function markExport(event, extra = {}) {
  if (event === "export" && extra.takeId && !isTakeOpen(rec.state)) {
    // The take being exported is this song's (`jobId`), never the one
    // recorded last on another song: from "saved" (an earlier visit's take
    // is exported the same way).
    if (rec.jobId !== extra.jobId || rec.takeId !== extra.takeId || rec.state === "idle" || rec.state === "failed") {
      rec.state = "saved";
    }
    rec.jobId = extra.jobId ?? rec.jobId;
  }
  set(event, extra);
}

/** The take `takeId` of `jobId` is gone from the server (deleted in
 * Advanced): it is not offered for export any more. */
export function forgetTake(jobId, takeId) {
  if (readKept(LAST_TAKE_KEY(jobId)) === takeId) writeKept(LAST_TAKE_KEY(jobId), null);
  writeKept(TAKE_INFO_KEY(takeId), null);
  takeInfos.delete(takeId);
  if (rec.jobId === jobId && rec.takeId === takeId && !isTakeOpen(rec.state)) set("reset", { takeId: null });
}

// The camera part: started when the take's frame 0 is stamped ("armed"),
// stopped before /finish (the participant), as videoTake.js does.
onTakeEvent((ev) => {
  if (!rec.session || ev.takeId == null) return;
  if (ev.type === "armed" && ev.jobId === rec.jobId && ev.trackId === rec.trackId && !rec.camera && rec.stream) {
    rec.takeId = ev.takeId;
    const jobId = ev.jobId;
    const takeId = ev.takeId;
    try {
      rec.camera = startCameraRecording({
        stream: rec.stream,
        jobId,
        takeId,
        perfMsAtFrame0: ev.perfMsAtFrame0,
        ctxTimeAtFrame0: ev.ctxTimeAtFrame0,
        bitsPerSecond: videoBitrate(rec.size, rec.fps),
        deviceLabel: "Performance scene",
        mirrorPreview: false,
        previewVideo: null,
        upload: (seq, blob) => uploadPart(jobId, takeId, "camera", seq, blob),
      });
    } catch (err) {
      console.warn("[streamer] the scene recorder did not start:", err);
    }
  } else if ((ev.type === "failed" || ev.type === "discarded") && ev.takeId === rec.takeId) {
    try { rec.camera?.abort(); } catch (err) { console.warn(err); }
    rec.camera = null;
  }
});

registerTakeParticipant(async (takeId) => {
  if (!rec.camera || rec.camera.takeId !== takeId) return null;
  const camera = rec.camera;
  rec.camera = null;
  try {
    return await camera.stop();
  } catch (err) {
    console.warn("[streamer] the scene upload failed:", err);
    return null;
  }
});
