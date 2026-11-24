// Records the camera for one take and uploads it in parts while it records.
//
// Raw camera only: no compositing, no canvas. With the camera's microphone
// when the caller opened it (video/cameraAudio.js): the server measures the
// picture's place against the take from that sound. The browser's media thread does
// the encoding, so this does not depend on requestAnimationFrame and keeps
// running in a hidden window (not verified for a minimised WebView2).
//
// Parts go up as soon as 4 MiB is buffered (about one every 4 to 7 seconds at
// 5 to 8 Mbps), one at a time and in order, so seq is strictly increasing and
// a crash loses at most the last few seconds.

import {
  cutParts, extForMime, pickRecorderMime, buildVideoBlock, firstRecordedCapture, FRAME_RATE,
} from "./videoCodec.js";

const TIMESLICE_MS = 1000;

export function recorderSupported() {
  return typeof window.MediaRecorder === "function";
}

// Frames watched for capture times: enough to pass the first recorded frame
// and to measure the frame interval, then the watch stops.
const CAPTURE_WATCH_FRAMES = 8;

/**
 * Watch `video` (playing the camera stream) for frame capture times.
 * Returns { times, stop() }; `times` stays empty on an engine without
 * requestVideoFrameCallback or captureTime, or while the preview is not
 * painted (a hidden window), and the block then falls back to the
 * recorder's start event.
 */
function watchCaptureTimes(video) {
  const times = [];
  let handle = null;
  let done = false;
  const supported = Boolean(video) && typeof video.requestVideoFrameCallback === "function";
  const onFrame = (_now, meta) => {
    if (done) return;
    if (Number.isFinite(meta?.captureTime)) times.push(meta.captureTime);
    if (times.length >= CAPTURE_WATCH_FRAMES) {
      done = true;
      return;
    }
    handle = video.requestVideoFrameCallback(onFrame);
  };
  if (supported) handle = video.requestVideoFrameCallback(onFrame);
  return {
    times,
    stop() {
      done = true;
      if (supported && handle != null) video.cancelVideoFrameCallback?.(handle);
    },
  };
}

// The camera microphone's sound in the file: only compared on the server,
// never heard, so a speech-grade rate is plenty.
const AUDIO_BITS_PER_SECOND = 96_000;

/**
 * The MediaRecorder for the picture, with the camera's microphone when there
 * is one. Returns { recorder, chosen, withAudio }. A recorder that refuses
 * the sound (an engine or a type without that codec) records the picture
 * alone, as before: the sound only helps the sync, the picture is the take.
 */
function makeRecorder(stream, audioTrack, bitsPerSecond) {
  const isType = window.MediaRecorder?.isTypeSupported?.bind(window.MediaRecorder);
  const video = stream.getVideoTracks();
  const attempts = [];
  if (audioTrack && audioTrack.readyState !== "ended") {
    const chosen = pickRecorderMime(isType, true);
    const opts = { videoBitsPerSecond: bitsPerSecond, audioBitsPerSecond: AUDIO_BITS_PER_SECOND };
    if (chosen) opts.mimeType = chosen;
    attempts.push({ stream: new MediaStream([...video, audioTrack]), opts, chosen, withAudio: true });
  }
  const chosen = pickRecorderMime(isType);
  const opts = { videoBitsPerSecond: bitsPerSecond };
  if (chosen) opts.mimeType = chosen;
  attempts.push({ stream, opts, chosen, withAudio: false });
  for (const a of attempts) {
    try {
      return { recorder: new MediaRecorder(a.stream, a.opts), chosen: a.chosen, withAudio: a.withAudio };
    } catch (err) {
      console.warn("[video] MediaRecorder with", a.opts, a.withAudio ? "and the camera sound" : "", "failed:", err);
    }
  }
  console.warn("[video] retrying the picture's recorder with the engine's defaults");
  return { recorder: new MediaRecorder(stream), chosen: "", withAudio: false };
}

/**
 * start({ stream, jobId, takeId, perfMsAtFrame0, ctxTimeAtFrame0, bitsPerSecond,
 *         deviceLabel, mirrorPreview, upload, previewVideo, audioTrack, cameraAudio })
 * upload(seq, blob) -> Promise, resolves on the server ack.
 * previewVideo: a <video> playing `stream`, to read frame capture times.
 * audioTrack: the camera's microphone (video/cameraAudio.js), recorded in the
 * same file as the picture, or null. The caller owns it and stops it.
 * cameraAudio: { state, label } from openCameraAudio, for the video block.
 * Returns { stop() -> Promise<videoBlock>, abort(), takeId }.
 */
