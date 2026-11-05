// Performance Mode's arithmetic, kept apart from the page so it can be tested
// in Node: the scene a song keeps (its aspect and its trim), where it is kept,
// reading and writing it, its undo history, and the small sums the stage and
// the take timeline draw from (waveform bars, ruler ticks, clock text, the
// timeline's zoom). Nothing here holds state or touches the DOM: every edit
// returns a new scene.
//
// A scene is plain JSON:
//   { v, aspect, trim: { in, out } | null }
// The stage is the camera's picture, full frame, at the scene's aspect: there
// are no layers to place over it. A scene saved by an older build also holds
// `layers` (and anything else it wrote): those are not read, and are written
// back untouched with the next edit, so the old data is left as it was.

export const SCENE_VERSION = 2;
export const ASPECTS = Object.freeze(["16:9", "9:16", "1:1"]);
export const DEFAULT_ASPECT = "16:9";
export const TRIM_MIN = 1;

/** The saved scene of a song: one record per song (maintainer's decision),
 * a song without one starts from the default scene. */
export const sceneKey = (jobId) => `stemdeck:streamer:scene:${jobId}`;

// ── Aspect ──

/** Width over height of `aspect`; the default's for anything unknown. */
export function aspectRatio(aspect) {
  const [w, h] = String(ASPECTS.includes(aspect) ? aspect : DEFAULT_ASPECT).split(":").map(Number);
  return w / h;
}

/** The output size in pixels: the short side is `short` (1080 for 1080p). */
export function outputSize(aspect, short = 1080) {
  const r = aspectRatio(aspect);
  return r >= 1 ? { width: Math.round(short * r), height: short } : { width: short, height: Math.round(short / r) };
}

/** The `aspect` frame centred in a `boxW` x `boxH` box, as large as fits
 * (pillarboxed or letterboxed): { x, y, w, h } in the box's pixels. */
export function fitAspect(boxW, boxH, aspect) {
  const r = aspectRatio(aspect);
  let w = boxW;
  let h = w / r;
  if (h > boxH) {
    h = boxH;
    w = h * r;
  }
  return { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h };
}

/**
 * Where a `iw` x `ih` picture lands in a `bw` x `bh` box: "cover" fills it
 * (cut at the sides or top and bottom), "contain" fits inside it (bars
 * left as they are). { x, y, w, h } relative to the box. The stage draws the
 * camera with "cover": the whole output is picture, never stretched.
 */
export function fitRect(iw, ih, bw, bh, mode = "contain") {
  if (!(iw > 0 && ih > 0 && bw > 0 && bh > 0)) return { x: 0, y: 0, w: 0, h: 0 };
  const s = mode === "cover" ? Math.max(bw / iw, bh / ih) : Math.min(bw / iw, bh / ih);
  const w = iw * s;
  const h = ih * s;
  return { x: (bw - w) / 2, y: (bh - h) / 2, w, h };
}

// ── The scene ──

/** A new scene: `aspect`, the whole song kept. */
export function defaultScene(aspect = DEFAULT_ASPECT) {
  return { v: SCENE_VERSION, aspect: ASPECTS.includes(aspect) ? aspect : DEFAULT_ASPECT, trim: null };
}

/** `scene` at a new aspect (an unknown one leaves it as it is). */
export function withAspect(scene, aspect) {
  if (!ASPECTS.includes(aspect) || aspect === scene.aspect) return scene;
  return { ...scene, aspect };
}

// What a scene is: the fields read here. Everything else a stored record
// holds (an older build's layers) is kept as it was, in `kept`.
const OWN = new Set(["v", "aspect", "trim", "kept"]);

/**
 * A saved scene made safe to use: anything unreadable gives the default
 * scene, an unknown aspect the default one, a trim that cannot be one no
 * trim. The fields of an older build (its layers) are not read; they are
 * carried in `kept` and written back as they were (serializeScene).
 */
