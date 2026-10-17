// Streamer Mode's recording and export arithmetic, kept apart from the page
// so it can be tested in Node: the recorder's states, where a take starts and
// when it stops itself, the output size and bit rate, the exported file's
// name, and the render request the server is sent. Nothing here touches the
// DOM, a device or the network.

// ── The recorder's states ──
//
//   idle -> starting -> recording -> stopping -> saved
//   starting -> idle (aborted: the view closed before the take began)
//   any of them -> failed (with a reason), saved or failed -> idle (a new take)
//   saved -> exporting -> exported | exportFailed, either -> exporting again
export const RECORD_STATES = Object.freeze([
  "idle", "starting", "recording", "stopping", "saved", "failed", "exporting", "exported", "exportFailed",
]);

const MOVES = {
  idle: { start: "starting" },
  // abort: Streamer closed before the take began (nothing was recorded).
  starting: { started: "recording", stop: "stopping", fail: "failed", abort: "idle" },
  recording: { stop: "stopping", fail: "failed" },
  stopping: { saved: "saved", fail: "failed" },
  saved: { start: "starting", export: "exporting", reset: "idle" },
  failed: { start: "starting", reset: "idle" },
  exporting: { done: "exported", fail: "exportFailed", cancel: "saved" },
  exported: { start: "starting", export: "exporting", reset: "idle" },
  exportFailed: { start: "starting", export: "exporting", reset: "idle" },
};

/** The state after `event`, or `state` itself when the event does not apply. */
export function nextRecordState(state, event) {
  return MOVES[state]?.[event] || state;
}

/** Recording in the wide sense: a take is open (starting, recording, stopping). */
export function isTakeOpen(state) {
  return state === "starting" || state === "recording" || state === "stopping";
}

// ── The last take while a new one records ──
//
// The Performance track's take plays with the backing like any lane, so the
// musician can review it. Only while a NEW take records is it held silent
// (mixer.holdLaneSilent: heard as muted, never saved), so the headphones do
// not carry the last take; the hold is let go when the take ends, and the
// lane's own mute is never touched by it.

/**
 * One step of the hold: `held` is the lane held now (or null), `open` whether
 * a take is open, `trackId` the Performance track of the song (or null).
 * Returns { held, hold, release }: the lane held after the step, the lane to
 * hold now and the lane to let go now (null when none).
 */
export function takeHoldStep(held, { open, trackId }) {
  const want = open && trackId ? trackId : null;
  if (want === held) return { held, hold: null, release: null };
  return { held: want, hold: want, release: held };
}

/**
 * A new take was just saved: unmute its track once, so Play plays it with
 * the backing. Due when the song's lanes have reloaded with the new take
 * (`peaksReady`), the take track is a lane (`loaded`), the open mix record is
 * Performance's own (`mode`, not while `switching`) and no take is open.
 * Returns "wait", "unmute" (it is muted: unmute it), "done" (audible
 * already) or "drop" (another song is open now: forget it).
 */
export function reviewStep({ pending, job, mode, switching, open, loaded, peaksReady, muted }) {
  if (!pending) return "done";
  if (pending.job !== job) return "drop";
  if (open || switching || mode !== "streamer" || !loaded || !peaksReady) return "wait";
  return muted ? "unmute" : "done";
}

// ── Where a take starts and when it stops ──

/**
 * Where recording starts: the playhead when it stands inside the kept range
 * (at least half a second before its end), else the range's start.
 */
export function startPoint(now, range) {
  const t = Number(now) || 0;
  if (t >= range.in && t < range.out - 0.5) return t;
  return range.in;
}

/**
 * Whether the take should stop now: "Auto-stop after song ends" on and the
 * playhead at the range's end. (The song's own end stops a take anyway:
 * the transport ends it, takeRecorder.js.)
 */
export function shouldAutoStop({ t, range, duration, autoStop }) {
  if (!autoStop || !range) return false;
  const end = Math.min(range.out, Number(duration) || range.out);
  return Number(t) >= end - 0.02;
}

// ── Output ──

const SHORT = { 720: 720, 1080: 1080 };

/** The output's pixels for an aspect and a resolution (the server's
 * canvas_size: the short side is the resolution; anything else, such as a
 * take recorded at 4K before it was removed, is 1080). */
export function outputPixels(aspect, resolution) {
  const short = SHORT[resolution] || 1080;
  if (aspect === "1:1") return { width: short, height: short };
  const long = Math.floor((short * 16) / 9);
  return aspect === "9:16" ? { width: short, height: long } : { width: long, height: short };
}

// The server keeps at most 20 Mbit/s of a take's picture per second of song
// (app/core/config.py TAKE_CAMERA_MAX_BYTES_PER_SEC). The recorder's rate is
// a target, not a ceiling (a busy scene overshoots it), so it asks for
// 70% of that at most.
export const CAMERA_CAP_BPS = 20_000_000;
export const BITRATE_MAX = Math.floor(CAMERA_CAP_BPS * 0.7);

