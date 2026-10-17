// Stem choice per mode (static/js/partChoice.js v2, playbackStems.js):
// the record model, migration from v1 and from nothing, the layout each mode
// shows, lane set to rest set, the split freeze, and per-mode isolation.
import {
  BASE_STEMS, parseChoice, serializeChoice, mergeChoice, modeLayout, layoutLanes, baseLanes,
  restSetName, originalSource, jobOriginalSet, layoutSignature, songDefaultLanes, splitFreezePatches,
  isSongDefault, choiceKey, STEM_MODES, stemsReasonKey,
} from "../../static/js/partChoice.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";
import { buildPlaybackStems, sumPeaks } from "../../static/js/playbackStems.js";
import { buildRenderRequest } from "../../static/js/streamer/recordModel.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const all = [...BASE_STEMS];
const split = [...all, "lead_vocals", "backing_vocals"];

// ── The record ──
check("a v1 record reads exactly as before", eq(parseChoice({ v: 1, remove: "bass" }), { remove: "bass" }));
check("a v1 record is written back as v1", eq(serializeChoice({ remove: "bass" }), { v: 1, remove: "bass" }));
check("v2 keeps lanes in stem order, each once", eq(parseChoice({ v: 2, remove: "", lanes: ["drums", "vocals", "drums", "kazoo"] }), { remove: "", lanes: ["vocals", "drums"] }));
check("v2 is written when it holds a choice", eq(serializeChoice({ lanes: ["drums"], voice: "split" }), { v: 2, remove: "", lanes: ["drums"], voice: "split" }));
check("an unknown voice is dropped", parseChoice({ voice: "both" }).voice === undefined);
check("an empty lane list is a choice (everything in Original)", eq(parseChoice({ lanes: [] }).lanes, []));
check("merge keeps the rest of the record", eq(mergeChoice({ v: 1, remove: "bass" }, { voice: "combined" }), { remove: "bass", voice: "combined" }));
check("merge with null removes a key", eq(mergeChoice({ lanes: ["drums"], voice: "split" }, { lanes: null, voice: null }), { remove: "" }));
check("Streamer's write keeps a frozen voice", eq(serializeChoice({ ...parseChoice({ v: 2, remove: "", voice: "combined" }), remove: "drums" }), { v: 2, remove: "drums", voice: "combined" }));

// ── Defaults (nothing changes until a choice is made) ──
check("song default: its own selection", eq(songDefaultLanes(["drums", "vocals"]), ["vocals", "drums"]));
check("song default: none recorded is all six", eq(songDefaultLanes([]), all) && eq(songDefaultLanes(null), all));
check("Practice without a record: the song's selection, today's voice rule", eq(modeLayout("practice", null, ["vocals"]), { lanes: ["vocals"], voice: null }));
check("Advanced without a record: the same", eq(modeLayout("daw", null, ["vocals", "piano"]), { lanes: ["vocals", "piano"], voice: null }));
check("Karaoke without a record: the voice only", eq(modeLayout("karaoke", null, ["vocals", "piano"]), { lanes: ["vocals"], voice: null }));
check("Streamer: the song's selection whatever its record", eq(modeLayout("streamer", { remove: "bass", lanes: ["drums"] }, ["vocals"]).lanes, ["vocals"]));
check("Karaoke ignores lanes, keeps its voice", eq(modeLayout("karaoke", { lanes: ["drums"], voice: "split" }, ["vocals"]), { lanes: ["vocals"], voice: "split" }));
check("isSongDefault", isSongDefault(modeLayout("practice", null, ["vocals"]), ["vocals"]) && !isSongDefault({ lanes: ["vocals"], voice: "split" }, ["vocals"]));

// ── What a layout shows ──
check("lanes and Original", eq(layoutLanes({ lanes: ["vocals", "drums"] }, all), { lanes: ["vocals", "drums"], original: ["bass", "guitar", "piano", "other"], split: false }));
check("split done, no voice: the pair (today's rule)", eq(layoutLanes({ lanes: ["vocals"], voice: null }, split).lanes, ["lead_vocals", "backing_vocals"]));
check("split done, voice combined: one Vocals lane", eq(layoutLanes({ lanes: ["vocals"], voice: "combined" }, split).lanes, ["vocals"]));
check("voice split before the split exists: one Vocals lane", eq(layoutLanes({ lanes: ["vocals"], voice: "split" }, all).lanes, ["vocals"]));
check("a missing stem gets no lane and is not in Original", eq(layoutLanes({ lanes: ["vocals", "piano"] }, ["vocals", "drums"]), { lanes: ["vocals"], original: ["drums"], split: false }));
check("all six lanes: no Original", layoutLanes({ lanes: all }, all).original.length === 0);
check("signature tells layouts apart", layoutSignature(layoutLanes({ lanes: ["vocals"] }, all)) !== layoutSignature(layoutLanes({ lanes: ["vocals", "drums"] }, all))
  && layoutSignature(layoutLanes({ lanes: ["vocals"] }, all)) === layoutSignature(layoutLanes(modeLayout("daw", null, ["vocals"]), all)));

