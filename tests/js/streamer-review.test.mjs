// The take review window of Performance Mode (streamer/reviewModel.js): where
// the live and review windows go at each size, what the review shows, and
// the sync of its video to the transport (the same placement as the export).
import {
  MIN_WINDOW_W, PAIR_MIN_SHARE, STILL_SEC, JUMP_SEC, NUDGE_MAX, SEEK_LEAD,
  reviewTier, reviewView, reviewNote, showsTake, takeVideoTime, syncStep, compactShows, fitRatio,
} from "../../static/js/streamer/reviewModel.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const near = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps;

// ── Tiers ──
// The defaults are the stylesheet's (--sm-col-gap 12, --sm-strip-h 40,
// --sm-gap 8, --sm-tl-min 217, 12 px between the two windows). Nothing sits
// beside the windows any more (no layers panel): they have the whole width.
const base = { pairGap: 12, colGap: 12, stripH: 40, gap: 8, tlMin: 217 };
// A wide, short builder: two windows of the full height fit with room left.
let r = reviewTier({ ...base, width: 1500, height: 670 });
const byH = (670 - 12 - 40 - 8 - 217) * 16 / 9;
check("wide: both windows at full size (tier a)", r.tier === "a" && r.win === Math.floor(byH), JSON.stringify(r));
check("tier a fits the two windows and their gap", 2 * r.win + 12 <= 1500);
check("a layers width from an older caller changes nothing", reviewTier({ ...base, width: 1500, height: 670, layersW: 320 }).tier === "a");
// Not quite room for two full windows: they share the width (tier b).
r = reviewTier({ ...base, width: 1300, height: 670 });
check("medium: two equal windows sharing the width (tier b)", r.tier === "b" && r.win === Math.floor((1300 - 12) / 2), JSON.stringify(r));
check("tier b windows are at least the minimum", r.win >= MIN_WINDOW_W);
check("tier b windows are smaller than the height allows", r.win < byH);
// Too narrow for two windows of 360: one window (tier c).
r = reviewTier({ ...base, width: 700, height: 670 });
check("narrow: one window (tier c)", r.tier === "c", JSON.stringify(r));
check("tier c window takes the width or the height, whichever is less", r.win === Math.floor(Math.min(700, byH)), JSON.stringify(r));
// The boundary between b and c: two minimum windows (here more than 55% of
// the one window: the height allows 600 px, 615 - 277 = 338 px of 16:9).
const edge = 2 * MIN_WINDOW_W + 12;
check("exactly two minimum windows: tier b", reviewTier({ ...base, width: edge, height: 615 }).tier === "b", JSON.stringify(reviewTier({ ...base, width: edge, height: 615 })));
check("one pixel less: tier c", reviewTier({ ...base, width: edge - 1, height: 615 }).tier === "c");
// Two windows of 360 or more, but under 55% of the one window: one window.
r = reviewTier({ ...base, width: 780, height: 950 });
check("two windows under 55% of one: tier c (one large window)", r.tier === "c" && r.win === 780, JSON.stringify(r));
check("PAIR_MIN_SHARE is 0.55", PAIR_MIN_SHARE === 0.55);
// The maintainer's window (1920 x 1080, library folded): the builder is
// 1536 x 841, and the windows get the width the layers panel had: two of 762.
r = reviewTier({ ...base, width: 1536, height: 841 });
check("1920 x 1080 with the library folded: tier b, two of 762", r.tier === "b" && r.win === 762, JSON.stringify(r));
// A short window caps the height-limited width under the minimum: never b.
r = reviewTier({ ...base, width: 1900, height: 450 });
check("too short for a 360 px window: tier c at the minimum", r.tier === "c" && r.win === MIN_WINDOW_W, JSON.stringify(r));
check("a narrow workspace (stacked): always tier c", reviewTier({ ...base, width: 1900, height: 900, stacked: true }).tier === "c");
check("no size yet: tier c", reviewTier({ ...base, width: 0, height: 0 }).tier === "c");
// Monotonic: a wider builder never drops a tier.
const rank = { c: 0, b: 1, a: 2 };
let prev = -1;
let mono = true;
for (let w = 600; w <= 2400; w += 10) {
  const k = rank[reviewTier({ ...base, width: w, height: 700 }).tier];
  if (k < prev) mono = false;
  prev = k;
}
check("a wider builder never drops a tier", mono);