export function startCameraRecording(opts) {
  const {
    stream, takeId, perfMsAtFrame0, ctxTimeAtFrame0, bitsPerSecond, deviceLabel, mirrorPreview, upload,
    previewVideo, audioTrack = null, cameraAudio = null,
  } = opts;
  const made = makeRecorder(stream, audioTrack, bitsPerSecond);
  let { recorder, chosen } = made;
  // The block says what went into the file: a microphone the recorder
  // refused is "failed", not "recorded".
  let audioInfo = cameraAudio;
  if (audioInfo?.state === "recorded" && !made.withAudio) audioInfo = { ...audioInfo, state: "failed" };

  const track = stream.getVideoTracks()[0];
  const settings = track?.getSettings?.() || {};
  let buffered = new Blob([]);
  let seq = 0;
  let chain = Promise.resolve();
  let uploadError = null;
  let startedAtPerf = null;
  let firstChunkAtPerf = null;
  let aborted = false;

  const send = (part) => {
    const n = seq++;
    // After one part has failed for good the file has a hole and the take
    // is kept without its video, so the rest is not sent: each later part
    // would only be refused (a 409 and a resync each), and a long take
    // queued hundreds of them, holding Stop for minutes (measured).
    chain = chain.then(() => (aborted || uploadError ? null : upload(n, part))).catch((err) => {
      uploadError = uploadError || err;
      console.warn(`[video] camera part ${n} failed:`, err);
    });
  };

  const drain = (final) => {
    const { parts, rest } = cutParts(buffered, final);
    for (const p of parts) send(p);
    buffered = rest || new Blob([]);
  };

  const onData = (e) => {
    if (!e.data || e.data.size === 0) return;
    if (firstChunkAtPerf === null) {
      // The container's zero is the first frame the recorder accepted, which
      // the "start" event marks. The arrival time of this chunk says nothing
      // more precise (BlobEvent.timecode is relative to the recorder's own
      // first chunk), so the start time stands for it. What remains is the
      // camera's own latency, invisible to the browser: cameraOffsetMs.
      firstChunkAtPerf = startedAtPerf ?? performance.now();
    }
    buffered = new Blob([buffered, e.data], { type: e.data.type });
    drain(false);
  };
  // Whether the recorder now running has the camera's sound in it.
  let hasAudio = made.withAudio;
  const audioLost = (why, err) => {
    if (!hasAudio) return;
    hasAudio = false;
    if (audioInfo?.state === "recorded") audioInfo = { ...audioInfo, state: "failed" };
    console.warn(`[video] the camera sound ${why}; the picture is kept, the sync falls back:`, err || "");
  };
  // A recorder error (an encoder or a track failing mid-take) ends the
  // recording: what was recorded so far is kept and uploaded as the take's
  // picture (the render shows black after it), and with the camera's sound
  // in it the sync is marked as failed. "stop" may never follow an error, so
  // the error ends the wait too: Stop never hangs on a dead recorder.
  const wire = (r) => new Promise((resolve) => {
    r.addEventListener("start", () => { startedAtPerf = performance.now(); });
    r.addEventListener("dataavailable", onData);
    r.addEventListener("stop", resolve, { once: true });
    r.addEventListener("error", (e) => {
      console.warn("[video] the picture's recorder failed mid-take:", e?.error || e);
      audioLost("failed with the recorder", e?.error);
      resolve();
    }, { once: true });
  });
  // A microphone that goes away (unplugged) leaves the picture recording.
  audioTrack?.addEventListener?.("ended", () => audioLost("track ended"), { once: true });

  let stopped = wire(recorder);
  // The recorder's first frame is the first one captured after this call.
  const captures = watchCaptureTimes(previewVideo);
  const startCallMs = performance.now();
  try {
    recorder.start(TIMESLICE_MS);
  } catch (err) {
    // An engine that took the sound at construction and refuses it at start:
    // the picture records alone, as before.
    if (!made.withAudio) throw err;
    console.warn("[video] the recorder refused the camera sound at start, recording the picture alone:", err);
    ({ recorder, chosen } = makeRecorder(stream, null, bitsPerSecond));
    if (audioInfo) audioInfo = { ...audioInfo, state: "failed" };
    hasAudio = false;
    stopped = wire(recorder);
    recorder.start(TIMESLICE_MS);
  }
  if (startedAtPerf === null) startedAtPerf = performance.now();

  return {
    takeId,
    get mimeType() { return recorder.mimeType || chosen; },
    async stop() {
      captures.stop();
      if (recorder.state !== "inactive") recorder.stop();
      await stopped;
      drain(true);
      await chain;
      if (uploadError) throw uploadError;
      const mime = recorder.mimeType || chosen;
      return buildVideoBlock({
        mime,
        ext: extForMime(mime),
        width: settings.width,
        height: settings.height,
        fps: settings.frameRate || FRAME_RATE,
        deviceLabel,
        mirrorPreview,
        perfMsAtFrame0,
        ctxTimeAtFrame0,
        startedAtPerf,
        firstChunkAtPerf,
        firstCapturePerf: firstRecordedCapture(captures.times, startCallMs),
        parts: seq,
        cameraAudio: audioInfo,
      });
    },
    abort() {
      aborted = true;
      captures.stop();
      try {
        if (recorder.state !== "inactive") recorder.stop();
      } catch (err) {
        console.warn("[video] recorder stop on abort failed:", err);
      }
    },
  };
}
