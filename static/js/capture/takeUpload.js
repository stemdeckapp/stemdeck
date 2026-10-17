// The takes API from the browser side: create, chunked parts, finish, patch,
// delete. Shapes are the backend's (app/api/takes.py, agent B):
//   POST   /api/jobs/{id}/takes {track_id}              -> {take_id, label}
//   POST/PATCH/DELETE /api/jobs/{id}/tracks[/{tid}]     -> recording tracks
//   PUT    /api/jobs/{id}/takes/{tid}/parts/{kind}?seq=n -> {seq, bytes, duplicate}
//   GET    /api/jobs/{id}/takes/{tid}/parts              -> {audio:{next_seq}, camera:{next_seq}, finished}
//   POST   /api/jobs/{id}/takes/{tid}/finish             -> take entry
//   PATCH  /api/jobs/{id}/takes/{tid}                    -> take entry
//   DELETE /api/jobs/{id}/takes/{tid}                    -> {take_id, status}
// Parts are at most 4 MB, seq starts at 0 per kind and must go up by one; a
// repeat of the last seq is a no-op, which is what makes a retry safe.

import { PART_MAX_BYTES } from "./takeMath.js";

const base = (jobId, takeId) => `/api/jobs/${encodeURIComponent(jobId)}/takes${takeId ? `/${encodeURIComponent(takeId)}` : ""}`;

export class TakeApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail || "";
  }
}

async function call(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json())?.detail || ""; } catch { /* not JSON */ }
    throw new TakeApiError(res.status, typeof detail === "string" ? detail : "");
  }
  try { return await res.json(); } catch { return {}; }
}

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body ?? {}),
});

/** A take always belongs to a recording track. */
export function createTake(jobId, label, trackId) {
  return call(base(jobId), json("POST", { ...(label ? { label } : {}), track_id: trackId }));
}

// ── Recording tracks ──
const trackBase = (jobId, trackId) => `/api/jobs/${encodeURIComponent(jobId)}/tracks${trackId ? `/${encodeURIComponent(trackId)}` : ""}`;

/** POST /tracks {name?, input?} -> the track. */
export function createTrack(jobId, fields = {}) {
  return call(trackBase(jobId), json("POST", fields));
}

/** PATCH /tracks/{id} with any of {name, input, active_take_id, order}. */
export function patchTrack(jobId, trackId, fields) {
  return call(trackBase(jobId, trackId), json("PATCH", fields));
}

/** DELETE /tracks/{id}: the track and every take on it. */
export function deleteTrack(jobId, trackId) {
  return call(trackBase(jobId, trackId), { method: "DELETE" });
}

export function finishTake(jobId, takeId, sidecar) {
  return call(`${base(jobId, takeId)}/finish`, json("POST", sidecar));
}

export function patchTake(jobId, takeId, fields) {
  return call(base(jobId, takeId), json("PATCH", fields));
}

export function deleteTake(jobId, takeId) {
  return call(base(jobId, takeId), { method: "DELETE" });
}

export function getPartsState(jobId, takeId) {
  return call(`${base(jobId, takeId)}/parts`, { method: "GET" });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const RETRIES = 3;

/**
 * Upload one part and resolve on the server's acknowledgement. Retries a
 * network failure or a 5xx three times with backoff. A 409 for the sequence
 * is checked against the server: if the server already has this part (a
 * lost reply), that counts as acknowledged.
 *
 * Shared with the camera recorder: it keeps its own seq per take and splits
 * anything over 4 MB itself.
 */
export async function uploadPart(jobId, takeId, kind, seq, data) {
  const size = data?.byteLength ?? data?.size ?? 0;
  if (size > PART_MAX_BYTES) throw new TakeApiError(413, "part over 4 MB");
  const url = `${base(jobId, takeId)}/parts/${kind}?seq=${seq}`;
  let lastErr = null;
  for (let attempt = 0; attempt <= RETRIES; attempt++) {
    if (attempt) await sleep(500 * 2 ** (attempt - 1));
    try {
      return await call(url, {
        method: "PUT",
        headers: { "Content-Type": "application/octet-stream" },
        body: data,
      });
    } catch (e) {
      lastErr = e;
      if (e instanceof TakeApiError) {
        if (e.status === 409) {
          try {
            const st = await getPartsState(jobId, takeId);
            if ((st?.[kind]?.next_seq ?? 0) > seq) return { seq, duplicate: true };
          } catch (e2) { console.warn("[takes] part resync failed:", e2); }
          throw e;
        }
        if (e.status < 500) throw e;
      }
      console.warn(`[takes] part ${kind} ${seq} failed (attempt ${attempt + 1}):`, e);
    }
  }
  throw lastErr;
}

/**
 * Batches float32 PCM into parts and sends them in order, one at a time, as
 * the take is recorded (every 2 s), so a crash loses seconds and the browser
 * never holds the whole take. The take id may arrive after the first frames:
 * nothing is sent until `setTake` gives it.
 */
export class AudioPartQueue {
  constructor({ channels, flushMs = 2000 } = {}) {
    this.maxBytes = Math.floor(PART_MAX_BYTES / (4 * channels)) * 4 * channels;
    this.pending = [];
    this.pendingBytes = 0;
    this.seq = 0;
    this.jobId = null;
    this.takeId = null;
    this.chain = Promise.resolve();
    this.error = null;
    this.bytesSent = 0;
    this.timer = setInterval(() => this.cut(), flushMs);
  }

  setTake(jobId, takeId) {
    this.jobId = jobId;
    this.takeId = takeId;
    this.cut();
  }

  /** Add one interleaved Float32Array. */
  push(samples) {
    this.pending.push(samples);
    this.pendingBytes += samples.byteLength;
    if (this.pendingBytes >= this.maxBytes) this.cut();
  }

  /** Close the current batch into parts and queue them. */
  cut() {
    if (!this.takeId || !this.pendingBytes) return;
    const all = new Uint8Array(this.pendingBytes);
    let off = 0;
    for (const s of this.pending) {
      all.set(new Uint8Array(s.buffer, s.byteOffset, s.byteLength), off);
      off += s.byteLength;
    }
    this.pending = [];
    this.pendingBytes = 0;
    for (let i = 0; i < all.length; i += this.maxBytes) {
      const part = all.slice(i, Math.min(all.length, i + this.maxBytes));
      const seq = this.seq++;
      this.chain = this.chain.then(async () => {
        if (this.error) return;
        try {
          await uploadPart(this.jobId, this.takeId, "audio", seq, part);
          this.bytesSent += part.byteLength;
        } catch (e) {
          this.error = e;
          console.warn("[takes] audio part upload gave up:", e);
        }
      });
    }
  }

  /** Send everything left and resolve when the last part is acknowledged. */
  async drain() {
    clearInterval(this.timer);
    this.cut();
    await this.chain;
    if (this.error) throw this.error;
    return this.bytesSent;
  }

  dispose() {
    clearInterval(this.timer);
    this.pending = [];
    this.pendingBytes = 0;
  }
}