// ── What the review shows ──
const v = (o) => reviewView({ hasTake: true, hasVideo: true, recording: false, playing: false, engaged: false, ...o });
check("recording wins over everything", v({ recording: true, playing: true, engaged: true }) === "recording");
check("no take: none", v({ hasTake: false }) === "none");
check("record not loaded yet: pending", v({ pending: true }) === "pending");
check("no picture: noVideo", v({ hasVideo: false, playing: true }) === "noVideo");
check("playing: play", v({ playing: true }) === "play");
check("paused or moved after Play: still", v({ engaged: true }) === "still");
check("before Play (or after Stop): idle", v({}) === "idle");
check("notes: idle, none, recording, noVideo, pending have one", ["idle", "none", "recording", "noVideo", "pending"].every((k) => reviewNote(k).startsWith("streamer.review.")));
check("notes: play and still have none", reviewNote("play") === "" && reviewNote("still") === "");
check("showsTake only for play and still", showsTake("play") && showsTake("still") && !showsTake("idle") && !showsTake("recording"));

// ── The take's time: the export's placement ──
// The camera's first frame is at camera_start_sec of the song (the export
// trims and places it there: app/core/takes.py, app/api/takes.py).
let tv = takeVideoTime(10, 2.5, 30);
check("in the take: song time less the start", near(tv.at, 7.5) && tv.edge === "in", JSON.stringify(tv));
tv = takeVideoTime(1, 2.5, 30);
check("before the take: its first frame", tv.at === 0 && tv.edge === "before", JSON.stringify(tv));
tv = takeVideoTime(40, 2.5, 30);
check("after the take: its last frame", near(tv.at, 30 - 1 / 30) && tv.edge === "after", JSON.stringify(tv));
tv = takeVideoTime(40, 2.5, Infinity);
check("unknown length: no end", near(tv.at, 37.5) && tv.edge === "in", JSON.stringify(tv));
tv = takeVideoTime(0.5, -0.0966, 18);
check("a take starting before the song (count-in): positive time", near(tv.at, 0.5966) && tv.edge === "in", JSON.stringify(tv));
// Within one frame of the export at every song time, at 30 fps: the review
// and the export use the same placement, so the offset is zero.
let worst = 0;
for (let s = 0; s < 20; s += 0.037) {
  const exportFrameTime = s - 1.234; // render time s shows the video at s - start
  const got = takeVideoTime(s, 1.234, 60).at;
  if (s >= 1.234) worst = Math.max(worst, Math.abs(got - exportFrameTime));
}
check("review time matches the export's placement (under one frame)", worst < 1 / 30, String(worst));

