// Sound and picture sync of a take with a camera: what the export step says
// about it, and the client for its route. Pure apart from the fetch, which
// is passed in so node tests run it without a server. `t` is i18n.t.
//
// The server (app/core/takes.py) measures where the picture starts against
// the take's own sound when the take is finished, falls back to the
// browser's estimate when it cannot, and keeps a hand nudge on top. The take
// entry carries that as `video_sync`:
//   { source: "measured" | "estimated" | "manual", base: "measured" | "estimated",
//     offsetMs, estimateMs, nudgeMs, confidence (0..1 or null), reason, pictureOffsetMs }
// A positive nudgeMs shows the picture later (the sound then comes earlier
// against it). POST .../sync { nudgeMs } sets it, { remeasure: true } measures
// again; both answer with the take entry.

// The hand nudge offered in the export step: a second either way, in 5 ms
// steps. The server accepts up to 2000; a value set beyond this range is
// shown as it is and only moved when the person moves it.
export const NUDGE_MIN_MS = -1000;
export const NUDGE_MAX_MS = 1000;
export const NUDGE_STEP_MS = 5;

const SOURCES = new Set(["measured", "estimated", "manual"]);

// The server's reason codes, each to the sentence that says it.
const REASONS = {
  "no-camera-audio": "avsync.reason.noCameraAudio",
  "no-camera-mic": "avsync.reason.noCameraMic",
  "camera-mic-denied": "avsync.reason.cameraMicDenied",
  "camera-mic-not-asked": "avsync.reason.cameraMicNotAsked",
  "camera-mic-failed": "avsync.reason.cameraMicFailed",
  silent: "avsync.reason.silent",
  "low-confidence": "avsync.reason.lowConfidence",
  "measure-failed": "avsync.reason.measureFailed",
  legacy: "avsync.reason.legacy",
};

/** Whole milliseconds on the 5 ms grid, within the nudge range; anything not finite is 0. */
export function clampNudgeMs(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  const stepped = Math.round(n / NUDGE_STEP_MS) * NUDGE_STEP_MS;
  // -0 reads as "0" everywhere, but Object.is tells it apart.
  return Math.max(NUDGE_MIN_MS, Math.min(NUDGE_MAX_MS, stepped)) || 0;
}

const num = (v, fallback = 0) => (Number.isFinite(Number(v)) && v !== null && v !== "" ? Number(v) : fallback);

/**
 * The take entry's sync, made safe to show: null when the take has no
 * camera or the server did not say (an older server).
 */
export function readSync(take) {
  const s = take?.video_sync;
  if (!take?.has_camera || !s || typeof s !== "object") return null;
  const nudgeMs = num(s.nudgeMs);
  const base = s.base === "measured" ? "measured" : "estimated";
  let source = SOURCES.has(s.source) ? s.source : base;
  // "manual" whenever a nudge is set, whatever the block says.
  if (nudgeMs !== 0) source = "manual";
  else if (source === "manual") source = base;
  const offsetMs = num(s.offsetMs);
  const confidence = Number.isFinite(s.confidence) ? Math.max(0, Math.min(1, s.confidence)) : null;
  return {
    source,
    base,
    offsetMs,
    nudgeMs,
    confidence,
    reason: typeof s.reason === "string" ? s.reason : "",
    pictureOffsetMs: num(s.pictureOffsetMs, offsetMs + nudgeMs - num(take.camera_offset_ms)),
  };
}

/** The sentence key for an estimate's reason; an unknown code gets the general one. */
export function reasonKey(reason) {
  return REASONS[reason] || "avsync.reason.other";
}

/** "+40", "-15", "0": a signed whole number of milliseconds. */
export function signedMs(ms) {
  const n = Math.round(num(ms));
  if (n === 0) return "0";
  return n > 0 ? `+${n}` : String(n);
}

/** What the state line says: how the picture was placed, in words. */
export function stateText(sync, t) {
  if (!sync) return "";
  if (sync.source === "manual") {
    return t(sync.base === "measured" ? "avsync.manualMeasured" : "avsync.manualEstimated");
  }
  if (sync.source === "measured") {
    return sync.confidence === null
      ? t("avsync.measured")
      : t("avsync.measuredMatch", { pct: Math.round(sync.confidence * 100) });
  }
  return t(reasonKey(sync.reason));
}

/** Where the picture starts against the sound, in words, with the milliseconds. */
export function offsetText(sync, t) {
  if (!sync) return "";
  const n = Math.round(sync.pictureOffsetMs);
  if (n === 0) return t("avsync.offsetTogether");
  return t(n > 0 ? "avsync.offsetAfter" : "avsync.offsetBefore", { n: Math.abs(n) });
}

/**
 * Everything the control shows for `sync`: { show, state, offset, warn,
 * nudge, value, canReset, canRemeasure }. `show` is false for a take with
 * no picture.
 */
export function syncView(sync, t) {
  if (!sync) return { show: false, state: "", offset: "", warn: "", nudge: 0, value: "", canReset: false, canRemeasure: false };
  const nudge = clampNudgeMs(sync.nudgeMs);
  return {
    show: true,
    state: stateText(sync, t),
    offset: offsetText(sync, t),
    // An estimate may be off: say so, and what fixes it.
    warn: sync.source === "estimated" ? t("avsync.warn") : "",
    nudge,
    value: t("avsync.ms", { n: signedMs(sync.nudgeMs) }),
    canReset: sync.nudgeMs !== 0,
    // Only a measurement that did not finish is worth running again: the
    // other reasons give the same answer from the same files.
    canRemeasure: sync.base === "estimated" && sync.reason === "measure-failed",
  };
}

/** The route for a take's sync. */
export function syncUrl(jobId, takeId) {
  return `/api/jobs/${encodeURIComponent(jobId)}/takes/${encodeURIComponent(takeId)}/sync`;
}

/**
 * POST the sync change: { nudgeMs } or { remeasure: true }. Resolves with
 * the take entry; rejects with an Error carrying { status, detail } (status
 * 0 when the server was not reached).
 */
export async function postSync(jobId, takeId, body, fetchImpl = globalThis.fetch) {
  let res;
  try {
    res = await fetchImpl(syncUrl(jobId, takeId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw Object.assign(new Error(String(err?.message || err)), { status: 0, detail: "" });
  }
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    console.warn("[avsync] the sync answer was not JSON:", err);
  }
  if (!res.ok) {
    const detail = typeof data?.detail === "string" ? data.detail : "";
    throw Object.assign(new Error(detail || `HTTP ${res.status}`), { status: res.status, detail });
  }
  return data;
}

/** The sentence for a refused or failed sync change. */
export function refusalText(err, t) {
  const status = Number(err?.status);
  const detail = String(err?.detail || "");
  if (status === 0) return t("avsync.err.offline");
  if (detail === "video render running") return t("avsync.err.running");
  if (detail === "take not finished") return t("avsync.err.notFinished");
  if (detail === "take has no camera") return t("avsync.err.noCamera");
  if (status === 404) return t("avsync.err.gone");
  const reason = detail.replace(/\s+/g, " ").trim().slice(0, 160);
  return reason ? t("avsync.err.failedWhy", { reason }) : t("avsync.err.failed");
}
