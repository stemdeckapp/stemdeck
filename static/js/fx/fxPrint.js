// The export print of a lane's effects chain (Advanced mode, report 3.12).
//
// ffmpeg cannot run the chain: its delays, modulation and saturation are Web
// Audio graphs with no faithful ffmpeg equivalent. So before an export the
// browser renders each lane whose chain changes the sound, in an
// OfflineAudioContext with the same modules the studio plays (pedals.js),
// at the lane file's own rate, from song time 0, and uploads it. The server
// export then uses the print in place of the stem (app/core/fx_prints.py);
// EQ, key, gain, master and limiter run on it as on a stem.
//
// - Format: 24-bit PCM WAV, peak-normalised to -1 dBFS, plus the gain that
//   undoes it (D5): a chain can exceed full scale before the fader, and a
//   file cannot. 24 bits keep quantisation about 140 dB under the peak.
// - Length: the lane plus the chain's tail (capped at 4 s, chainModel.js),
//   with a 50 ms fade at the very end; the chain's own latency is cut from
//   the start, so the printed lane lines up with the others (D8).
// - Upload in parts under the server's 4 MB request limit.
// - The server keys a print by the lane's file and this module's canonical
//   chain string; the same chain on the same file is printed once.

import { createSeries } from "./pedals.js";
import { canonicalChain, chainLatencyFrames, chainTailSec } from "./chainModel.js";
import { loopOverheadFrames, shaperLatencyFrames } from "./dspBlocks.js";

export const PEAK_TARGET = 10 ** (-1 / 20);
const GAIN_MIN = 1e-6;
const GAIN_MAX = 64;
// Under app/main.py's 4 MB body limit, with room for headers.
const PART_BYTES = 3.5 * 1024 * 1024;
const END_FADE_SEC = 0.05;

const OAC = () => globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;

/**
 * Render `buffer` (an AudioBuffer at `rate`) through a chain from song time
 * 0. `minFrames`: at least this many frames of the lane (the server's count
 * of the file). Returns {channels: [Float32Array, Float32Array], frames}.
 */
export async function renderChain({ buffer, desc, inst, bpm, rate, minFrames = 0 }) {
  const Ctx = OAC();
  const loopExtra = { mono: await loopOverheadFrames(rate, 1), shaper4x: await shaperLatencyFrames(rate) };
  const lane = Math.max(buffer.length, minFrames);
  const tail = Math.ceil(chainTailSec(desc, bpm) * rate);
  const lat = chainLatencyFrames(desc, loopExtra);
  const ctx = new Ctx(2, lane + tail + lat, rate);
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  const trim = ctx.createGain();
  trim.gain.value = desc.trim === 0 ? 1 : 10 ** (desc.trim / 20);
  const series = createSeries(ctx, desc, { inst, bpm, loopExtra, songTime: 0, when: 0 });
  src.connect(trim);
  trim.connect(series.input);
  series.output.connect(ctx.destination);
  src.start(0);
  const res = await ctx.startRendering();
  const frames = lane + tail;
  const channels = [0, 1].map((ch) => res.getChannelData(Math.min(ch, res.numberOfChannels - 1)).slice(lat, lat + frames));
  if (tail > 0) {
    const n = Math.min(tail, Math.round(END_FADE_SEC * rate));
    for (const d of channels) {
      for (let i = 0; i < n; i++) d[frames - n + i] *= 0.5 * (1 + Math.cos((Math.PI * (i + 1)) / n));
    }
  }
  series.dispose();
  return { channels, frames };
}