export function parseScene(raw) {
  let data = raw;
  if (typeof data === "string") {
    try { data = JSON.parse(data); } catch { return defaultScene(); }
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return defaultScene();
  const scene = defaultScene(data.aspect);
  scene.trim = cleanTrim(data.trim);
  const kept = {};
  for (const [k, v] of Object.entries(data)) if (!OWN.has(k)) kept[k] = v;
  if (Object.keys(kept).length) scene.kept = kept;
  return scene;
}

/** What is stored: a deep copy, the scene's own fields over what an older
 * build left in the record (kept untouched). */
export function serializeScene(scene) {
  return JSON.parse(JSON.stringify({ ...(scene.kept || {}), v: SCENE_VERSION, aspect: scene.aspect, trim: scene.trim ?? null }));
}

function cleanTrim(trim) {
  const a = Number(trim?.in);
  const b = Number(trim?.out);
  if (!Number.isFinite(a) || !Number.isFinite(b) || a < 0 || b - a < TRIM_MIN) return null;
  return { in: a, out: b };
}

// ── Undo history ──

export const HISTORY_CAP = 100;

export function emptyHistory() {
  return { past: [], future: [] };
}

/** The history after an edit made from `before`: a new step, redo cleared. */
export function pushHistory(h, before, cap = HISTORY_CAP) {
  const past = [...h.past, before];
  if (past.length > cap) past.splice(0, past.length - cap);
  return { past, future: [] };
}

/** Undo from `current`: { history, scene } (scene null when nothing to undo). */
export function undoHistory(h, current) {
  if (!h.past.length) return { history: h, scene: null };
  return { history: { past: h.past.slice(0, -1), future: [current, ...h.future] }, scene: h.past[h.past.length - 1] };
}

export function redoHistory(h, current) {
  if (!h.future.length) return { history: h, scene: null };
  const [next, ...future] = h.future;
  return { history: { past: [...h.past, current], future }, scene: next };
}

// ── Trim (the part of the song a take keeps) ──

/** The kept range of `scene` over a song of `duration`: the whole song by default. */
export function trimRange(scene, duration) {
  const d = Math.max(0, Number(duration) || 0);
  const t = scene?.trim;
  if (!t) return { in: 0, out: d };
  const out = Math.min(d, t.out);
  const inn = Math.max(0, Math.min(t.in, out - TRIM_MIN));
  return { in: inn, out: Math.max(out, Math.min(d, inn + TRIM_MIN)) };
}

/** `t` on the nearest of `targets` within `threshold` seconds, else `t`. */
export function snapTime(t, targets, threshold) {
  let best = t;
  let dist = threshold;
  for (const s of targets || []) {
    const d = Math.abs(t - s);
    if (d <= dist) {
      dist = d;
      best = s;
    }
  }
  return best;
}

/**
 * `scene` with the in ("in") or out ("out") point at `t`, held to the song and
 * at least TRIM_MIN seconds from the other point. The whole song is stored as
 * no trim at all.
 */
export function withTrimPoint(scene, which, t, duration) {
  const d = Math.max(0, Number(duration) || 0);
  if (d < TRIM_MIN) return scene;
  const cur = trimRange(scene, d);
  let a = cur.in;
  let b = cur.out;
  if (which === "in") a = Math.max(0, Math.min(Number(t) || 0, b - TRIM_MIN));
  else b = Math.min(d, Math.max(Number(t) || 0, a + TRIM_MIN));
  const trim = a <= 1e-6 && b >= d - 1e-6 ? null : { in: a, out: b };
  return { ...scene, trim };
}

// ── Waveform bars ──

/**
 * `n` bar heights (0..1) from the overview peaks ({ stem: [[min, max], ...] }):
 * each bar the loudest peak of the stems in `names` over its stretch, against
 * the loudest point of those stems in the whole song. `from` and `to`
 * (fractions of the song) draw only a stretch of it, for a zoomed timeline.
 * Empty peaks give zeros.
 */
export function peakBars(peaks, names, n, from = 0, to = 1) {
  const count = Math.max(0, Math.floor(n) || 0);
  const out = new Array(count).fill(0);
  if (!count || !peaks) return out;
  const span = Math.max(1e-9, to - from);
  let max = 0;
  for (const name of names || []) {
    const pts = peaks[name];
    if (!pts?.length) continue;
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i] || [];
      const v = Math.max(Math.abs(Number(p[0]) || 0), Math.abs(Number(p[1]) || 0));
      if (v > max) max = v;
      const f = (i / pts.length - from) / span;
      if (f < 0 || f >= 1) continue;
      const b = Math.min(count - 1, Math.floor(f * count));
      if (v > out[b]) out[b] = v;
    }
  }
  return max > 0 ? out.map((v) => v / max) : out;
}

// ── Time ──

const pad = (n) => String(n).padStart(2, "0");

/** "00:42.3": the take timeline's clock, in tenths. */
export function clockTenths(seconds) {
  const tenths = Math.max(0, Math.floor((Number(seconds) || 0) * 10 + 1e-6));
  const s = Math.floor(tenths / 10);
  return `${pad(Math.floor(s / 60))}:${pad(s % 60)}.${tenths % 10}`;
}

/** "00:00:42": the REC badge's elapsed time. */
export function clockHours(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}

const TICK_STEPS = [5, 10, 15, 30, 60, 120, 300];

/** The ruler's labelled ticks over `duration`: the smallest step that gives
 * at most `maxTicks` of them, as [{ t, label: "m:ss" }]. */
export function rulerTicks(duration, maxTicks = 8) {
  const d = Math.max(0, Number(duration) || 0);
  if (!d) return [];
  const step = TICK_STEPS.find((s) => d / s < maxTicks) ?? TICK_STEPS[TICK_STEPS.length - 1];
  return ticksEvery(d, step);
}

// ── Timeline zoom ──

export const ZOOM_MAX_PPS = 120;

/**
 * Pixels per second at `zoom` (0..1) for a song of `duration` seconds shown
 * in `view` pixels: 0 is the whole song in view (Fit), 1 is ZOOM_MAX_PPS,
 * geometric in between so each step feels the same.
 */
export function pixelsPerSecond(duration, view, zoom) {
  const d = Math.max(1e-6, Number(duration) || 0);
  const fit = Math.max(1e-6, view) / d;
  const max = Math.max(fit, ZOOM_MAX_PPS);
  const z = Math.max(0, Math.min(1, Number(zoom) || 0));
  return fit * Math.pow(max / fit, z);
}

const TICK_STEPS_FINE = [1, 2, 5, 10, 15, 30, 60, 120, 300];

/** The ruler step (seconds) that keeps labels at least `minPx` apart. */
export function tickStep(pps, minPx = 64) {
  return TICK_STEPS_FINE.find((s) => s * pps >= minPx) ?? TICK_STEPS_FINE[TICK_STEPS_FINE.length - 1];
}

/** Labelled ticks every `step` seconds from 0 to `duration`. */
export function ticksEvery(duration, step) {
  const d = Math.max(0, Number(duration) || 0);
  const out = [];
  if (!d || !(step > 0)) return out;
  for (let i = 0; i * step <= d + 1e-6; i++) {
    const t = i * step;
    out.push({ t, label: `${Math.floor(t / 60)}:${pad(Math.floor(t % 60))}` });
  }
  return out;
}
