// The take review window of Performance Mode (streamer/reviewModel.js): where
// the live and review windows go at each size, what the review shows, and
// the sync of its video to the transport (the same placement as the export).
import {
  MIN_WINDOW_W, MIN_SMALL_W, STACK_MAX_SHARE, STILL_SEC, JUMP_SEC, NUDGE_MAX, SEEK_LEAD, SIDE_PANEL_AT,
  reviewTier, foldsLibrary, reviewView, reviewNote, showsTake, takeVideoTime, syncStep, compactShows, fitRatio,
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
// Two side-by-side windows of 360 or more are always side by side, however
// large one window could be (no single-window tier above the hard minimum).
r = reviewTier({ ...base, width: 780, height: 950 });
check("tall and narrow, two of 384 fit across: tier b", r.tier === "b" && r.win === 384, JSON.stringify(r));
// Too narrow for two windows of 360 across: still side by side, smaller,
// down to the hard minimum (the strip and the card keep the height under
// them), however tall the builder.
r = reviewTier({ ...base, width: 700, height: 800 });
check("narrow: two across under 360 (tier b), not stacked", r.tier === "b" && r.win === 344, JSON.stringify(r));
r = reviewTier({ ...base, width: 700, height: 1400 });
check("narrow and very tall: still two across (tier b)", r.tier === "b" && r.win === 344, JSON.stringify(r));
// The 800 x 900 window with the library folded: a builder of 716 x 667.
r = reviewTier({ ...base, width: 716, height: 667 });
check("800 x 900, library folded: two of 352 across", r.tier === "b" && r.win === 352, JSON.stringify(r));
// The boundary between b and s: two windows of 288 across.
const edge = 2 * MIN_SMALL_W + 12;
check("exactly two windows of 288 across: tier b", reviewTier({ ...base, width: edge, height: 900 }).tier === "b", JSON.stringify(reviewTier({ ...base, width: edge, height: 900 })));
// One pixel less: stacked (tier s), within the share of the height and
// leaving the timeline its least.
const stackOf = (w, h) => Math.floor(Math.min(w, (Math.min(STACK_MAX_SHARE * h, h - 277) - 12) / 2 * 16 / 9));
r = reviewTier({ ...base, width: edge - 1, height: 900 });
check("one pixel less, tall: stacked (tier s)", r.tier === "s" && r.win === stackOf(edge - 1, 900), JSON.stringify(r));
check("tier s: the pair within 65% of the height", 2 * (r.win * 9 / 16) + 12 <= STACK_MAX_SHARE * 900 + 1);
check("tier s: the timeline keeps its least", 900 - (2 * (r.win * 9 / 16) + 12) - 12 - 40 - 8 >= 217 - 1);
r = reviewTier({ ...base, width: 500, height: 1400 });
check("narrow and very tall: stacked windows as wide as the builder", r.tier === "s" && r.win === 500, JSON.stringify(r));
check("STACK_MAX_SHARE is 0.65", STACK_MAX_SHARE === 0.65);
// One pixel less but short: no room to stack two of 288 either: one window.
r = reviewTier({ ...base, width: edge - 1, height: 600 });
check("one pixel less, short: one window (tier c)", r.tier === "c", JSON.stringify(r));
// The maintainer's window (1920 x 1080, library folded): the builder is
// 1536 x 841, two of 762.
r = reviewTier({ ...base, width: 1536, height: 841 });
check("1920 x 1080 with the library folded: tier b, two of 762", r.tier === "b" && r.win === 762, JSON.stringify(r));
// A short, wide builder: two smaller windows across rather than one.
r = reviewTier({ ...base, width: 1900, height: 450 });
check("short and wide: two across under 360 (tier b)", r.tier === "b" && r.win === Math.floor((450 - 277) * 16 / 9) && r.win >= MIN_SMALL_W, JSON.stringify(r));
// The hard minimum: no room for two windows of 288 either way.
r = reviewTier({ ...base, width: 1900, height: 420 });
check("too short for two windows of 288: one window (tier c)", r.tier === "c" && r.win === MIN_WINDOW_W, JSON.stringify(r));
check("MIN_SMALL_W is 288", MIN_SMALL_W === 288);
// The narrow workspace (the column scrolls): the same rules with the height
// the column has; below the hard minimum, stacked and scrolling, never one.
r = reviewTier({ ...base, width: 616, height: 530, tlMin: 251, stacked: true });
check("narrow workspace with room for two of 288 across: tier b, no scroll", r.tier === "b" && r.win === 302 && !r.scroll, JSON.stringify(r));
r = reviewTier({ ...base, width: 600, height: 1100, tlMin: 251, stacked: true });
check("narrow workspace, tall, two of 288 fit across: tier b, no scroll", r.tier === "b" && !r.scroll && r.win === 294, JSON.stringify(r));
r = reviewTier({ ...base, width: 560, height: 1100, tlMin: 251, stacked: true });
check("narrow workspace, tall, too narrow for two of 288: stacked to fit, no scroll", r.tier === "s" && !r.scroll && r.win === 560, JSON.stringify(r));
r = reviewTier({ ...base, width: 400, height: 500, tlMin: 251, stacked: true });
check("narrow workspace under the hard minimum: stacked at the builder's width, the column scrolls", r.tier === "s" && r.scroll && r.win === 400, JSON.stringify(r));
check("the wide builder never scrolls by tier", !reviewTier({ ...base, width: 1900, height: 420 }).scroll);
check("no size yet: tier c", reviewTier({ ...base, width: 0, height: 0 }).tier === "c");
// Monotonic: a wider builder never drops a tier, and within a tier its
// windows never shrink, at any height. (From stacked to side by side they
// may: two across leave the timeline far more height.)
const rank = { c: 0, s: 1, b: 2, a: 3 };
let mono = true;
let grow = true;
for (const h of [420, 500, 615, 700, 800, 1000, 1400]) {
  let prev = -1;
  let prevWin = 0;
  let prevTier = "";
  for (let w = 300; w <= 2400; w += 10) {
    const t = reviewTier({ ...base, width: w, height: h });
    const k = rank[t.tier];
    if (k < prev) mono = false;
    if (t.tier === prevTier && t.win < prevWin - 1) grow = false;
    prev = k;
    prevTier = t.tier;
    prevWin = t.win;
  }
}
check("a wider builder never drops a tier", mono);
check("a wider builder never shrinks the windows within a tier", grow);
// Above the hard minimum both windows show, and they always fit the height
// with the timeline at its least.
let fits = true;
for (let h = 440; h <= 1400; h += 20) {
  for (let w = 600; w <= 2400; w += 20) {
    const t = reviewTier({ ...base, width: w, height: h });
    if (t.tier === "c") { fits = false; continue; }
    const winH = t.win * 9 / 16;
    const used = t.tier === "s" ? 2 * winH + 12 : winH;
    if (used + 277 > h + 1 || (t.tier === "s" ? t.win : 2 * t.win + 12) > w + 1) fits = false;
  }
}
check("above the hard minimum: two windows, always within the builder", fits);
// Side by side before stacked: wherever two of 288 fit across (and the
// height leaves them room), the windows are side by side.
let across = true;
for (let h = 440; h <= 1400; h += 20) {
  for (let w = 2 * MIN_SMALL_W + 12; w <= 2400; w += 20) {
    if ((h - 277) * 16 / 9 < MIN_SMALL_W) continue;
    if (!/^[ab]$/.test(reviewTier({ ...base, width: w, height: h }).tier)) across = false;
  }
}
check("two of 288 fit across: never stacked", across);

// -- The library folds for Performance --
// The sidebar is 390 px open and 66 folded (daw.css); the centre column has
// 18 px of padding across.
const lib = { sidebarOpenW: 390, padX: 18 };
// Library open: the layer is the window less 390 (the 1024 x 768 window of
// the report: 634 px of layer, two of 360 do not fit).
check("1024 wide, library open: folds", foldsLibrary({ ...lib, layerW: 1024 - 390, sidebarW: 390 }));
check("800 wide, library open: folds", foldsLibrary({ ...lib, layerW: 800 - 390, sidebarW: 390 }));
check("1240 wide, library open: stays open", !foldsLibrary({ ...lib, layerW: 1240 - 390, sidebarW: 390 }));
check("1280 wide, library open: stays open", !foldsLibrary({ ...lib, layerW: 1280 - 390, sidebarW: 390 }));
check("1366 wide, library open: stays open", !foldsLibrary({ ...lib, layerW: 1366 - 390, sidebarW: 390 }));
check("1920 wide, library open: stays open", !foldsLibrary({ ...lib, layerW: 1920 - 390, sidebarW: 390 }));
// The same answer folded and open, and while the sidebar slides: no flip.
let steady = true;
for (let vw = 700; vw <= 2000; vw += 1) {
  const open = foldsLibrary({ ...lib, layerW: vw - 390, sidebarW: 390 });
  for (const sw of [66, 150, 300, 389]) if (foldsLibrary({ ...lib, layerW: vw - sw, sidebarW: sw }) !== open) steady = false;
}
check("the answer is the same open, folded and sliding", steady);
// The edge: the centre exactly two of 360 and their gap.
const edgeLayer = 2 * MIN_WINDOW_W + 12 + 18;
check("exactly two of 360 beside the open library: stays open", !foldsLibrary({ ...lib, layerW: edgeLayer, sidebarW: 390 }));
check("one pixel less: folds", foldsLibrary({ ...lib, layerW: edgeLayer - 1, sidebarW: 390 }));
check("from SIDE_PANEL_AT the output panel's 300 px count", SIDE_PANEL_AT === 1200 && !foldsLibrary({ ...lib, layerW: 1200, sidebarW: 390 }));
check("no size yet: no fold", !foldsLibrary({ ...lib, layerW: 0, sidebarW: 390 }) && !foldsLibrary({ layerW: 500, sidebarW: 390, sidebarOpenW: NaN }));

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

// ── One window (tier c, the hard minimum) ──
check("tiers a, b and s show both", compactShows({ tier: "a", view: "play", pick: null }) === "both" && compactShows({ tier: "b", view: "idle", pick: null }) === "both" && compactShows({ tier: "s", view: "play", pick: null }) === "both");
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
