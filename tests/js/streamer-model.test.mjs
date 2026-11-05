// Performance Mode's arithmetic (static/js/streamer/streamerModel.js).
import * as model from "../../static/js/streamer/streamerModel.js";

const {
  SCENE_VERSION, ASPECTS, DEFAULT_ASPECT, sceneKey,
  aspectRatio, outputSize, fitAspect, fitRect,
  defaultScene, withAspect, parseScene, serializeScene,
  peakBars, clockTenths, clockHours, rulerTicks,
  emptyHistory, pushHistory, undoHistory, redoHistory, HISTORY_CAP,
  TRIM_MIN, trimRange, snapTime, withTrimPoint, pixelsPerSecond, tickStep, ticksEvery, ZOOM_MAX_PPS,
} = model;

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const near = (a, b, e = 1e-9) => Math.abs(a - b) < e;

// No layers, no tabs: the scene is the aspect and the trim only.
const s = defaultScene();
check("default scene is 16:9, current version, the whole song", eq(s, { v: SCENE_VERSION, aspect: "16:9", trim: null }) && DEFAULT_ASPECT === "16:9");
check("a scene has no layers", !("layers" in s) && !("layers" in parseScene(JSON.stringify(serializeScene(s)))));
check("the model offers no layer, template or picture editing", ["addLayer", "LAYER_TYPES", "TABS", "lyricWindow", "imageKey", "migrateLayers"].every((k) => !(k in model)));
check("an unknown aspect gives the default scene", defaultScene("4:3").aspect === "16:9");
check("a new scene is a new object each time", defaultScene() !== defaultScene());

// Aspect fractions
check("aspect ratios", near(aspectRatio("16:9"), 16 / 9) && near(aspectRatio("9:16"), 9 / 16) && aspectRatio("1:1") === 1 && near(aspectRatio("nope"), 16 / 9));
check("output sizes keep the short side", eq(outputSize("16:9"), { width: 1920, height: 1080 }) && eq(outputSize("9:16"), { width: 1080, height: 1920 }) && eq(outputSize("1:1", 720), { width: 720, height: 720 }));
check("16:9 fills a 16:9 box", eq(fitAspect(1600, 900, "16:9"), { x: 0, y: 0, w: 1600, h: 900 }));
const p = fitAspect(1600, 900, "9:16");
check("9:16 is pillarboxed and centred", near(p.h, 900) && near(p.w, 900 * 9 / 16) && near(p.x, (1600 - p.w) / 2) && p.y === 0);
const q = fitAspect(1600, 900, "1:1");
check("1:1 is pillarboxed and square", near(q.w, 900) && near(q.h, 900) && near(q.x, 350));
const l = fitAspect(400, 400, "16:9");
check("16:9 in a square box is letterboxed", near(l.w, 400) && near(l.h, 225) && near(l.y, 87.5));
check("cover fills the box (cut at the sides)", eq(fitRect(200, 100, 100, 100, "cover"), { x: -50, y: 0, w: 200, h: 100 }));
check("contain fits inside the box (bars above and below)", eq(fitRect(200, 100, 100, 100, "contain"), { x: 0, y: 25, w: 100, h: 50 }));
check("an empty picture or box draws nothing", eq(fitRect(0, 100, 100, 100), { x: 0, y: 0, w: 0, h: 0 }));

// Changing the aspect
const portrait = withAspect(s, "9:16");
check("withAspect returns a new scene", portrait !== s && s.aspect === "16:9" && portrait.aspect === "9:16");
check("an unknown or the same aspect changes nothing", withAspect(s, "21:9") === s && withAspect(s, "16:9") === s);
check("every aspect is offered", eq([...ASPECTS], ["16:9", "9:16", "1:1"]));

// Per-song key
check("scene key is per song", sceneKey("ff5490ec654a") === "stemdeck:streamer:scene:ff5490ec654a" && sceneKey("a") !== sceneKey("b"));

// Reading and writing
const tr = withTrimPoint(portrait, "in", 12.5, 186);
check("serialize then parse is the same scene", eq(parseScene(JSON.stringify(serializeScene(tr))), tr));
check("serialize writes the scene's own fields", eq(Object.keys(serializeScene(tr)).sort(), ["aspect", "trim", "v"]));
check("garbage gives the default scene", [parseScene("{nope"), parseScene(null), parseScene(7), parseScene([1]), parseScene("")].every((x) => eq(x, defaultScene())));
check("an unknown aspect reads as 16:9", parseScene({ aspect: "4:3" }).aspect === "16:9");
check("a bad trim reads as none", parseScene({ trim: { in: 10, out: 10.5 } }).trim === null && parseScene({ trim: "x" }).trim === null && parseScene({ trim: { in: -1, out: 20 } }).trim === null);

