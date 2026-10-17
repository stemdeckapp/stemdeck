// "I play" (static/js/partChoice.js): the part a mode leaves out of a song,
// per song and per mode; and playback leaving it out (playbackStems.js).
import {
  STEM_CHOICES, BASE_STEMS, choiceKey, parseChoice, serializeChoice, offeredChoices, effectiveChoice,
  excludedNames, sameNames, timelineLanes,
} from "../../static/js/partChoice.js";
import { buildPlaybackStems } from "../../static/js/playbackStems.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const throws = (fn) => { try { fn(); return false; } catch { return true; } };

// The record
check("one record per song and per mode", choiceKey("ff5490ec654a", "streamer") === "stemdeck:stems:ff5490ec654a:streamer"
  && choiceKey("ff5490ec654a", "practice") !== choiceKey("ff5490ec654a", "streamer"));
check("a key needs a job and a plain mode", throws(() => choiceKey("", "streamer")) && throws(() => choiceKey("x", "a:b")) && throws(() => choiceKey("x", "")));
check("the choices: nothing, then the parts a musician plays", eq(STEM_CHOICES, ["", "drums", "bass", "guitar", "piano", "vocals"]) && !STEM_CHOICES.includes("other"));
check("the six Demucs stems", eq(BASE_STEMS, ["vocals", "drums", "bass", "guitar", "piano", "other"]));
check("a stored choice reads back", eq(parseChoice(serializeChoice({ remove: "bass" })), { remove: "bass" }) && serializeChoice({ remove: "bass" }).v === 1);
check("anything unknown is the whole song", ["kazoo", "other", 7, null].every((r) => parseChoice({ remove: r }).remove === "")
  && parseChoice(null).remove === "" && parseChoice("{nope").remove === "" && parseChoice('{"remove":"drums"}').remove === "drums");

// What a song offers
const all = ["vocals", "drums", "bass", "guitar", "piano", "other"];
check("a song offers the parts it has a file for", eq(offeredChoices(all), STEM_CHOICES) && eq(offeredChoices(["vocals", "bass"]), ["", "bass", "vocals"]) && eq(offeredChoices([]), [""]) && eq(offeredChoices(null), [""]));
check("a choice the song cannot offer reads as the whole song", effectiveChoice({ remove: "piano" }, ["bass"]) === "" && effectiveChoice({ remove: "bass" }, ["bass"]) === "bass");

// What is left out
check("nothing removed leaves out nothing", excludedNames("").size === 0);
check("Voice leaves out vocals and both halves of a split", sameNames(excludedNames("vocals"), new Set(["vocals", "lead_vocals", "backing_vocals"])));
check("an instrument leaves out itself", sameNames(excludedNames("bass"), new Set(["bass"])));
check("set comparison", sameNames(new Set(["a", "b"]), new Set(["b", "a"])) && !sameNames(new Set(["a"]), new Set(["a", "b"])));

// The timeline's lanes
check("lanes: the backing (every stem but the part) and the part", eq(timelineLanes("bass", all), { backing: ["vocals", "drums", "guitar", "piano", "other"], part: "bass" }));
check("lanes: nothing removed is the whole song, no part lane", eq(timelineLanes("", all), { backing: all, part: null }));
check("lanes: Voice", eq(timelineLanes("vocals", all).backing, ["drums", "bass", "guitar", "piano", "other"]) && timelineLanes("vocals", all).part === "vocals");
check("lanes: a song without stems has none", eq(timelineLanes("bass", []), { backing: [], part: null }));

// Playback leaves the part out (shared code: unchanged without an exclusion)
const raw = all.map((n) => ({ name: n, url: `/s/${n}.wav` })).concat([{ name: "original", url: "/s/original.wav" }]);
const visible = [{ name: "original", url: "/s/original.wav" }, { name: "vocals", url: "/s/vocals.wav" }, { name: "piano", url: "/s/piano.wav" }];
const plain = buildPlaybackStems(raw, visible, all);
check("without an exclusion playback is as before", eq(plain.map((s) => `${s.name}>${s.controlName}`), ["vocals>vocals", "piano>piano", "drums>original", "bass>original", "guitar>original", "other>original"]));
const noBass = buildPlaybackStems(raw, visible, all, new Set(["bass"]));
check("a part inside Original is left out of it", !noBass.some((s) => s.name === "bass") && noBass.filter((s) => s.controlName === "original").length === 3);
const noVoice = buildPlaybackStems(raw, visible, all, excludedNames("vocals"));
check("a part with a lane of its own is left out", !noVoice.some((s) => s.name === "vocals") && noVoice.some((s) => s.name === "piano"));
const partial = buildPlaybackStems([{ name: "original", url: "/o.wav" }, { name: "vocals", url: "/v.wav" }], [{ name: "original", url: "/o.wav" }], all, new Set(["bass"]));
check("a song that cannot be taken apart never plays the part back from its rendered mix", !partial.some((s) => s.name === "original"));
const partialPlain = buildPlaybackStems([{ name: "original", url: "/o.wav" }], [{ name: "original", url: "/o.wav" }], all);
check("...and without an exclusion keeps it as before", partialPlain.length === 1 && partialPlain[0].name === "original");
check("drums keep their own unpitched route when another part is removed", noBass.find((s) => s.name === "drums")?.pitched === false);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
