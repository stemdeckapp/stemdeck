// The take review window of Performance Mode (streamer.js): the recorded take
// played back beside the live preview, in step with the transport. Pure
// functions only, so node can test them (tests/js/streamer-review.test.mjs):
// where the windows go at a given size, what the review window shows, and
// how its <video> is kept on the song's clock.
//
// What the review shows is the take's own picture: a Performance take films
// the camera at the output's shape (drawn by the one stage renderer without
// the live window's guides), and the export renders that picture as it is
// (app/api/takes.py, fit "fill"). So nothing is drawn over it here: it is
// exactly the picture the MP4 will have.

/** Two windows side by side are each at least this wide, in CSS pixels;
 * narrower, they stack, or stay across down to MIN_SMALL_W in a builder too
 * short for stacking to do better (see reviewTier). */
export const MIN_WINDOW_W = 360;
// The hard minimum: two windows (side by side or stacked) are never
// narrower than this (162 px of 16:9 height). Below it, and only there,
// one window is shown (tier "c"). With the timeline at its least (217 px)
// and the strip, that is a builder under about 440 px high (400 in a short
// workspace), where nothing else fits.
export const MIN_SMALL_W = 288;
// Stacked, the two windows and the gap between them take at most this
// share of the builder's height: the strip and the Timeline / Effects card
// keep the rest (and never less than the timeline's least).
export const STACK_MAX_SHARE = 0.65;

// Stopped or scrubbing: the frame shown is the playhead's within this much
// (videoPreview.js STILL_SEC).
export const STILL_SEC = 0.04;
// Playing: a picture this far off the playhead jumps back to it; nearer, its
// rate is nudged so it drifts back without a visible jump.
export const JUMP_SEC = 0.25;
export const NUDGE_SEC = 0.02;
// The most the rate is nudged, as a fraction of the playback speed, and how
// hard: a picture 50 ms off plays 10% faster or slower until it is back.
export const NUDGE_MAX = 0.2;
export const NUDGE_GAIN = 2;
// A jump while playing lands this far ahead (song seconds at 1x): the video
// takes a moment to seek and start, while the song plays on (rv1.mjs on the
// camera MP4s: about 50 ms behind after a seek without it, about 100 ms
// ahead with 60 ms).
export const SEEK_LEAD = 0.03;

/**
 * Where the windows go. The view's centre (the windows, the strip and the
 * timeline) is `width` x `height` CSS pixels; the two windows are `pairGap`
 * apart (across or down); under the windows sit the strip (`colGap` +
 * `stripH`), the gap and the timeline at its least (`gap` + `tlMin`). A
 * window is 16:9. The live window and the take review are both shown in
 * every tier but "c".
 *
 * - "a": side by side, each as large as the height allows, with room to
 *   spare across.
 * - "b": side by side, sharing the width as two equal windows, each at least
 *   MIN_WINDOW_W (or, in a short builder where stacking cannot do better,
 *   at least MIN_SMALL_W).
 * - "s": stacked, the live window over the review, when the width is too
 *   narrow for two side by side: each as wide as the builder allows while
 *   the pair stays within STACK_MAX_SHARE of the height and leaves the
 *   timeline its least.
 * - "c": the hard minimum, below MIN_SMALL_W for both layouts: one window,
 *   the review taking the live window's place while a take plays.
 * `stacked` is the narrow workspace (under 680 px), where the column already
 * scrolls: the same rules (`height` is then the height the column has for
 * the builder), but below the hard minimum the two windows stack at the
 * stylesheet's width and the column scrolls ({ tier: "s", scroll: true }),
 * never one window.
 * Returns { tier, win, scroll } with `win` the width of one window.
 */
export function reviewTier({ width, height, pairGap = 12, colGap = 12, stripH = 40, gap = 8, tlMin = 217, minW = MIN_WINDOW_W, minSmall = MIN_SMALL_W, stacked = false }) {
  const room = height - colGap - stripH - gap - tlMin;
  const byHeight = Math.max(0, room * 16 / 9);
  const avail = Math.max(0, width);
  const single = Math.floor(Math.max(minW, Math.min(avail, byHeight)));
  if (!(width > 0) || !(height > 0)) return stacked ? { tier: "s", win: Math.floor(avail), scroll: true } : { tier: "c", win: single, scroll: false };
  if (byHeight >= minW && 2 * byHeight + pairGap <= avail) return { tier: "a", win: Math.floor(byHeight), scroll: false };
  const pair = Math.min(byHeight, (avail - pairGap) / 2);
  if (pair >= minW) return { tier: "b", win: Math.floor(pair), scroll: false };
  const stackH = Math.max(0, Math.min(STACK_MAX_SHARE * height, room) - pairGap) / 2;
  const stack = Math.min(avail, stackH * 16 / 9);
  if (stack >= pair && stack >= minSmall) return { tier: "s", win: Math.floor(stack), scroll: false };
  if (pair >= minSmall) return { tier: "b", win: Math.floor(pair), scroll: false };
  if (stacked) return { tier: "s", win: Math.floor(avail), scroll: true };
  return { tier: "c", win: single, scroll: false };
}

