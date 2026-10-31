// Creating the AudioContext playback runs in.
//
// An AudioContext built with no options runs at whatever rate the operating
// system's output device is set to, and every node downstream inherits it. That
// is the right default up to a point, and past that point it is pure waste:
// every stem StemDeck produces is 44.1 kHz (`-ar 44100` in the ffmpeg call in
// app/pipeline/runner.py), so a 192 kHz context upsamples on decode and then
// does several times the work on interpolated samples carrying nothing the
// originals did not (#578).
//
// The pitch chain's WSOLA search is O(overlap * seek) per sequence and both
// windows are set in milliseconds, so its cost per second of audio is quadratic
// in the context rate. One active chain, measured offline against the real
// processor: 2.1% of real time at 44.1 kHz, 2.4% at 48, 7.3% at 96 and 28.2% at
// 192. Decoded buffers scale linearly over the same range, from 606 MB to
// 2.6 GB for a five minute six stem track.
//
// So: a cap, not a pin. At 44.1 and 48 kHz the graph is left exactly as it is
// today, because there is nothing to gain and a needless resample to lose. Only
// a device asking for more resolution than the material has gets capped.
const MAX_GRAPH_RATE = 48000;

// The output device's rate, once a context has told us. Later contexts go
// straight to the right constructor, so none is opened only to be closed.
let _deviceRate = 0;

/**
 * An AudioContext at a sane rate for 44.1 kHz material.
 *
 * The device rate can only be read from a context, and a context's rate is
 * fixed once it exists. So the first call opens a plain one, which is the
 * one returned unless the device runs above the cap; only then is it closed
 * and a capped one opened. A probe context opened and closed on every call
 * is what this used to do, and Chromium never frees a context that was
 * closed before it started: one was kept per song load, for good.
 *
 * If the capped constructor fails, or the browser declines the rate we ask
 * for, the plain constructor is used: playing at the device's rate is the
 * old behaviour and is never worse than not playing.
 */
export function createPlaybackContext(AudioCtx) {
  if (!AudioCtx) return null;
  if (!(_deviceRate > MAX_GRAPH_RATE)) {
    let ctx;
    try {
      ctx = new AudioCtx();
    } catch {
      // A browser that will not build one now may on a second try; the
      // device's own rate is what we would have used anyway.
      return new AudioCtx();
    }
    _deviceRate = ctx.sampleRate;
    if (!(_deviceRate > MAX_GRAPH_RATE)) return ctx;
    // Nothing is ever routed into it, and close() is a promise we do not need
    // to wait on. Safari rejects close() on a context that never started.
    try { Promise.resolve(ctx.close()).catch(() => {}); } catch { /* never started */ }
  }
  try {
    return new AudioCtx({ sampleRate: MAX_GRAPH_RATE });
  } catch {
    return new AudioCtx();
  }
}

/** Forget the device rate (tests: each case is a different device). */
export function resetDeviceRate() {
  _deviceRate = 0;
}