// ── Lane set to rest set ──
check("rest set spelling is sorted, as the server's", restSetName(["piano", "bass", "other", "bass"]) === "bass-other-piano");
check("base lanes: the split pair is Voice", eq(baseLanes(["lead_vocals", "backing_vocals", "drums", "original", "tk_1"]), ["vocals", "drums"]));
const urls = { original: "/o.wav", bass: "/b.wav", drums: "/d.wav" };
check("Original = the job's file when it holds exactly that set", eq(originalSource("j1", ["bass", "drums"], ["drums", "bass"], urls), { kind: "job", url: "/o.wav" }));
check("Original = one stem's own file", eq(originalSource("j1", ["bass"], ["drums", "bass"], urls), { kind: "stem", url: "/b.wav" }));
check("Original = the rest file otherwise", eq(originalSource("j1", ["bass", "piano"], ["drums"], urls), { kind: "rest", url: "/api/jobs/j1/rest/bass-piano.wav" }));
check("Original = nothing when empty", originalSource("j1", [], null, urls).kind === "none");
check("job original set: the stems not selected", eq(jobOriginalSet(["vocals"], true), ["drums", "bass", "guitar", "piano", "other"]) && jobOriginalSet(["vocals"], false) === null);

// ── The split freeze ──
const before = { practice: null, daw: { v: 2, remove: "", lanes: ["drums"] }, karaoke: { v: 2, remove: "", voice: "split" }, streamer: { v: 1, remove: "bass" } };
const patches = splitFreezePatches("practice", before);
check("the asking mode asks for the pair", patches.practice.voice === "split");
check("another mode with no voice is frozen at one Vocals lane, its lanes kept", eq(parseChoice(patches.daw), { remove: "", lanes: ["drums"], voice: "combined" }));
check("a mode that chose its voice is left alone", !("karaoke" in patches));
check("Streamer is frozen and keeps its I play", eq(parseChoice(patches.streamer), { remove: "bass", voice: "combined" }));
check("after the split, only the asking mode shows the pair", (() => {
  const shown = (m) => layoutLanes(modeLayout(m, patches[m] ?? before[m], ["vocals"]), split).lanes;
  return eq(shown("practice"), ["lead_vocals", "backing_vocals"]) && eq(shown("daw"), ["drums"]) && eq(shown("streamer"), ["vocals"])
    && eq(layoutLanes(modeLayout("daw", { lanes: ["vocals"], voice: "combined" }, ["vocals"]), split).lanes, ["vocals"]);
})());

// ── Per-mode isolation, through a store ──
const store = new Map();
const job = "ff5490ec654a";
const shownIn = (mode) => layoutLanes(modeLayout(mode, store.get(choiceKey(job, mode)) ?? null, ["vocals"]), all);
const beforeAll = Object.fromEntries(STEM_MODES.map((m) => [m, shownIn(m)]));
store.set(choiceKey(job, "practice"), serializeChoice(mergeChoice(store.get(choiceKey(job, "practice")), { lanes: ["vocals", "drums"] })));
check("Practice changed", eq(shownIn("practice").lanes, ["vocals", "drums"]));
check("Advanced, Karaoke and Streamer unchanged by Practice", ["daw", "karaoke", "streamer"].every((m) => eq(shownIn(m), beforeAll[m])));
store.set(choiceKey(job, "daw"), serializeChoice({ lanes: ["bass"] }));
check("Advanced's choice does not change Practice", eq(shownIn("practice").lanes, ["vocals", "drums"]) && eq(shownIn("daw").lanes, ["bass"]));
check("Practice and Advanced keep separate records", choiceKey(job, "practice") !== choiceKey(job, "daw"));