/** The recorder's bit rate: about 0.1 bit per pixel per frame, 2.5 Mbps to
 * BITRATE_MAX (14 Mbps). */
export function videoBitrate({ width, height }, fps) {
  const bits = width * height * (Number(fps) || 30) * 0.1;
  return Math.round(Math.max(2_500_000, Math.min(BITRATE_MAX, bits)));
}

// ── The exported file ──

// Characters no file system accepts, and control characters.
const BAD = /[<>:"/\\|?*\u0000-\u001f\u007f]/g;

/**
 * "<Title> - <Artist> (Performance).mp4", made safe for every file system:
 * forbidden characters become spaces, runs of spaces one, no dot or space at
 * the ends, at most 120 characters before the suffix. Without a title,
 * "Performance.mp4"; without an artist, "<Title> (Performance).mp4".
 */
export function exportFileName(title, artist, ext = "mp4") {
  const clean = (s) => String(s || "").replace(BAD, " ").replace(/\s+/g, " ").trim().replace(/^[.\s]+|[.\s]+$/g, "");
  const t = clean(title);
  const a = clean(artist);
  let base = t ? (a ? `${t} - ${a}` : t) : "";
  // Cut by code points, never inside an emoji's surrogate pair (half a pair
  // would make the URL encoder throw).
  const chars = Array.from(base);
  if (chars.length > 120) base = chars.slice(0, 120).join("").trim().replace(/[.\s]+$/g, "");
  // A name Windows reserves (CON, NUL, COM1...) is not a file name there.
  if (/^(con|prn|aux|nul|com\d|lpt\d)$/i.test(base)) base = `${base} song`;
  return base ? `${base} (Performance).${ext}` : `Performance.${ext}`;
}

// ── The render request ──

/**
 * The body of POST /takes/{id}/render for a Streamer take:
 * - mix: { names, gains, pitches } as heard (player.currentMixLanes), with
 *   `exclude` (the Streamer track itself) left out; none at all when
 *   `mixedAudio` is off (the take's sound alone);
 * - the canvas at the scene's aspect, `resolution` and `fps`, fit "fill"
 *   (the recorded picture, the camera cropped to that aspect, is already
 *   the canvas's shape);
 * - the kept range as span "range";
 * - never a click (Performance has no click switch);
 * - the lanes' EQ and the master when given;
 * - `remove`: the part the musician played, left out of the backing.
 */
export function buildRenderRequest({
  mix, exclude = [], mixedAudio = true, aspect = "16:9",
  resolution = "1080", fps = 30, range, fx = "", master = "", remove = "",
}) {
  const names = [];
  const gains = [];
  const pitches = [];
  if (mixedAudio && mix) {
    (mix.names || []).forEach((name, i) => {
      if (exclude.includes(name)) return;
      names.push(name);
      gains.push(mix.gains?.[i] ?? 1);
      pitches.push(mix.pitches?.[i] ?? 0);
    });
  }
  const body = {
    stems: names,
    gains,
    take_gain: 1,
    canvas: aspect,
    fit: "fill",
    mirror: false,
    // A take recorded at 4K (no longer offered) renders at 1080p.
    size: SHORT[resolution] ? String(resolution) : "1080",
    fps: Number(fps),
    click: false,
  };
  if (range) {
    body.span = "range";
    body.range_in = Math.max(0, Math.round(range.in * 1000) / 1000);
    body.range_out = Math.max(body.range_in, Math.round(range.out * 1000) / 1000);
  } else {
    body.span = "take";
  }
  // Omitted when nothing is transposed, so a build without rubberband can
  // still render (videoRender.requestRender's rule).
  if (pitches.some((p) => p !== 0)) body.pitches = pitches;
  if (fx) body.fx = fx;
  if (master) body.master = master;
  // "I play": the part left out of the backing (the server leaves it out of
  // the mix exactly as playback did). Omitted for the whole song.
  if (remove) body.remove_stem = remove;
  // The lanes the song was heard with (player.js _exportLanes), so Original
  // is rebuilt as playback built it. Only with the mixed audio.
  if (mixedAudio && Array.isArray(mix?.lanes)) body.lanes = [...mix.lanes];
  return body;
}

/** The raw recording's file extension from its Content-Type ("webm" when
 * unknown): Chromium records MP4 where it can, WebM elsewhere. */
export function rawExtension(contentType) {
  const type = String(contentType || "").toLowerCase();
  if (type.includes("mp4")) return "mp4";
  if (type.includes("quicktime")) return "mov";
  if (type.includes("matroska")) return "mkv";
  return "webm";
}

/** Whether a refused render means "this ffmpeg cannot make MP4" (the raw
 * recording is then offered instead). */
export function isNoEncoder(status, detail) {
  return status === 422 && /libx264|AAC encoder/i.test(String(detail || ""));
}
