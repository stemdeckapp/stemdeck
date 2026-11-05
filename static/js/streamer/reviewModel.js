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

/** No window (live or review) is ever narrower than this, in CSS pixels. */
export const MIN_WINDOW_W = 360;
// Two windows side by side only while each keeps at least this share of the
// one window's size: below it the live preview would shrink to about half
// of what it was (measured: 374 px windows at 1800 x 1020 with the library
// open, against one of 760 px), and one large window with Live | Take reads
// better. With the library folded (1920 x 1080) the pair is 596 px against
// 1003 (0.59): side by side.
export const PAIR_MIN_SHARE = 0.55;

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
 * timeline) is `width` x `height` CSS pixels; the two windows, when side by
 * side, are `pairGap` apart; under the windows sit the strip (`colGap` +
 * `stripH`), the gap and the timeline at its least (`gap` + `tlMin`). A
 * window is 16:9.
 *
 * - "a": live and review side by side, each as large as the height allows,
 *   with room to spare across.
 * - "b": side by side, sharing the width as two equal windows (each smaller
 *   than the height would allow), both at least MIN_WINDOW_W and at least
 *   PAIR_MIN_SHARE of the one window's size.
 * - "c": one window; the review takes the live window's place while a take
 *   plays.
 * `stacked` (a narrow workspace, where the column scrolls) is always "c".
 * Returns { tier, win } with `win` the width of one window.
 */
export function reviewTier({ width, height, pairGap = 12, colGap = 12, stripH = 40, gap = 8, tlMin = 217, minW = MIN_WINDOW_W, stacked = false }) {
  const byHeight = Math.max(0, (height - colGap - stripH - gap - tlMin) * 16 / 9);
  const avail = Math.max(0, width);
  const single = Math.floor(Math.max(minW, Math.min(avail, byHeight)));
  if (stacked || !(width > 0) || !(height > 0)) return { tier: "c", win: single };
  if (byHeight >= minW && 2 * byHeight + pairGap <= avail) return { tier: "a", win: Math.floor(byHeight) };
  const pair = Math.min(byHeight, (avail - pairGap) / 2);
  if (pair >= minW && pair >= PAIR_MIN_SHARE * single) return { tier: "b", win: Math.floor(pair) };
  return { tier: "c", win: single };
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
 * In tier "c" (one window), which picture is in it: "take" while the take
 * plays, "live" while it is paused (so Record is in reach), unless `pick`
 * ("live", "take" or null), the person's own choice from the Live | Take
 * switch, says otherwise; the pick is kept until the review goes back to
 * idle. In tiers "a" and "b" both are shown: "both".
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
