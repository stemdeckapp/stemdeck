// Performance's Export Performance menu: its state (recordModel.exportMenuState),
// the DaVinci Resolve request (recordModel.buildPackageRequest), the file
// names, and that every new string and Guide row is there in all ten
// languages (static/js/i18n.js, static/js/helpMode.js).
//
// Run:  node tests/js/performance-export.test.mjs

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { TRANSLATIONS } from "../../static/js/i18n.js";
import {
  exportMenuState, buildPackageRequest, buildRenderRequest, exportFileName, exportFormatOf, EXPORT_FORMATS, EXPORT_EXT,
} from "../../static/js/streamer/recordModel.js";

let passed = 0;
let failed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ── The menu's state ──
{
  const mp4 = exportMenuState({ format: "mp4", hasTake: true });
  check("MP4: Export MP4, its line, no options, nothing in the way",
    mp4.runKey === "streamer.export.go" && mp4.descKey === "streamer.export.mp4Desc" && !mp4.options && !mp4.runOff && eq(mp4.reasons, []));
  const res = exportMenuState({ format: "resolve", hasTake: true });
  check("Resolve: its own title, its line and the options",
    res.runKey === "streamer.export.resolveTitle" && res.descKey === "streamer.export.resolveDesc" && res.options);
  check("an unknown format is MP4", exportMenuState({ format: "mov", hasTake: true }).format === "mp4");
  check("no take: the action is off", exportMenuState({ format: "mp4" }).runOff);
  const blocked = exportMenuState({ format: "resolve", hasTake: true, transposeBlocked: true });
  check("a transposed mix this ffmpeg cannot export: off, with the reason",
    blocked.runOff && eq(blocked.reasons, ["export.transposeUnavailable"]));
  const noPicture = exportMenuState({ format: "mp4", hasTake: true, hasCamera: false });
  check("a take without a picture: off, with the reason",
    noPicture.runOff && eq(noPicture.reasons, ["streamer.export.noCamera"]));
  check("a camera not known yet does not block (the server says)", !exportMenuState({ format: "mp4", hasTake: true, hasCamera: null }).runOff);
  check("no beat grid: the click option is off", exportMenuState({ format: "resolve", hasTake: true, hasGrid: false }).clickOff);
  const failedMp4 = { code: "failed", kind: "mp4" };
  check("Try again after this format failed",
    exportMenuState({ format: "mp4", hasTake: true, error: failedMp4 }).runKey === "streamer.export.retry");
  check("the other format keeps its own title",
    exportMenuState({ format: "resolve", hasTake: true, error: failedMp4 }).runKey === "streamer.export.resolveTitle");
  check("no Try again when it would fail the same way (no encoder, no picture)",
    ["no-encoder", "no-camera"].every((code) => exportMenuState({ format: "mp4", hasTake: true, error: { code, kind: "mp4" } }).runKey === "streamer.export.go"));
}

// ── The package request ──
{
  const mix = { names: ["vocals", "k00000001", "drums"], gains: [1, 0.8, 0.5], pitches: [0, 0, 2], lanes: ["vocals", "drums"] };
  const range = { in: 1.25, out: 9.5 };
  const body = buildPackageRequest({
    mix, exclude: ["k00000001"], aspect: "9:16", resolution: "720", fps: 24, range, fx: "eyJ", remove: "bass",
    takeGain: 0.7, backing: "stems", click: true, clickSound: "wood", name: "Song - A (Performance)",
  });
  check("the backing as heard, the take's own track left out",
    eq(body.stems, ["vocals", "drums"]) && eq(body.gains, [1, 0.5]) && eq(body.pitches, [0, 2]));
  check("the take's gain, the canvas, the rate, the trim",
    body.take_gain === 0.7 && body.canvas === "9:16" && body.size === "720" && body.fps === 24
      && body.span === "range" && body.range_in === 1.25 && body.range_out === 9.5);
  check("the EQ, the part played, the mode's lanes", body.fx === "eyJ" && body.remove_stem === "bass" && eq(body.lanes, ["vocals", "drums"]));
  check("never a master", !("master" in body));
  check("stems, the click with its sound, the timeline's name",
    body.backing === "stems" && body.click === true && body.click_sound === "wood" && body.name === "Song - A (Performance)");
  const plain = buildPackageRequest({ mix, range });
  check("defaults: one backing track, no click, no name, no click sound",
    plain.backing === "mix" && plain.click === false && !("name" in plain) && !("click_sound" in plain));
  check("an unknown backing is one track", buildPackageRequest({ mix, range, backing: "all" }).backing === "mix");
  // With "Record mixed audio" off the MP4 is the take alone; the package
  // keeps the backing (it is for editing, the take is on its own track).
  const mp4Alone = buildRenderRequest({ mix, range, mixedAudio: false });
  check("the MP4 without the mixed audio has no backing, the package has it",
    eq(mp4Alone.stems, []) && buildPackageRequest({ mix, range }).stems.length === 3);
  const long = buildPackageRequest({ mix, range, name: "x".repeat(300) });
  check("a long name is cut to 200 characters", long.name.length === 200);
}

// ── Names and the stored choice ──
check("the package file is .otioz", exportFileName("Song", "Artist", EXPORT_EXT.resolve) === "Song - Artist (Performance).otioz");
check("the video file is .mp4", exportFileName("Song", "", EXPORT_EXT.mp4) === "Song (Performance).mp4");
check("two formats", eq(EXPORT_FORMATS, ["mp4", "resolve"]));
check("a stored format is one of them, else MP4",
  exportFormatOf("resolve") === "resolve" && exportFormatOf("mp4") === "mp4" && exportFormatOf(null) === "mp4" && exportFormatOf("<x>") === "mp4");

// ── Strings in every language, and the Guide ──
const LANGS = ["en", "pl", "ja", "zh-Hans", "de", "fr", "pt", "id", "es", "ko"];
const KEYS = [
  "streamer.export.menu", "streamer.export.mp4Desc", "streamer.export.resolveTitle", "streamer.export.resolveDesc",
  "streamer.export.stems", "streamer.export.packaging", "streamer.export.large", "streamer.export.noCamera",
  "help.smExport", "help.smExportFormat", "help.smExportRun", "help.smExportStems", "help.smExportClick",
];
for (const lang of LANGS) {
  const table = TRANSLATIONS[lang];
  const missing = KEYS.filter((k) => typeof table?.[k] !== "string" || !table[k].trim());
  check(`${lang}: every Export Performance string`, missing.length === 0, missing.join(", "));
  const packaging = table?.["streamer.export.packaging"] || "";
  check(`${lang}: the progress keeps its {pct}`, packaging.includes("{pct}"));
}
check("English names the version floor", TRANSLATIONS.en["streamer.export.resolveDesc"].includes("18.5"));

const help = readFileSync(fileURLToPath(new URL("../../static/js/helpMode.js", import.meta.url)), "utf8");
for (const [sel, key] of [
  ["#streamer .sm-export-fmts", "help.smExportFormat"],
  ['#streamer .sm-export-opt[data-opt="stems"]', "help.smExportStems"],
  ['#streamer .sm-export-opt[data-opt="click"]', "help.smExportClick"],
  ["#streamer .sm-export-run", "help.smExportRun"],
  ["#streamer .sm-export-go", "help.smExport"],
]) {
  check(`the Guide explains ${sel}`, help.includes(`${sel}", "${key}"`) || help.includes(`${sel}', "${key}"`));
}
check("in the Guide, Export Performance still opens its menu", /const PASS = "[^"]*#streamer \.sm-export-go/.test(help));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