// ── Sync steps ──
let st = syncStep({ playing: true, inTake: true, current: 5, target: 5, paused: true, speed: 1 });
check("playing, in step, paused video: play, no seek", st.play && st.seek === null && st.rate === 1, JSON.stringify(st));
st = syncStep({ playing: true, inTake: true, current: 5 + JUMP_SEC + 0.01, target: 5, paused: false, speed: 1 });
check("far ahead: jump back, a seek's time ahead", near(st.seek, 5 + SEEK_LEAD) && !st.play, JSON.stringify(st));
st = syncStep({ playing: true, inTake: true, current: 5.1, target: 5, paused: false, speed: 1 });
check("a little ahead: slower, no jump", st.seek === null && st.rate < 1 && st.rate >= 1 - NUDGE_MAX, JSON.stringify(st));
st = syncStep({ playing: true, inTake: true, current: 4.9, target: 5, paused: false, speed: 1 });
check("a little behind: faster, no jump", st.seek === null && st.rate > 1 && st.rate <= 1 + NUDGE_MAX, JSON.stringify(st));
st = syncStep({ playing: true, inTake: true, current: 5.01, target: 5, paused: false, speed: 0.75 });
check("in step at 0.75x: the speed itself", st.rate === 0.75, JSON.stringify(st));
st = syncStep({ playing: true, inTake: true, current: 5.1, target: 5, paused: false, speed: 0.5 });
check("the nudge is relative to the speed", st.rate < 0.5 && st.rate > 0.5 * (1 - NUDGE_MAX) - 1e-9, JSON.stringify(st));
st = syncStep({ playing: false, inTake: true, current: 5, target: 8, paused: false, speed: 1 });
check("paused after a seek: pause and show the playhead's frame", st.pause && st.seek === 8, JSON.stringify(st));
st = syncStep({ playing: false, inTake: true, current: 8 + STILL_SEC / 2, target: 8, paused: true, speed: 1 });
check("paused, already there: nothing", !st.pause && st.seek === null && !st.play, JSON.stringify(st));
st = syncStep({ playing: true, inTake: false, current: 3, target: 0, paused: false, speed: 1 });
check("playing outside the take: held on the edge frame, paused", st.pause && st.seek === 0 && !st.play, JSON.stringify(st));
// A simulated play: the video follows a transport at 1x after a seek and a
// speed change, within 100 ms after it settles.
let cur = 0;
let rate = 1;
let paused = true;
let song = 3;
let speed = 1;
let maxErr = 0;
const dt = 1 / 60;
for (let i = 0; i < 60 * 20; i++) {
  if (i === 60 * 5) song = 12; // a seek
  if (i === 60 * 10) speed = 0.75; // a speed change
  song += dt * speed;
  if (!paused) cur += dt * rate * (1 + (i % 7 === 0 ? 0.01 : 0)); // a decoder that runs a little fast
  const target = takeVideoTime(song, 1, 60).at;
  const s2 = syncStep({ playing: true, inTake: true, current: cur, target, paused, speed });
  rate = s2.rate;
  if (s2.seek != null) cur = s2.seek;
  if (s2.play) paused = false;
  if (s2.pause) paused = true;
  const settled = !(i >= 60 * 5 && i < 60 * 5 + 30) && !(i >= 60 * 10 && i < 60 * 10 + 30);
  if (settled && i > 30) maxErr = Math.max(maxErr, Math.abs(cur - target));
}
check("simulated play, seek and speed change: within 100 ms", maxErr < 0.1, String(maxErr));

// ── One window (tier c) ──
check("tiers a and b show both", compactShows({ tier: "a", view: "play", pick: null }) === "both" && compactShows({ tier: "b", view: "idle", pick: null }) === "both");
check("tier c, playing a take: the take", compactShows({ tier: "c", view: "play", pick: null }) === "take");
check("tier c, paused after Play: live (Record in reach)", compactShows({ tier: "c", view: "still", pick: null }) === "live");
check("tier c, paused, Take picked: the paused frame", compactShows({ tier: "c", view: "still", pick: "take" }) === "take");
check("tier c, Live picked: live", compactShows({ tier: "c", view: "play", pick: "live" }) === "live");
check("tier c, idle: live", compactShows({ tier: "c", view: "idle", pick: "take" }) === "live");
check("tier c, recording: live", compactShows({ tier: "c", view: "recording", pick: null }) === "live");

// ── Letterbox ──
let f = fitRatio(1600, 900, 16 / 9);
check("16:9 in 16:9: the whole box", near(f.w, 1600) && near(f.h, 900) && near(f.x, 0));
f = fitRatio(1600, 900, 9 / 16);
check("9:16: pillarboxed, centred", near(f.h, 900) && near(f.w, 506.25) && near(f.x, (1600 - 506.25) / 2));
f = fitRatio(1600, 900, 1);
check("1:1: pillarboxed", near(f.w, 900) && near(f.x, 350));
f = fitRatio(1600, 900, 0);
check("unknown ratio: 16:9", near(f.w, 1600));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
