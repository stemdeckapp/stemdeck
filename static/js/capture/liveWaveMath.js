// Pure arithmetic for the live waveform of a take being recorded: no DOM, no
// Web Audio, so node tests can pin every number
// (tests/js/live-wave-math.test.mjs).
//
// The live waveform has to look like the waveform the server draws once the
// take is finished, so the switch from one to the other shows no jump. So it
// copies the server's shape on purpose:
//   - peaks.json holds PEAK_POINTS [min, max] points across the whole song,
//     channel 0 only (app/pipeline/audio_stats.py scan_stem, _PEAK_POINTS in
//     app/pipeline/collect.py). Here the points are on the same grid, filled
//     as audio arrives, kept for every channel; drawing uses channel 0.
//   - the lane draws bars exactly as player.js barsWaveformSvg does: a slot of
//     BAR_SLOT_PX, a bar of BAR_FRAC of it, height max(0.7, amp * 44) of a 48
//     unit box, one shared normalisation across every lane.

export const PEAK_POINTS = 3000;
export const BAR_SLOT_PX = 5;
export const BAR_FRAC = 0.6;
export const MIN_BARS = 40;
const BOX = 48;
const MID = 24;
const AMP_UNITS = 44;
const MIN_UNITS = 0.7;

/**
 * An empty set of peak points for one track: `points` slots across
 * `durationSec` of song, per channel. `pointSec` is the length of one point;
 * by default duration / points, but the server's grid (serverPointSec) is
 * what the finished waveform uses, so the caller passes that when known.
 */
export function createPeaks(durationSec, channels = 1, points = PEAK_POINTS, pointSec = 0) {
  const n = Math.max(1, points | 0);
  const ch = Math.max(1, channels | 0);
  const peaks = {
    durationSec: Math.max(0, Number(durationSec) || 0),
    points: n,
    pointSec: 0,
    channels: ch,
    min: [],
    max: [],
    // Points 0..filledTo-1 have seen audio (the drawn extent).
    filledTo: 0,
    // Largest |sample| of channel 0 so far, for the shared normalisation.
    level: 0,
    // Song time just after the last sample placed, or -Infinity.
    lastSongSec: -Infinity,
    // Points changed since the last takeDirty(), as an inclusive range.
    dirtyLo: n,
    dirtyHi: -1,
  };
  peaks.pointSec = pointSec > 0 ? pointSec : (peaks.durationSec > 0 ? peaks.durationSec / n : 0);
  for (let c = 0; c < ch; c++) {
    peaks.min.push(new Float32Array(n));
    peaks.max.push(new Float32Array(n));
  }
  return peaks;
}

/**
 * Length of one peaks.json point, as the server cuts them: blocks of
 * floor(frames / points) samples of the stem WAV (audio_stats.scan_stem). Not
 * duration / points: on a 6 s stem at 44.1 kHz that is 88 samples against
 * 88.2, and the difference adds up to half a bar by the middle of the song.
 */
export function serverPointSec(frames, sampleRate, points = PEAK_POINTS) {
  if (!(frames > 0) || !(sampleRate > 0)) return 0;
  return Math.max(1, Math.floor(frames / points)) / sampleRate;
}

/**
 * Sample rate and frame count from the start of a WAV file, or null. Walks
 * the chunk list, so extra chunks before "data" are fine (#358).
 */
export function parseWavFormat(buf) {
  if (!buf || buf.byteLength < 12) return null;
  const view = new DataView(buf);
  const tag = (off) => String.fromCharCode(view.getUint8(off), view.getUint8(off + 1), view.getUint8(off + 2), view.getUint8(off + 3));
  if (tag(0) !== "RIFF" || tag(8) !== "WAVE") return null;
  let off = 12;
  let sampleRate = 0;
  let blockAlign = 0;
  while (off + 8 <= buf.byteLength) {
    const id = tag(off);
    const size = view.getUint32(off + 4, true);
    if (id === "fmt " && off + 24 <= buf.byteLength) {
      sampleRate = view.getUint32(off + 12, true);
      blockAlign = view.getUint16(off + 20, true);
    } else if (id === "data") {
      if (!sampleRate || !blockAlign) return null;
      return { sampleRate, frames: Math.floor(size / blockAlign) };
    }
    off += 8 + size + (size & 1);
  }
  return null;
}

/**
 * Place one block of samples. `channels` are per-channel Float32Arrays of
 * equal length; sample k sits at song time `songStartSec + k / sampleRate`
 * (tempo 1.0: Record refuses any other). Samples before song 0 (a count-in)
 * or past the end are not drawn. A point's [min, max] starts at [0, 0], as
 * the lanes' bars do.
 * Returns the number of samples placed.
 */
