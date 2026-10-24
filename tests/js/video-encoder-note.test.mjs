// The take render's encoder as the UI words it (static/js/videoEncoderNote.js):
// the "Encoded with" note after a Streamer export, the Settings select's
// choices, the table cells and the Copy text for bug reports.
import {
  encoderFamily, encoderName, encoderNote, encoderOptionLabel, workingEncoders, encoderChoices,
  cellText, reasonText, encoderTableText,
} from "../../static/js/videoEncoderNote.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// A t() that shows the key and its vars, so the test sees which sentence was picked.
const t = (key, vars) => (vars ? `${key}${JSON.stringify(vars)}` : key);

// Families and names
check("every encoder has its vendor", encoderFamily("h264_nvenc") === "nvidia" && encoderFamily("h264_qsv") === "intel"
  && encoderFamily("h264_amf") === "amd" && encoderFamily("h264_vaapi") === "vaapi"
  && encoderFamily("h264_videotoolbox") === "apple" && encoderFamily("libx264") === "cpu");
check("an unknown encoder has no family", encoderFamily("h264_mf") === "" && encoderFamily(undefined) === "");
check("names come from i18n", encoderName("h264_nvenc", t) === "encoder.name.nvidia" && encoderName("libx264", t) === "encoder.name.cpu");
check("an unknown name is shown as it is", encoderName("h264_new", t) === "h264_new");

// The note after an export
check("GPU: Encoded with NVIDIA GPU", encoderNote({ encoder: "h264_nvenc", fallback: false }, t)
  === 'encoder.note.used{"name":"encoder.name.nvidia"}');
check("CPU: Encoded with CPU", encoderNote({ encoder: "libx264", fallback: false }, t)
  === 'encoder.note.used{"name":"encoder.name.cpu"}');
check("fallback: the GPU failed sentence, not Encoded with",
  encoderNote({ encoder: "libx264", fallback: true }, t) === "encoder.note.fallback");
check("Apple and Intel", encoderNote({ encoder: "h264_videotoolbox" }, t).includes("encoder.name.apple")
  && encoderNote({ encoder: "h264_qsv" }, t).includes("encoder.name.intel"));
check("no encoder said (an older server): no note", encoderNote({ saved: true }, t) === ""
  && encoderNote(null, t) === "" && encoderNote({ encoder: null, fallback: true }, t) === "");

// The select
const rows = [
  { encoder: "h264_nvenc", disabled: false, results: { 1080: { ok: true, speed: 2.91 }, 2160: { ok: true, speed: 1.26 } } },
  { encoder: "h264_qsv", disabled: false, results: { 1080: { ok: false, reason: "failed", detail: "Error creating a MFX session: -9." }, 2160: { ok: false, reason: "failed" } } },
  { encoder: "h264_amf", disabled: true, results: { 1080: { ok: true, speed: 3 } } },
  { encoder: "libx264", disabled: false, results: { 1080: { ok: true, speed: 3.12 }, 2160: { ok: false, reason: "budget" } } },
];
check("working: passed somewhere, not disabled, not the CPU", eq(workingEncoders(rows), ["h264_nvenc"]));
check("choices: Auto, CPU only, the working ones", eq(encoderChoices(rows, "auto"), ["auto", "cpu", "h264_nvenc"]));
check("the stored choice stays visible", eq(encoderChoices(rows, "h264_qsv"), ["auto", "cpu", "h264_nvenc", "h264_qsv"]));
check("nothing tested yet", eq(encoderChoices([], "cpu"), ["auto", "cpu"]) && eq(encoderChoices(undefined, ""), ["auto", "cpu"]));
check("option label", encoderOptionLabel("h264_nvenc", t) === 'encoder.option{"name":"encoder.name.nvidia","encoder":"h264_nvenc"}');

// The table
check("a pass shows its speed", cellText({ ok: true, speed: 2.91 }, t) === 'settings.export.encoder.works{"speed":"2.9"}');
check("a fail is No", cellText({ ok: false, reason: "failed" }, t) === "settings.export.encoder.fails");
check("out of time and missing are Not tested", cellText({ ok: false, reason: "budget" }, t) === "settings.export.encoder.untested"
  && cellText(undefined, t) === "settings.export.encoder.untested");
check("reason with ffmpeg's line", reasonText(rows[1], t) === "encoder.reason.failed: Error creating a MFX session: -9.");
check("a disabled encoder says so", reasonText(rows[2], t) === "encoder.reason.disabled");
check("a working encoder has no reason", reasonText(rows[0], t) === "");
check("an unknown reason code reads as failed", reasonText({ results: { 1080: { ok: false, reason: "weird" } } }, t) === "encoder.reason.failed");

// The Copy text
const text = encoderTableText({
  ffmpeg: "ffmpeg version n8.1.2", os: "windows", platform: "win32", setting: "auto", effective: "auto", source: "setting", results: rows,
});
const lines = text.split("\n");
check("copy: header with ffmpeg and OS", lines[0] === "StemDeck video encoder test" && lines[1] === "ffmpeg: ffmpeg version n8.1.2" && lines[2] === "os: windows (win32)");
check("copy: one line per encoder", lines.filter((l) => /^(h264_|libx264)/.test(l)).length === 4);
check("copy: speed and failure codes", /^h264_nvenc\s+ok 2\.91x\s+ok 1\.26x$/.test(lines.find((l) => l.startsWith("h264_nvenc")))
  && lines.find((l) => l.startsWith("h264_qsv")).endsWith("failed: Error creating a MFX session: -9.")
  && lines.find((l) => l.startsWith("h264_amf")).endsWith("disabled after failures"));
check("copy: a budget stop is not a failure reason", /^libx264\s+ok 3\.12x\s+no \(budget\)$/.test(lines.find((l) => l.startsWith("libx264"))));
check("copy: empty data does not throw", encoderTableText(null).startsWith("StemDeck video encoder test"));
check("no em dash anywhere", !text.includes(String.fromCharCode(0x2014)));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
