// Karaoke Mode's arithmetic (static/js/karaoke/karaokeModel.js).
import {
  clampSpeed, stepSpeed, activePreset, speedPercent, SPEED_PRESETS,
  parseKey, transposedKey, semitoneLabel,
  clockShort, clockLong,
  sungLines, lyricFrame, sungCount, timedWords,
  karaokeStems, karaokeMutedByDefault, karaokeAlwaysOn, queueInsert, queueRemove, queueClean, dropIndex,
} from "../../static/js/karaoke/karaokeModel.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// Speed
check("speed clamps to 0.5..1.5", clampSpeed(0.2) === 0.5 && clampSpeed(3) === 1.5 && clampSpeed("x") === 1);
check("a step is 5% and stays in range", stepSpeed(1, 1) === 1.05 && stepSpeed(1, -1) === 0.95 && stepSpeed(1.5, 1) === 1.5 && stepSpeed(0.5, -1) === 0.5);
check("steps add no float noise", stepSpeed(0.75, 1) === 0.8 && stepSpeed(1.15, 1) === 1.2);
check("presets are 0.5 to 1.5", eq(SPEED_PRESETS, [0.5, 0.75, 1, 1.25, 1.5]));
check("a preset lights only at its exact speed", activePreset(1.25) === 1.25 && activePreset(1.05) === null && activePreset(0.75) === 0.75);
check("speed reads as a percentage", speedPercent(1) === "100%" && speedPercent(0.75) === "75%" && speedPercent(1.25) === "125%");

// Key
check("parses StemDeck's key form", eq(parseKey("E min"), { root: 7, minor: true }) && eq(parseKey("F# maj"), { root: 9, minor: false }));
check("parses flats and long names", eq(parseKey("B♭ minor"), { root: 1, minor: true }) && eq(parseKey("C major"), { root: 3, minor: false }));
check("no key is null", parseKey("— —") === null && parseKey("") === null && transposedKey(null, 2) === null);
check("transpose moves the root", eq(transposedKey("A min", 2), { root: "B", minor: true }) && eq(transposedKey("A min", -1), { root: "A♭", minor: true }));
check("transpose wraps the octave", eq(transposedKey("G maj", 5), { root: "C", minor: false }) && eq(transposedKey("C maj", -12), { root: "C", minor: false }));
check("semitone badge", semitoneLabel(0) === "0" && semitoneLabel(2) === "+2" && semitoneLabel(-1) === "−1");

// Time
check("clocks", clockShort(48) === "0:48" && clockShort(78.9) === "1:18" && clockLong(186) === "03:06" && clockLong(-3) === "00:00");

// Lyrics
const lines = sungLines([
  { time: 10, text: "one two" }, { time: 12, text: "" }, { time: 14, text: "three four" },
  { time: 20, text: "five" }, { time: 25, text: "♪" }, { time: 30, text: "six seven" },
]);
check("empty and ♪ lines are not sung", lines.length === 4);
check("no lines is none", lyricFrame([], 5).state === "none");
const intro = lyricFrame(lines, 4);
check("before the first line: intro, with the first phrase next", intro.state === "intro" && intro.firstAt === 10 && eq(intro.next, ["one two", "three four"]));
const f1 = lyricFrame(lines, 15);
check("current phrase is the pair holding the line", f1.state === "lyric" && eq(f1.current, [0, 1]) && f1.prev === "" && eq(f1.next, ["five", "six seven"]));
const f2 = lyricFrame(lines, 31);
check("second phrase, with the line before it above", eq(f2.current, [2, 3]) && f2.prev === "three four" && eq(f2.next, []));
check("an odd last line is a phrase of one", eq(lyricFrame(lines.slice(0, 3), 21).current, [2]));
const words = timedWords(lines)[0];
check("words come from the Lyrics tab's timing", words.length === 2 && words[0].start === 10);
check("sung words: those started by t", sungCount(words, 9.9) === 0 && sungCount(words, 10) === 1 && sungCount(words, 100) === 2);
const stamped = timedWords([{ time: 5, text: "a b", words: [{ time: 5, text: "a" }, { time: 6, text: "b" }] }])[0];
check("word stamps are used where the lyrics have them", stamped[1].start === 6);