export function addBlock(peaks, channels, songStartSec, sampleRate) {
  const n = channels?.[0]?.length || 0;
  if (!n || !(sampleRate > 0) || !(peaks.pointSec > 0)) return 0;
  const perPoint = peaks.pointSec;
  const inv = 1 / sampleRate;
  let placed = 0;
  let lo = peaks.dirtyLo;
  let hi = peaks.dirtyHi;
  const chs = Math.min(peaks.channels, channels.length);
  let level = peaks.level;
  let lastJ = -1;
  for (let k = 0; k < n; k++) {
    const t = songStartSec + k * inv;
    if (t < 0) continue;
    const j = Math.floor(t / perPoint);
    if (j >= peaks.points) break;
    for (let c = 0; c < chs; c++) {
      const v = channels[c][k];
      if (v < peaks.min[c][j]) peaks.min[c][j] = v;
      if (v > peaks.max[c][j]) peaks.max[c][j] = v;
    }
    const a = Math.abs(channels[0][k]);
    if (a > level) level = a;
    if (j < lo) lo = j;
    if (j > hi) hi = j;
    lastJ = j;
    placed++;
  }
  peaks.level = level;
  peaks.dirtyLo = lo;
  peaks.dirtyHi = hi;
  if (lastJ >= 0 && lastJ + 1 > peaks.filledTo) peaks.filledTo = lastJ + 1;
  if (placed) {
    const end = songStartSec + n * inv;
    if (end > peaks.lastSongSec) peaks.lastSongSec = Math.min(end, peaks.durationSec);
  }
  return placed;
}

/**
 * The changed point range since the last call, as [lo, hi] inclusive, or
 * null. Writes into `out` (a 2-slot array) so the draw loop allocates nothing.
 */
export function takeDirty(peaks, out) {
  if (peaks.dirtyHi < peaks.dirtyLo) return null;
  out[0] = peaks.dirtyLo;
  out[1] = peaks.dirtyHi;
  peaks.dirtyLo = peaks.points;
  peaks.dirtyHi = -1;
  return out;
}

/** How many bars the lane draws across `widthPx` (player.js overviewBarCount). */
export function barCount(widthPx) {
  const w = Number(widthPx) || 0;
  return Math.max(MIN_BARS, w > 0 ? Math.round(w / BAR_SLOT_PX) : 220);
}

/** First point of bar `i` of `bars` over `points` (barsWaveformSvg). */
export function barStartPoint(i, bars, points) {
  return Math.floor((i * points) / bars);
}

/** One past the last point of bar `i` (barsWaveformSvg). */
export function barEndPoint(i, bars, points) {
  const start = Math.floor((i * points) / bars);
  return Math.max(start + 1, Math.floor(((i + 1) * points) / bars));
}

/** The bars that contain points lo..hi inclusive, written into `out`. */
export function barsForPoints(lo, hi, bars, points, out) {
  // A point belongs to the bar whose [start, end) holds it; the floor of
  // j * bars / points can be one past it at a rounding edge, so widen by one.
  out[0] = Math.max(0, Math.floor((lo * bars) / points) - 1);
  // With more bars than points one point feeds several bars, up to the one
  // that starts just before point hi + 1.
  out[1] = Math.min(bars - 1, Math.ceil(((hi + 1) * bars) / points));
  return out;
}

/**
 * Peak amplitude of bar `i` on channel `ch`: max(-min, max) over its points,
 * from 0 (barsWaveformSvg). -1 when the bar has no audio yet, so the lane
 * leaves it blank rather than drawing a silent baseline ahead of the take.
 */
export function barAmplitude(peaks, i, bars, ch = 0) {
  const n = peaks.points;
  const start = barStartPoint(i, bars, n);
  if (start >= peaks.filledTo) return -1;
  const end = barEndPoint(i, bars, n);
  const mins = peaks.min[ch];
  const maxs = peaks.max[ch];
  let mn = 0;
  let mx = 0;
  for (let j = start; j < end && j < n; j++) {
    if (mins[j] < mn) mn = mins[j];
    if (maxs[j] > mx) mx = maxs[j];
  }
  return Math.max(-mn, mx);
}

/**
 * Pixel box of bar `i`: [x, y, w, h] written into `out`. Same geometry as
 * the SVG bars: viewBox width = bars, height 48, stretched over the lane.
 */
export function barRect(i, bars, widthPx, heightPx, amp, norm, out) {
  const slot = widthPx / bars;
  const units = Math.max(MIN_UNITS, Math.min(1, amp * norm) * AMP_UNITS);
  out[0] = (i + (1 - BAR_FRAC) / 2) * slot;
  out[2] = BAR_FRAC * slot;
  out[3] = (units / BOX) * heightPx;
  out[1] = ((MID - units / 2) / BOX) * heightPx;
  return out;
}

/**
 * The shared normalisation the finished lane will use: one over the loudest
 * peak of every lane, this take included (renderAllOverviewWaveformsFromPeaks).
 */
export function liveNorm(otherLanesMax, liveLevel) {
  const m = Math.max(Number(otherLanesMax) || 0, Number(liveLevel) || 0);
  return m > 0 ? 1 / m : 0;
}

/** Content x (CSS px) of a song time on a lane `widthPx` wide for `durationSec`. */
export function songSecToX(sec, durationSec, widthPx) {
  return durationSec > 0 ? (sec / durationSec) * widthPx : 0;
}
