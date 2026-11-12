// Pure helpers for the camera take: no DOM, no network, so node tests cover
// them directly (tests/js/video-codec.test.mjs).
//
// The server re-encodes whatever arrives at render time, so the recorder never
// depends on one codec. The ladder only decides what is cheapest to record on
// this engine: MP4 H.264 where the engine has it (Safari always, recent
// Chromium), else WebM VP9, else VP8, else whatever the engine picks itself.

export const MIME_LADDER = [
  "video/mp4;codecs=avc1.42E01E",
  "video/mp4",
  "video/webm;codecs=vp9",
  "video/webm;codecs=vp8",
  "video/webm",
];

/**
 * First supported type in the ladder, or "" when none is (the recorder is
 * then built with no mimeType and reports its own choice in recorder.mimeType).
 * `isTypeSupported` is injected so tests do not need a browser.
 */
export function pickRecorderMime(isTypeSupported) {
  if (typeof isTypeSupported !== "function") return "";
  for (const type of MIME_LADDER) {
    try {
      if (isTypeSupported(type)) return type;
    } catch (err) {
      console.warn("[video] isTypeSupported threw for", type, err);
    }
  }
  return "";
}

/** File extension for a recorder MIME type; the server keys camera.<ext> on it. */
export function extForMime(mime) {
  const m = String(mime || "").toLowerCase();
  if (m.startsWith("video/mp4")) return "mp4";
  if (m.startsWith("video/x-matroska")) return "mkv";
  return "webm";
}

// The upload cap for one part: the non-multipart body limit in app/main.py
// (_JSON_BODY_LIMIT, 4 MiB). A part of exactly the cap is accepted.
export const PART_BYTES = 4 * 1024 * 1024;

// Capture sizes. 720p is the default because the camera encoder shares the
// CPU with the audio engine during the take; 1080p is the ceiling for 0.21.0.
export const QUALITY = {
  "720": { width: 1280, height: 720, bitsPerSecond: 5_000_000 },
  "1080": { width: 1920, height: 1080, bitsPerSecond: 8_000_000 },
};
export const DEFAULT_QUALITY = "720";
export const FRAME_RATE = 30;

/** getUserMedia video constraints for a quality id and an optional device. */
export function cameraConstraints(quality, deviceId) {
  const q = QUALITY[quality] || QUALITY[DEFAULT_QUALITY];
  return {
    deviceId: deviceId ? { exact: deviceId } : undefined,
    width: { ideal: q.width, max: 1920 },
    height: { ideal: q.height, max: 1080 },
    frameRate: { ideal: FRAME_RATE, max: FRAME_RATE },
  };
}

/**
 * Cut whatever is buffered into parts the server accepts.
 *
 * Returns { parts, rest }: every full PART_BYTES slice, plus the remainder.
 * With `final` true the remainder is emitted as a last part too (if non-empty),
 * so Stop flushes everything. Works on anything with size and slice(), which
 * is Blob in the browser and a tiny fake in tests.
 */
export function cutParts(blob, final = false, partBytes = PART_BYTES) {
  const parts = [];
  let offset = 0;
  while (blob.size - offset >= partBytes) {
    parts.push(blob.slice(offset, offset + partBytes));
    offset += partBytes;
  }
  let rest = offset ? blob.slice(offset) : blob;
  if (final && rest.size > 0) {
    parts.push(rest);
    rest = null;
  }
  return { parts, rest };
}

// Sync nudge range. Webcam latency is tens to a few hundred ms; a full second
// either way covers every camera seen so far with room to spare.
export const OFFSET_MIN_MS = -1000;
export const OFFSET_MAX_MS = 1000;

/** Integer milliseconds within the nudge range; anything not finite is 0. */
export function clampOffsetMs(value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return 0;
  return Math.max(OFFSET_MIN_MS, Math.min(OFFSET_MAX_MS, n));
}

function _round(n, digits = 3) {
  const f = 10 ** digits;
  return Math.round(n * f) / f;
}

// How much later than its capture a microphone sample's stamp is. The take
// recorder stamps audio frame 0 with the time the AudioContext plays that
// context time (getOutputTimestamp, perfMsAtFrame0), and a captured sample
// reaches the worklet through the WebAudio input buffer first. Measured with
// Chromium's file-based fake devices against Chromium's own A/V MediaRecorder
// (0.21.0 overnight log, task C): 25.8 to 35.0 ms over 17 takes, median
// 30.5, the same for every camera codec. A browser pipeline delay, not a
// camera or microphone property: the camera frame's capture time is moved
// onto the same stamped clock, so picture and sound captured together land
// together.
export const AUDIO_STAMP_LAG_MS = 30;