// A scene an older build saved: its layers are not read, never crash, and
// are written back untouched with the next edit.
const OLD = {
  v: 2, aspect: "1:1", trim: { in: 4, out: 40 },
  layers: [
    { id: "camera", type: "camera", visible: false, frame: { x: 0, y: 0, w: 1, h: 1 }, props: { mirror: true } },
    { id: "lyrics", type: "lyrics", visible: true, frame: { x: 0.5, y: 0.6, w: 0.3, h: 0.3 }, props: { lines: 5 } },
    { id: "image", type: "image", visible: true, frame: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, props: { imageKey: "job:image:abc" } },
  ],
};
const old = parseScene(JSON.stringify(OLD));
check("an old scene keeps its aspect and trim", old.aspect === "1:1" && eq(old.trim, { in: 4, out: 40 }));
check("an old scene's layers are not part of the scene", !("layers" in old) && eq(old.kept.layers, OLD.layers));
const edited = withTrimPoint(old, "out", 30, 186);
const saved = serializeScene(edited);
check("an edit writes the old layers back exactly as they were", eq(saved.layers, OLD.layers) && eq(saved.trim, { in: 4, out: 30 }) && saved.aspect === "1:1" && saved.v === 2);
check("the written copy shares nothing with the scene", saved.layers !== OLD.layers && saved.layers[0] !== old.kept.layers[0]);
check("a version 1 scene (old layer types) reads without a throw", (() => {
  const v1 = parseScene({ v: 1, aspect: "9:16", layers: [{ type: "title" }, { type: "albumArt", frame: "x" }, null, 7] });
  return v1.aspect === "9:16" && v1.trim === null && v1.kept.layers.length === 4 && v1.kept.v === undefined;
})());
check("unreadable layers do not matter", parseScene({ aspect: "16:9", layers: "nope" }).aspect === "16:9" && eq(serializeScene(parseScene({ layers: "nope" })).layers, "nope"));
check("other fields an older build wrote are kept too, the scene's own fields win", (() => {
  const x = serializeScene(withAspect(parseScene({ aspect: "1:1", layers: [], extra: { a: 1 }, v: 1 }), "9:16"));
  return eq(x.extra, { a: 1 }) && x.v === SCENE_VERSION && x.aspect === "9:16" && eq(x.layers, []);
})());

// Waveform bars (the take timeline's lanes)
const peaks = { vocals: [[0, 0.5], [-1, 0.2], [0, 0.1], [0, 0]], bass: [[-0.25, 0.25], [0, 0], [0, 0.75], [0, 0]] };
check("bars from one stem, normalised", eq(peakBars(peaks, ["vocals"], 4), [0.5, 1, 0.1, 0]));
check("bars take the loudest stem heard", eq(peakBars(peaks, ["vocals", "bass"], 4), [0.5, 1, 0.75, 0]));
check("bars bucket many points", eq(peakBars(peaks, ["bass"], 2), [1 / 3, 1]));
check("no stems or no peaks give zeros", eq(peakBars(peaks, [], 3), [0, 0, 0]) && eq(peakBars(null, ["vocals"], 2), [0, 0]));
check("zero bars", eq(peakBars(peaks, ["vocals"], 0), []));
const pk = { v: [[0, 1], [0, 0.5], [0, 0.25], [0, 0.125]] };
check("bars of a stretch, against the whole song", eq(peakBars(pk, ["v"], 2, 0.5, 1), [0.25, 0.125]) && eq(peakBars(pk, ["v"], 2), [1, 0.25]));

