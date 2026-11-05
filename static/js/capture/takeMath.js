// Pure arithmetic for recording a take: no DOM, no Web Audio, so node tests
// can pin every number (tests/js/take-math.test.mjs).
//
// Latency sign convention, shared with the backend and the video recorder:
// positive means "arrives late", and it is subtracted to align.

/**
 * Song time of a captured input frame (05 section 5.4).
 *
 * The engine's heard playhead is
 *   song = (ctx - startCtxTime - pipelineLatencySeconds) * rate + startOffset
 * (audioEngine.now, chunkedAudioEngine._getCurrentTime). A frame captured at
 * context time `ctxTime` is what the player did in answer to audio they heard
 * one round trip earlier, so the formula is evaluated at ctxTime - roundTrip.
 * Unlike the engine's now(), this is not clamped: frames recorded during the
 * count-in get a negative song time, which is how pickup notes are kept.
 */
export function songTimeAtCtx(ctxTime, mapping, roundTripSec) {
  const heard = ctxTime - (roundTripSec || 0);
  return (heard - mapping.startCtxTime - (mapping.pipelineLatencySeconds || 0))
    * (mapping.rate || 1) + mapping.startOffset;
}

/** `timeline.first_sample_song_sec` for the sidecar: song time of raw frame 0. */
export function firstSampleSongSec(ctxTimeAtFrame0, mapping, roundTripSec) {
  return songTimeAtCtx(ctxTimeAtFrame0, mapping, roundTripSec);
}

/** Song time where the take's audio stops (the last captured frame). */
export function endSongSec(firstSongSec, frames, sampleRate, rate = 1) {
  return firstSongSec + (frames / sampleRate) * rate;
}

/**
 * Shortest take kept when it never read the song's clock. Below this, Stop
 * came before anything worth keeping was captured (a double click on Record).
 * Above it the take holds real audio, and real audio is never thrown away.
 */
export const KEEP_UNPLACED_MIN_SEC = 0.5;

/** True when a take with no clock mapping still holds audio worth keeping. */
export function keepUnplacedTake(frames, sampleRate) {
  return sampleRate > 0 && frames / sampleRate >= KEEP_UNPLACED_MIN_SEC;
}

/**
 * The mapping for a take that recorded but never read the engine's clock
 * (the engine it was bound to went away before playing). Frame 0 is put
 * where the playhead stood when Record was pressed: the transport starts a
 * moment after frame 0, so this is right to within that moment plus any
 * count-in, and the user is told to check it and nudge.
 */
export function estimatedMapping(ctxTimeAtFrame0, startSongSec, roundTripSec) {
  return {
    startCtxTime: ctxTimeAtFrame0 - (roundTripSec || 0),
    startOffset: Number.isFinite(startSongSec) ? Math.max(0, startSongSec) : 0,
    rate: 1,
    pipelineLatencySeconds: 0,
    epoch: null,
    estimated: true,
  };
}

/**
 * Where the backend puts raw frame 0 once a nudge is applied. The server is
 * the one place the nudge is folded in; this mirrors it for the UI readout.
 */
export function placementSec(firstSongSec, userNudgeMs) {
  return firstSongSec - (userNudgeMs || 0) / 1000;
}

/**
 * `performance.now()` time of a context time, from one getOutputTimestamp()
 * reading. Lets the camera recorder put its frames on the audio clock.
 */
export function perfMsAtCtx(ctxTime, outputTimestamp) {
  if (!outputTimestamp || !Number.isFinite(outputTimestamp.contextTime)
      || !Number.isFinite(outputTimestamp.performanceTime)) return null;
  return outputTimestamp.performanceTime + (ctxTime - outputTimestamp.contextTime) * 1000;
}

/**
 * Round trip to use before a calibration exists (05 section 4.3). Every input
 * is a browser report that is known to be optimistic, so the result is
 * labelled "estimated" and the nudge stays available.
 */
export function estimateRoundTripSec({ outputLatency, baseLatency, trackLatency } = {}) {
  const fin = (v, d) => (Number.isFinite(v) && v >= 0 ? v : d);
  return fin(outputLatency, 0) + fin(baseLatency, 0) + fin(trackLatency, 0.01);
}

export function median(values) {
  const v = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!v.length) return NaN;
  const mid = v.length >> 1;
  return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

// ─── Loopback calibration ───

export const CAL_CLICKS = 5;
export const CAL_SPACING_SEC = 0.6;
export const CAL_BURST_SEC = 0.002;
// Longest round trip searched for. Bluetooth can reach 300 ms; anything past
// this is not a loopback we can trust. It must stay under the click spacing so
// two clicks never share a search window.
export const CAL_MAX_RT_SEC = 0.5;
export const CAL_AGREE_SEC = 0.002;
export const CAL_MIN_AGREE = 4;