/**
 * Capture time (performance.now() clock) of the first frame a MediaRecorder
 * records, from the capture times of the frames the preview showed.
 *
 * The recorder's file starts with the first frame captured after start() was
 * called (verified frame by frame with an indexed test video, 18 of 18, MP4,
 * VP9 and VP8). The preview can skip a frame under load: when the gap before
 * the first frame it showed after the call is more than one and a half
 * frames, the skipped frame's time is used. Returns null when nothing usable was
 * seen (no captureTime on this engine, or the preview never painted).
 *
 * `captures` is ascending captureTime values; `startCallMs` the time just
 * before recorder.start().
 */
export function firstRecordedCapture(captures, startCallMs) {
  if (!Array.isArray(captures) || !Number.isFinite(startCallMs)) return null;
  const times = captures.filter(Number.isFinite);
  const idx = times.findIndex((c) => c > startCallMs);
  if (idx < 0) return null;
  const first = times[idx];
  if (idx === 0) return first;
  const deltas = [];
  for (let i = 1; i < times.length; i++) deltas.push(times[i] - times[i - 1]);
  deltas.sort((a, b) => a - b);
  const interval = deltas[deltas.length >> 1];
  // A gap of more than one and a half frames before it: the preview skipped
  // the recorder's first frame, which came one interval after the last frame
  // before the call.
  const before = times[idx - 1];
  if (first - before > 1.5 * interval && before + interval > startCallMs) return before + interval;
  return first;
}

/**
 * The "video" block of take.json (validated by app/api on finish).
 *
 * Clock: everything is on performance.now(). The take recorder stamps
 * perfMsAtFrame0 for raw audio frame 0; firstFrameAtMs is where the camera
 * file's first frame sits on that same stamped clock, so positive means video
 * starts later. With `firstCapturePerf` (the first recorded frame's capture
 * time) it is that time plus AUDIO_STAMP_LAG_MS (firstFrameClock "capture");
 * without it the recorder's "start" event stands in ("recorder-start"),
 * which fires 11 to 61 ms after the first frame depending on the codec and
 * the main thread's load (measured). Camera latency is invisible to the
 * browser and is what cameraOffsetMs (positive = video late, subtract)
 * corrects.
 */
export function buildVideoBlock({
  mime, ext, width, height, fps, deviceLabel, mirrorPreview,
  perfMsAtFrame0, ctxTimeAtFrame0, startedAtPerf, firstChunkAtPerf, firstCapturePerf,
  parts, cameraOffsetMs = 0,
}) {
  // The server validates every number as finite and bounded, so a value the browser
  // did not report is left out rather than sent as null.
  const block = {
    file: `camera.${ext || extForMime(mime)}`,
    mime: String(mime || "").slice(0, 100),
    fps_nominal: Number.isFinite(fps) && fps >= 1 && fps <= 240 ? _round(fps, 2) : FRAME_RATE,
    deviceLabel: String(deviceLabel || "").slice(0, 200),
    cameraOffsetMs: clampOffsetMs(cameraOffsetMs),
    mirrorPreview: Boolean(mirrorPreview),
    parts: Number.isInteger(parts) && parts >= 0 ? parts : 0,
  };
  if (Number.isFinite(width) && width >= 1) block.w = Math.min(4096, Math.round(width));
  if (Number.isFinite(height) && height >= 1) block.h = Math.min(4096, Math.round(height));
  if (Number.isFinite(ctxTimeAtFrame0)) block.startedAtAudioCtx = _round(ctxTimeAtFrame0, 6);
  if (Number.isFinite(startedAtPerf)) block.startedAtPerf = _round(startedAtPerf);
  const fromCapture = Number.isFinite(firstCapturePerf);
  const firstAt = fromCapture
    ? firstCapturePerf + AUDIO_STAMP_LAG_MS
    : (Number.isFinite(firstChunkAtPerf) ? firstChunkAtPerf : startedAtPerf);
  if (Number.isFinite(perfMsAtFrame0) && Number.isFinite(firstAt)) {
    block.firstFrameAtMs = Math.max(-600000, Math.min(600000, _round(firstAt - perfMsAtFrame0)));
    block.firstFrameClock = fromCapture ? "capture" : "recorder-start";
  }
  return block;
}

/** Does a registry take entry carry a camera recording? */
export function takeHasVideo(take) {
  if (!take || typeof take !== "object") return false;
  return take.has_camera === true;
}
