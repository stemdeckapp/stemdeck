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

/**
 * An AudioContext at a sane rate for 44.1 kHz material.
 *
 * The device rate can only be read from a context, and a context's rate is
 * fixed once it exists, so a throwaway one is opened to ask and closed again.
 * If that fails, or the browser declines the rate we ask for, we fall back to
 * the plain constructor: playing at the device's rate is the current behaviour
 * and is never worse than not playing.
 */
export function createPlaybackContext(AudioCtx) {
  if (!AudioCtx) return null;
  let deviceRate = 0;
  try {
    const probe = new AudioCtx();
    deviceRate = probe.sampleRate;
    // Nothing is ever routed into it, and close() is a promise we do not need
    // to wait on. Safari rejects close() on a context that never started.
    Promise.resolve(probe.close()).catch(() => {});
  } catch {
    return new AudioCtx();
  }
  if (!(deviceRate > MAX_GRAPH_RATE)) return new AudioCtx();
  try {
    return new AudioCtx({ sampleRate: MAX_GRAPH_RATE });
  } catch {
    return new AudioCtx();
  }
}