// ── Playback (buildPlaybackStems) with a layout ──
const raw = [...all, "original"].map((name) => ({ name, url: `/${name}.wav` }));
const layoutPlay = (lanes, extraOriginal) => {
  const visible = [...lanes.map((n) => ({ name: n, url: `/${n}.wav` })), { name: "original", url: extraOriginal }];
  return buildPlaybackStems(raw.filter((s) => s.name !== "original").concat([{ name: "original", url: extraOriginal }]), visible, all);
};
const sub = layoutPlay(["vocals"], "/api/jobs/x/rest/bass-drums-guitar-other-piano.wav");
check("a subset layout plays Original from its parts", eq(sub.filter((s) => s.controlName === "original").map((s) => s.name).sort(), ["bass", "drums", "guitar", "other", "piano"]));
const sup = layoutPlay(["vocals", "drums", "bass"], "/api/jobs/x/rest/guitar-other-piano.wav");
check("a superset layout never plays a laned stem inside Original", sup.filter((s) => s.controlName === "original").every((s) => !["vocals", "drums", "bass"].includes(s.name))
  && sup.filter((s) => s.name === "drums").length === 1);
const withEx = buildPlaybackStems(raw, [{ name: "vocals", url: "/vocals.wav" }, { name: "original", url: "/o.wav" }], all, new Set(["bass"]));
check("a layout plus Streamer's exclusion: bass nowhere", withEx.every((s) => s.name !== "bass"));
const pairRaw = [...raw, { name: "lead_vocals", url: "/l.wav" }, { name: "backing_vocals", url: "/b.wav" }];
const pair = buildPlaybackStems(pairRaw, [{ name: "lead_vocals", url: "/l.wav" }, { name: "backing_vocals", url: "/b.wav" }, { name: "original", url: "/o.wav" }], all);
check("split pair: vocals not in Original", pair.every((s) => s.name !== "vocals"));
const comb = buildPlaybackStems(pairRaw, [{ name: "vocals", url: "/vocals.wav" }, { name: "original", url: "/o.wav" }], all);
check("combined: one vocals source, no split halves", comb.filter((s) => s.name === "vocals").length === 1 && comb.every((s) => !s.name.includes("_vocals")));

// ── Peaks of a made Original ──
const peaks = { bass: [[-0.1, 0.2], [-0.3, 0.1]], drums: [[-0.2, 0.3], [-0.9, 0.95]] };
check("summed peaks: per point, clamped to full scale", eq(sumPeaks(peaks, ["bass", "drums"]), [[-0.30000000000000004, 0.5], [-1, 1]]));
check("summed peaks: none at all is null", sumPeaks(peaks, ["piano"]) === null);

// ── The take render carries the lanes ──
const body = buildRenderRequest({ mix: { names: ["original", "vocals"], gains: [1, 1], pitches: [0, 0], lanes: ["vocals", "drums"] } });
check("render body: lanes sent with the mixed audio", eq(body.lanes, ["vocals", "drums"]));
check("render body: no lanes without the mixed audio", buildRenderRequest({ mix: { names: ["vocals"], gains: [1], lanes: ["vocals"] }, mixedAudio: false }).lanes === undefined);
check("render body: an old mix without lanes sends none", buildRenderRequest({ mix: { names: ["vocals"], gains: [1] } }).lanes === undefined);

// ── What Apply and Extract say (#793) ──
// Each refusal has its own sentence; success is said only for "", and an
// unknown refusal never reads as success.
const reasons = ["recording", "noSong", "failed", "empty", "mode"];
const reasonKeys = reasons.map(stemsReasonKey);
check("success has no refusal text", stemsReasonKey("") === "");
check("every refusal has a key", reasonKeys.every((k) => k.startsWith("stems.")), JSON.stringify(reasonKeys));
check("every refusal has its own key", new Set(reasonKeys).size === reasons.length, JSON.stringify(reasonKeys));
check("no refusal reads as applied", [...reasonKeys, stemsReasonKey("whatever")].every((k) => k && k !== "stems.applied" && k !== "stems.extractApplied"));
const tables = Object.entries(TRANSLATIONS).filter(([code]) => code !== "pt-PT");
const missingText = [];
for (const k of [...reasonKeys, stemsReasonKey("whatever")]) {
  for (const [code, table] of tables) if (!table[k]) missingText.push(`${code}:${k}`);
}
check("every refusal text is in all ten tables", tables.length === 10 && !missingText.length, missingText.join(" "));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