// Time
check("clock in tenths", clockTenths(42.3) === "00:42.3" && clockTenths(186) === "03:06.0" && clockTenths(-1) === "00:00.0" && clockTenths(62.99) === "01:02.9");
check("REC clock", clockHours(0) === "00:00:00" && clockHours(42.9) === "00:00:42" && clockHours(3725) === "01:02:05");
const ticks = rulerTicks(186);
check("ruler ticks every 30 s for a 3 minute song", eq(ticks.map((x) => x.label), ["0:00", "0:30", "1:00", "1:30", "2:00", "2:30", "3:00"]));
check("ruler ticks stay under the maximum", rulerTicks(600, 8).length <= 8 && rulerTicks(20, 8).map((x) => x.label).join(",") === "0:00,0:05,0:10,0:15,0:20");
check("no duration, no ticks", eq(rulerTicks(0), []));

// Undo history (aspect and trim edits)
let h = emptyHistory();
const s1 = withAspect(s, "9:16");
h = pushHistory(h, s);
const s2 = withTrimPoint(s1, "in", 5, 60);
h = pushHistory(h, s1);
let u = undoHistory(h, s2);
check("undo gives the scene before the last edit", u.scene === s1 && u.history.past.length === 1 && u.history.future[0] === s2);
u = undoHistory(u.history, u.scene);
check("undo again gives the first", u.scene === s && u.history.past.length === 0);
check("nothing more to undo", undoHistory(u.history, u.scene).scene === null);
let r = redoHistory(u.history, u.scene);
check("redo gives the next", r.scene === s1);
r = redoHistory(r.history, r.scene);
check("redo again gives the last", r.scene === s2 && redoHistory(r.history, r.scene).scene === null);
check("a new edit clears redo", pushHistory(undoHistory(h, s2).history, s1).future.length === 0);
let big = emptyHistory();
for (let i = 0; i < HISTORY_CAP + 20; i++) big = pushHistory(big, { i });
check("history is capped at 100, oldest dropped", big.past.length === HISTORY_CAP && big.past[0].i === 20);

// Trim
check("no trim is the whole song", eq(trimRange(s, 186), { in: 0, out: 186 }));
const t1 = withTrimPoint(s, "in", 12.5, 186);
check("in point set", eq(trimRange(t1, 186), { in: 12.5, out: 186 }) && eq(t1.trim, { in: 12.5, out: 186 }));
check("out point set", eq(trimRange(withTrimPoint(t1, "out", 100, 186), 186), { in: 12.5, out: 100 }));
check("the range stays at least one second", eq(trimRange(withTrimPoint(t1, "out", 5, 186), 186), { in: 12.5, out: 13.5 }) && eq(trimRange(withTrimPoint(withTrimPoint(t1, "out", 50, 186), "in", 80, 186), 186), { in: 49, out: 50 }) && TRIM_MIN === 1);
check("points are held to the song", eq(trimRange(withTrimPoint(s, "in", -5, 186), 186), { in: 0, out: 186 }) && eq(trimRange(withTrimPoint(s, "out", 999, 186), 186), { in: 0, out: 186 }));
check("the whole song again stores no trim", withTrimPoint(t1, "in", 0, 186).trim === null);
check("trim survives save and load", eq(parseScene(serializeScene(t1)).trim, { in: 12.5, out: 186 }));
check("a trim past a shorter song is held to it", eq(trimRange({ trim: { in: 10, out: 300 } }, 186), { in: 10, out: 186 }));
check("snap to the nearest target in reach", snapTime(47.8, [0, 48, 78, 186], 0.5) === 48 && snapTime(50, [0, 48, 78], 0.5) === 50 && snapTime(77.9, [78, 77.95], 0.5) === 77.95);

// Zoom
check("zoom 0 fits the song in view", near(pixelsPerSecond(200, 1000, 0), 5));
check("zoom 1 is the closest", near(pixelsPerSecond(200, 1000, 1), ZOOM_MAX_PPS));
check("zoom grows with each step", [0, 0.2, 0.4, 0.6, 0.8, 1].map((z) => pixelsPerSecond(200, 1000, z)).every((v, i, a) => i === 0 || v > a[i - 1]));
check("a song that already fits wider than the max never shrinks", near(pixelsPerSecond(2, 1000, 1), 500));
check("zoom is clamped", near(pixelsPerSecond(200, 1000, -1), 5) && near(pixelsPerSecond(200, 1000, 3), ZOOM_MAX_PPS));
check("tick step keeps labels apart", tickStep(5) === 15 && tickStep(100) === 1 && tickStep(0.1) === 300);
check("ticks every step", eq(ticksEvery(65, 30).map((x) => x.label), ["0:00", "0:30", "1:00"]) && eq(ticksEvery(0, 5), []));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