/** A 2 ms broadband burst, deterministic so tests and runs match. */
export function makeBurst(sampleRate, seconds = CAL_BURST_SEC) {
  const n = Math.max(8, Math.round(sampleRate * seconds));
  const out = new Float32Array(n);
  let s = 0x9e3779b9;
  for (let i = 0; i < n; i++) {
    // xorshift32: white noise without Math.random, so it is reproducible.
    s ^= s << 13; s >>>= 0;
    s ^= s >>> 17;
    s ^= s << 5; s >>>= 0;
    const w = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1)); // Hann
    out[i] = ((s / 0xffffffff) * 2 - 1) * w * 0.9;
  }
  return out;
}

/**
 * Find where `burst` arrives in `signal`, searching lags [from, to).
 * Returns { lag, score } where score is the correlation peak over the RMS
 * correlation in the window (a peak-to-noise ratio), or null.
 */
export function findBurst(signal, burst, from, to) {
  const n = burst.length;
  const start = Math.max(0, from | 0);
  const end = Math.min(signal.length - n, to | 0);
  if (end <= start) return null;
  let best = -1;
  let bestLag = -1;
  let sumSq = 0;
  let count = 0;
  for (let lag = start; lag < end; lag++) {
    let acc = 0;
    for (let i = 0; i < n; i++) acc += signal[lag + i] * burst[i];
    const a = Math.abs(acc);
    sumSq += acc * acc;
    count++;
    if (a > best) { best = a; bestLag = lag; }
  }
  if (bestLag < 0 || best <= 0) return null;
  const rms = Math.sqrt(sumSq / count);
  return { lag: bestLag, score: rms > 0 ? best / rms : Infinity };
}

// Below this peak-to-noise ratio a detection is a guess. A clean loopback
// scores in the tens; room noise with no click scores 3 to 5.
export const CAL_MIN_SCORE = 6;

/**
 * Measure each click's round trip from a capture.
 *
 * `signal` is mono, starting at context frame `signalStartFrame`;
 * `clickFrames` are the context frames each burst was scheduled at.
 * Returns one entry per click: seconds, or null when it was not found.
 */
export function measureRoundTrips(signal, sampleRate, signalStartFrame, clickFrames, burst) {
  const maxLag = Math.round(CAL_MAX_RT_SEC * sampleRate);
  return clickFrames.map((cf) => {
    const rel = cf - signalStartFrame;
    const hit = findBurst(signal, burst, rel, rel + maxLag);
    if (!hit || hit.score < CAL_MIN_SCORE) return null;
    return (hit.lag - rel) / sampleRate;
  });
}

/**
 * Decide a calibration from per-click round trips (05 section 5.5): accept
 * when at least four of five agree within 2 ms of their median, and report
 * the median of those. Drift is the slope of round trip over click time.
 */
export function summariseCalibration(roundTrips, spacingSec = CAL_SPACING_SEC) {
  const found = roundTrips
    .map((rt, i) => ({ rt, t: i * spacingSec }))
    .filter((p) => Number.isFinite(p.rt));
  if (found.length < CAL_MIN_AGREE) {
    return { ok: false, reason: found.length ? "inconsistent" : "too-quiet", found: found.length };
  }
  const m = median(found.map((p) => p.rt));
  const agree = found.filter((p) => Math.abs(p.rt - m) <= CAL_AGREE_SEC);
  if (agree.length < CAL_MIN_AGREE) {
    return { ok: false, reason: "inconsistent", found: found.length };
  }
  const roundTripSec = median(agree.map((p) => p.rt));
  return {
    ok: true,
    roundTripSec,
    roundTripMs: Math.round(roundTripSec * 1e4) / 10,
    driftPpm: driftPpm(agree),
    agreeing: agree.length,
  };
}

function driftPpm(points) {
  if (points.length < 2) return 0;
  const n = points.length;
  const mt = points.reduce((s, p) => s + p.t, 0) / n;
  const mr = points.reduce((s, p) => s + p.rt, 0) / n;
  let num = 0;
  let den = 0;
  for (const p of points) {
    num += (p.t - mt) * (p.rt - mr);
    den += (p.t - mt) ** 2;
  }
  return den > 0 ? Math.round((num / den) * 1e6) : 0;
}

// ─── PCM packing ───

/** Interleave per-channel blocks into one float32 run (the part body format). */
export function interleave(channels, frames) {
  const ch = channels.length;
  if (ch === 1) return channels[0].slice(0, frames);
  const out = new Float32Array(frames * ch);
  for (let i = 0; i < frames; i++) {
    for (let c = 0; c < ch; c++) out[i * ch + c] = channels[c][i];
  }
  return out;
}

// Parts are capped at 4 MB by the server. A whole number of frames per part
// keeps every part independently meaningful.
export const PART_MAX_BYTES = 4 * 1024 * 1024;

export function maxFramesPerPart(channels) {
  return Math.floor(PART_MAX_BYTES / (4 * channels));
}

/**
 * Why Record cannot start, or null. The MVP records at tempo 1.0 only, and
 * only on the Web Audio engine, whose clock the take is placed against.
 */
export function recordBlockReason({ hasEngine, rate }) {
  if (!hasEngine) return "no-engine";
  if (Math.abs((rate ?? 1) - 1) > 1e-6) return "tempo";
  return null;
}