// Queue
check("add to the end", eq(queueInsert(["a", "b"], "c"), ["a", "b", "c"]));
check("add at a place", eq(queueInsert(["a", "b"], "c", 1), ["a", "c", "b"]) && eq(queueInsert(["a", "b"], "c", 0), ["c", "a", "b"]));
check("a place past either end is clamped", eq(queueInsert(["a"], "b", 9), ["a", "b"]) && eq(queueInsert(["a"], "b", -3), ["b", "a"]));
check("a song is queued once: adding it again moves it", eq(queueInsert(["a", "b", "c"], "a", 2), ["b", "c", "a"]) && eq(queueInsert(["a", "b", "c"], "c"), ["a", "b", "c"]));
check("moving counts places without the song itself", eq(queueInsert(["a", "b", "c"], "a", 1), ["b", "a", "c"]));
check("remove", eq(queueRemove(["a", "b"], "a"), ["b"]) && eq(queueRemove(["a"], "z"), ["a"]));
check("clean: strings only, once each, only known songs", eq(queueClean(["a", "a", 3, null, "b", "gone"], (id) => id !== "gone"), ["a", "b"]) && eq(queueClean("nope"), []) && eq(queueClean(undefined), []));
const rows = [{ top: 0, height: 40 }, { top: 48, height: 40 }, { top: 96, height: 40 }];
check("a drop lands before the row whose middle is below it", dropIndex(rows, 10) === 0 && dropIndex(rows, 25) === 1 && dropIndex(rows, 70) === 2);
check("a drop under the last row's middle is the end; no rows is the start", dropIndex(rows, 120) === 3 && dropIndex([], 5) === 0);

// Stems
const split = new Set(["original", "vocals", "lead_vocals", "backing_vocals", "drums", "bass", "guitar", "piano", "other", "k1234abcd"]);
check("split song: Original, then Lead and Backing (not Vocals), no instruments", eq(karaokeStems(split), ["original", "lead_vocals", "backing_vocals"]));
check("unsplit song: one Vocals card, no instruments", eq(karaokeStems(new Set(["vocals", "drums", "bass"])), ["vocals"]));
check("Original is always on the left, the voice right after it", eq(karaokeStems(new Set(["piano", "vocals", "drums", "original"])), ["original", "vocals"]));
check("a vocals-only extraction: its Original backing track, then Vocals", eq(karaokeStems(new Set(["original", "vocals"])), ["original", "vocals"]));
check("Original is a card, and is never off by default", karaokeStems(new Set(["original", "vocals", "piano"])).includes("original") && !karaokeMutedByDefault("original"));
check("half a split is no split", eq(karaokeStems(new Set(["vocals", "lead_vocals", "drums"])), ["vocals"]));
check("missing stems get no card", eq(karaokeStems(new Set(["vocals", "piano"])), ["vocals"]));
check("no instrument ever gets a card", ["drums", "bass", "guitar", "piano", "other"].every((n) => !karaokeStems(new Set([n, "vocals"])).includes(n)));
check("Original and the instruments always play, the voice and backing vocals do not", ["original", "drums", "bass", "guitar", "piano", "other"].every(karaokeAlwaysOn) && !karaokeAlwaysOn("vocals") && !karaokeAlwaysOn("lead_vocals") && !karaokeAlwaysOn("backing_vocals"));
check("defaults: the sung part off, the rest on", karaokeMutedByDefault("lead_vocals") && karaokeMutedByDefault("vocals") && !karaokeMutedByDefault("backing_vocals") && !karaokeMutedByDefault("drums"));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
