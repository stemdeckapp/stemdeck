// Reviewing a Performance take: the last take held silent only while a new
// one records (recordModel.takeHoldStep), the new take unmuted once for
// review (recordModel.reviewStep), and what the Video lane shows
// (inputModel.videoLaneState, videoLaneNote, filmSpan, cameraUse).
import { takeHoldStep, reviewStep } from "../../static/js/streamer/recordModel.js";
import { videoLaneState, videoLaneNote, filmSpan, cameraUse } from "../../static/js/streamer/inputModel.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ── The hold ──
let s = takeHoldStep(null, { open: false, trackId: "k1" });
check("no take open: nothing held", eq(s, { held: null, hold: null, release: null }), JSON.stringify(s));
s = takeHoldStep(null, { open: true, trackId: "k1" });
check("a take opens: its track is held", eq(s, { held: "k1", hold: "k1", release: null }), JSON.stringify(s));
s = takeHoldStep("k1", { open: true, trackId: "k1" });
check("every frame while it records: nothing new", eq(s, { held: "k1", hold: null, release: null }), JSON.stringify(s));
s = takeHoldStep("k1", { open: false, trackId: "k1" });
check("the take ends: the hold is let go", eq(s, { held: null, hold: null, release: "k1" }), JSON.stringify(s));
s = takeHoldStep(null, { open: true, trackId: null });
check("the first take (no track yet): nothing held", eq(s, { held: null, hold: null, release: null }), JSON.stringify(s));
s = takeHoldStep("k1", { open: true, trackId: "k2" });
check("another song's track: the old one let go, the new one held", eq(s, { held: "k2", hold: "k2", release: "k1" }), JSON.stringify(s));
// A whole take: start, frames, stop; the lane's mute is never the hold.
let held = null;
const log = [];
for (const open of [false, true, true, true, false, false]) {
  const st = takeHoldStep(held, { open, trackId: "k1" });
  if (st.release) log.push(`release ${st.release}`);
  if (st.hold) log.push(`hold ${st.hold}`);
  held = st.held;
}
check("one hold and one release per take", eq(log, ["hold k1", "release k1"]), JSON.stringify(log));

// ── The review unmute ──
const base = { pending: { job: "j", take: "t2" }, job: "j", mode: "streamer", switching: false, open: false, loaded: true, peaksReady: true, muted: true };
check("a saved take, lanes reloaded, muted: unmute", reviewStep(base) === "unmute");
check("already audible: done, nothing changed", reviewStep({ ...base, muted: false }) === "done");
check("nothing pending: done", reviewStep({ ...base, pending: null }) === "done");
check("the new take's lanes not reloaded yet: wait", reviewStep({ ...base, peaksReady: false }) === "wait");
check("the track not a lane yet: wait", reviewStep({ ...base, loaded: false }) === "wait");
check("another mode's mix is open: wait (never written there)", reviewStep({ ...base, mode: "practice" }) === "wait");
check("a mode switch on its way: wait", reviewStep({ ...base, switching: true }) === "wait");
check("a new take already open: wait", reviewStep({ ...base, open: true }) === "wait");
check("another song is open: dropped", reviewStep({ ...base, job: "other" }) === "drop");

// ── The Video lane ──
const cam = { has_camera: true, camera_start_sec: 29.8 };
check("no take: none", videoLaneState({ lane: "empty", take: null, film: null }) === "none");
check("recording: the growing block", videoLaneState({ lane: "live", take: cam, film: "ok" }) === "recording");
check("saved, the server's record not loaded yet: pending", videoLaneState({ lane: "saved", take: null, film: null }) === "pending");
check("saved without a video part: noVideo", videoLaneState({ lane: "saved", take: { has_camera: false, camera_start_sec: null }, film: null }) === "noVideo");
check("saved, frames on their way: loading", videoLaneState({ lane: "saved", take: cam, film: "loading" }) === "loading");
check("saved, frames not asked yet: loading", videoLaneState({ lane: "saved", take: cam, film: null }) === "loading");
check("saved, frames drawn: film", videoLaneState({ lane: "saved", take: cam, film: "ok" }) === "film");
check("saved, frames failed: failed", videoLaneState({ lane: "saved", take: cam, film: "failed" }) === "failed");
check("note: no video part", videoLaneNote("noVideo", null) === "streamer.lane.noVideo");
check("note: frames failed", videoLaneNote("failed", "on") === "streamer.lane.filmFailed");
check("note: filmed with no camera", videoLaneNote("film", "off") === "streamer.lane.cameraOff");
check("note: an older take filmed with its Camera layer hidden reads as no camera", videoLaneNote("film", "hidden") === "streamer.lane.cameraOff");
check("no note for a filmed take", videoLaneNote("film", "on") === "" && videoLaneNote("film", null) === "");
check("no note while recording or loading", videoLaneNote("recording", "off") === "" && videoLaneNote("loading", "off") === "");
check("camera: no camera open", cameraUse({ live: false }) === "off");
check("camera: on", cameraUse({ live: true }) === "on");
check("camera: never hidden (there is no Camera layer to hide)", cameraUse({ layerVisible: false, live: true }) === "on");
check("span: the camera's first frame for its length", eq(filmSpan(29.8, 15.2, 171), { from: 29.8, to: 45 }));
check("span: a camera started before the song shows from 0", eq(filmSpan(-0.3, 2, 171), { from: 0, to: 1.7 }));
check("span: clipped at the song's end", eq(filmSpan(170, 5, 171), { from: 170, to: 171 }));
check("span: nothing to place", filmSpan(null, 5, 171) === null && filmSpan(3, 0, 171) === null && filmSpan(3, 5, 0) === null);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