/**
 * What the review window shows:
 * - "recording": a new take is being recorded (the window waits for it);
 * - "none": the song has no take yet;
 * - "pending": the take is saved, the server's record of it not here yet;
 * - "noVideo": the take has no picture;
 * - "play": the take plays along with the transport;
 * - "still": the transport is paused (or was moved) after Play: the frame at
 *   the playhead;
 * - "idle": a take is there, Play not pressed yet (or Stop pressed).
 * `engaged`: Play was pressed, or the playhead moved, since the last Stop.
 */
export function reviewView({ hasTake, pending = false, hasVideo, recording, playing, engaged }) {
  if (recording) return "recording";
  if (!hasTake) return "none";
  if (pending) return "pending";
  if (!hasVideo) return "noVideo";
  if (playing) return "play";
  if (engaged) return "still";
  return "idle";
}

/** The i18n key of the review window's note for `view`, or "" for none. */
export function reviewNote(view) {
  return {
    recording: "streamer.review.recording",
    none: "streamer.review.none",
    pending: "streamer.review.loading",
    noVideo: "streamer.review.noVideo",
    idle: "streamer.review.idle",
  }[view] || "";
}

/** The view that shows a picture of the take. */
export const showsTake = (view) => view === "play" || view === "still";

/**
 * The take's video time for the song time `songSec`: the camera's first frame
 * falls at `startSec` of the song (the server's camera_start_sec, which the
 * export uses to place the picture: app/core/takes.py), so the picture at song
 * time T is the video at T - startSec, the same as in the MP4. `durationSec`
 * is the video's length (Infinity when unknown). Outside the take the first
 * or last frame is held: `edge` says which ("before", "in" or "after").
 */
export function takeVideoTime(songSec, startSec, durationSec, frameSec = 1 / 30) {
  const at = Number(songSec) - Number(startSec);
  const dur = Number(durationSec);
  if (!Number.isFinite(at)) return { at: 0, edge: "before" };
  if (at < 0) return { at: 0, edge: "before" };
  if (Number.isFinite(dur) && dur > 0 && at >= dur) return { at: Math.max(0, dur - frameSec), edge: "after" };
  return { at, edge: "in" };
}

/**
 * One step of keeping the video on the transport. `current` is the video's
 * time, `target` the take time the playhead asks for (takeVideoTime), `speed`
 * the transport's playback speed. Returns { seek (a time, or null), rate,
 * play, pause }.
 */
export function syncStep({ playing, inTake, current, target, paused, speed }) {
  const base = speed > 0 ? speed : 1;
  const d = Number(current) - Number(target);
  if (!playing || !inTake) {
    return { seek: Math.abs(d) > STILL_SEC || !Number.isFinite(d) ? target : null, rate: base, play: false, pause: !paused };
  }
  if (!Number.isFinite(d) || Math.abs(d) > JUMP_SEC) return { seek: target + SEEK_LEAD * base, rate: base, play: paused, pause: false };
  let rate = base;
  if (Math.abs(d) > NUDGE_SEC) rate = base * (1 - Math.max(-NUDGE_MAX, Math.min(NUDGE_MAX, d * NUDGE_GAIN)));
  return { seek: null, rate, play: paused, pause: false };
}

/**
 * In tier "c" (one window, the hard minimum), which picture is in it:
 * "take" while the take plays, "live" while it is paused (so Record is in
 * reach), unless `pick`
 * ("live", "take" or null), the person's own choice from the Live | Take
 * switch, says otherwise; the pick is kept until the review goes back to
 * idle. In tiers "a", "b" and "s" both are shown: "both".
 */
export function compactShows({ tier, view, pick }) {
  if (tier !== "c") return "both";
  if (!showsTake(view)) return "live";
  if (pick) return pick;
  // Paused, the live scene (and its Record button) comes back; Take shows
  // the paused frame.
  return view === "play" ? "take" : "live";
}

/** Where a picture of `ratio` (width / height) sits in a box, letterboxed. */
export function fitRatio(boxW, boxH, ratio) {
  const r = ratio > 0 ? ratio : 16 / 9;
  let w = boxW;
  let h = w / r;
  if (h > boxH) {
    h = boxH;
    w = h * r;
  }
  return { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h };
}
