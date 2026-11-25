// How old the camera's picture is when Performance draws it into the take.
//
// Performance records a canvas, not the camera: each recorded frame carries
// the time it was drawn, while what it shows was captured earlier, by the
// camera frame's age when drawn (the browser's capture pipeline plus up to one
// camera frame of waiting for the next draw). The camera's microphone in the
// same file is stamped at capture, so the server's measured offset
// (app/pipeline/av_sync.py) places the drawn frames; the picture they show is
// that much earlier. The median age, sent as video.drawAgeMs, is taken off
// (app/core/takes.py).
//
// Read from requestVideoFrameCallback's captureTime on the camera's <video>
// (performance.now() clock). An engine without it, or a <video> whose frames
// are not presented, gives no samples, and nothing is taken off (as before).
// The camera's own sensor latency is before captureTime and stays invisible.

export const MAX_AGE_MS = 1000;
const MIN_SAMPLES = 5;
const MAX_SAMPLES = 2000;

/** Median of the finite ages in 0..MAX_AGE_MS, in ms to 0.1, or null with
 * fewer than MIN_SAMPLES of them. */
export function medianAgeMs(ages) {
  const ok = (Array.isArray(ages) ? ages : []).filter((a) => Number.isFinite(a) && a >= 0 && a <= MAX_AGE_MS);
  if (ok.length < MIN_SAMPLES) return null;
  ok.sort((a, b) => a - b);
  const mid = ok.length >> 1;
  const m = ok.length % 2 ? ok[mid] : (ok[mid - 1] + ok[mid]) / 2;
  return Math.round(m * 10) / 10;
}

/**
 * Watch `video` (the camera's <video>) for frame capture times. sample()
 * at each draw records the age of the frame on show; medianMs() is the
 * result; stop() ends the watch.
 */
export function watchFrameAge(video) {
  const ages = [];
  let latest = null;
  let handle = null;
  let stopped = false;
  const supported = Boolean(video) && typeof video.requestVideoFrameCallback === "function";
  const onFrame = (_now, meta) => {
    if (stopped) return;
    if (Number.isFinite(meta?.captureTime)) latest = meta.captureTime;
    handle = video.requestVideoFrameCallback(onFrame);
  };
  if (supported) handle = video.requestVideoFrameCallback(onFrame);
  return {
    ages,
    sample(atMs = performance.now()) {
      if (stopped || latest === null || ages.length >= MAX_SAMPLES) return;
      ages.push(atMs - latest);
    },
    medianMs() {
      return medianAgeMs(ages);
    },
    stop() {
      stopped = true;
      if (supported && handle != null) video.cancelVideoFrameCallback?.(handle);
    },
  };
}