/** Peak-normalise to -1 dBFS and write a 24-bit stereo WAV. {bytes, gain}. */
export function encodeWav24(channels, rate) {
  const frames = channels[0].length;
  let peak = 0;
  for (const d of channels) for (let i = 0; i < frames; i++) { const a = Math.abs(d[i]); if (a > peak) peak = a; }
  const gain = peak > 0 ? Math.max(GAIN_MIN, Math.min(GAIN_MAX, peak / PEAK_TARGET)) : 1;
  const scale = 8388607 / gain;
  const dataBytes = frames * 2 * 3;
  const bytes = new Uint8Array(44 + dataBytes);
  const v = new DataView(bytes.buffer);
  const ascii = (off, s) => { for (let i = 0; i < s.length; i++) bytes[off + i] = s.charCodeAt(i); };
  ascii(0, "RIFF");
  v.setUint32(4, 36 + dataBytes, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, 2, true);
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * 6, true);
  v.setUint16(32, 6, true);
  v.setUint16(34, 24, true);
  ascii(36, "data");
  v.setUint32(40, dataBytes, true);
  let o = 44;
  const [l, r] = channels;
  for (let i = 0; i < frames; i++) {
    for (const d of [l, r]) {
      let s = Math.round(d[i] * scale);
      if (s > 8388607) s = 8388607; else if (s < -8388608) s = -8388608;
      bytes[o++] = s & 0xff;
      bytes[o++] = (s >> 8) & 0xff;
      bytes[o++] = (s >> 16) & 0xff;
    }
  }
  return { bytes, gain };
}

async function asJson(res) {
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json())?.detail || ""; } catch { /* not JSON */ }
    const err = new Error(`HTTP ${res.status}${detail ? `: ${detail}` : ""}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// Prints this page made or found, by job, lane and chain: the master's
// loudness readout builds its query synchronously and reads them here.
const known = new Map();
// Job ids are hex and lane names have no "|", so the key is unambiguous.
const keyOf = (jobId, lane, chain) => [jobId, lane, chain].join("|");

/**
 * Make sure the server holds the print of one lane's chain, and return its
 * id. `item`: {lane, url (the lane's audio), desc, inst, bpm}.
 */
export async function ensurePrint(jobId, item, { signal } = {}) {
  const chain = canonicalChain(item.desc, item.inst, item.bpm);
  const base = `/api/jobs/${encodeURIComponent(jobId)}/fx-prints`;
  const info = await asJson(await fetch(base, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ lane: item.lane, chain }),
    signal,
  }));
  if (!info.done) {
    const audio = await (await fetch(item.url, { signal })).arrayBuffer();
    const Ctx = OAC();
    const decoded = await new Ctx(2, 1, info.rate).decodeAudioData(audio);
    const { channels } = await renderChain({
      buffer: decoded, desc: item.desc, inst: item.inst, bpm: item.bpm, rate: info.rate, minFrames: info.frames,
    });
    const { bytes, gain } = encodeWav24(channels, info.rate);
    const start = (info.next_seq || 0) * PART_BYTES;
    for (let seq = info.next_seq || 0, off = start; off < bytes.length; seq++, off += PART_BYTES) {
      await asJson(await fetch(`${base}/${info.id}/parts?seq=${seq}`, {
        method: "PUT",
        headers: { "content-type": "application/octet-stream" },
        body: bytes.subarray(off, Math.min(bytes.length, off + PART_BYTES)),
        signal,
      }));
    }
    await asJson(await fetch(`${base}/${info.id}/finish`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ lane: item.lane, gain }),
      signal,
    }));
  }
  known.set(keyOf(jobId, item.lane, chain), info.id);
  return info.id;
}

/** Every lane's print, one after the other. Returns the prints= value. */
export async function ensurePrints(jobId, items, opts) {
  const pairs = [];
  for (const item of items) pairs.push(`${item.lane}:${await ensurePrint(jobId, item, opts)}`);
  return pairs.join(",");
}

/**
 * The prints= value from prints already made, or null when one is missing
 * (the chain changed since, or it was never printed).
 */
export function knownPrints(jobId, items) {
  const pairs = [];
  for (const item of items) {
    const id = known.get(keyOf(jobId, item.lane, canonicalChain(item.desc, item.inst, item.bpm)));
    if (!id) return null;
    pairs.push(`${item.lane}:${id}`);
  }
  return pairs.join(",");
}

// Background prints for a reader that cannot wait (the master's readout):
// asked again while a chain moves, made once it has rested for a moment.
let waitTimer = 0;
let running = null;
export function requestPrints(jobId, items, delayMs = 1500) {
  if (waitTimer) clearTimeout(waitTimer);
  waitTimer = setTimeout(() => {
    waitTimer = 0;
    if (running) return;
    running = ensurePrints(jobId, items)
      .catch((err) => console.warn("[fx] printing the effects for the master failed:", err))
      .finally(() => { running = null; });
  }, delayMs);
}
