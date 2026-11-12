// Records the camera for one take and uploads it in parts while it records.
//
// Raw camera only: no compositing, no canvas. The browser's media thread does
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

/**
 * start({ stream, jobId, takeId, perfMsAtFrame0, ctxTimeAtFrame0, bitsPerSecond,
 *         deviceLabel, mirrorPreview, upload, previewVideo })
 * upload(seq, blob) -> Promise, resolves on the server ack.
 * previewVideo: a <video> playing `stream`, to read frame capture times.
 * Returns { stop() -> Promise<videoBlock>, abort(), takeId }.
 */
export function startCameraRecording(opts) {
  const {
    stream, takeId, perfMsAtFrame0, ctxTimeAtFrame0, bitsPerSecond, deviceLabel, mirrorPreview, upload,
    previewVideo,
  } = opts;
  const chosen = pickRecorderMime(window.MediaRecorder?.isTypeSupported?.bind(window.MediaRecorder));
  const recOpts = { videoBitsPerSecond: bitsPerSecond };
  if (chosen) recOpts.mimeType = chosen;
  let recorder;
  try {
    recorder = new MediaRecorder(stream, recOpts);
  } catch (err) {
    console.warn("[video] MediaRecorder with", recOpts, "failed, retrying with defaults:", err);
    recorder = new MediaRecorder(stream);
  }

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

  recorder.addEventListener("start", () => { startedAtPerf = performance.now(); });
  recorder.addEventListener("dataavailable", (e) => {
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
  });

  const stopped = new Promise((resolve) => recorder.addEventListener("stop", resolve, { once: true }));
  // The recorder's first frame is the first one captured after this call.
  const captures = watchCaptureTimes(previewVideo);
  const startCallMs = performance.now();
  recorder.start(TIMESLICE_MS);
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
