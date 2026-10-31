// Streamer Mode's recording and export arithmetic (static/js/streamer/recordModel.js).
import {
  RECORD_STATES, nextRecordState, isTakeOpen, startPoint, shouldAutoStop,
  outputPixels, videoBitrate, exportFileName, buildRenderRequest, isNoEncoder, BITRATE_MAX, CAMERA_CAP_BPS, rawExtension,
} from "../../static/js/streamer/recordModel.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// The recorder's states
const run = (events, from = "idle") => events.reduce((s, e) => nextRecordState(s, e), from);
check("all states known", RECORD_STATES.length === 9);
check("a take: start, started, stop, saved", run(["start", "started", "stop", "saved"]) === "saved");
check("export: saved, exporting, exported", run(["export", "done"], "saved") === "exported");
check("export again after a failed export", run(["export", "fail", "export", "done"], "saved") === "exported");
check("a cancelled export goes back to saved", run(["export", "cancel"], "saved") === "saved");
check("a new take after an export", run(["start"], "exported") === "starting");
check("a failure at any open stage", ["starting", "recording", "stopping"].every((s) => nextRecordState(s, "fail") === "failed"));
check("events that do not apply change nothing", nextRecordState("idle", "stop") === "idle" && nextRecordState("recording", "start") === "recording" && nextRecordState("exporting", "start") === "exporting" && nextRecordState("idle", "export") === "idle");
check("stop while starting", nextRecordState("starting", "stop") === "stopping");
check("take open: starting, recording, stopping only", ["starting", "recording", "stopping"].every(isTakeOpen) && !["idle", "saved", "failed", "exporting", "exported"].some(isTakeOpen));

// Where a take starts and when it stops
const range = { in: 30, out: 40 };
check("start at the range's start when the playhead is before it", startPoint(5, range) === 30);
check("start where the playhead is inside the range", startPoint(33.5, range) === 33.5);
check("start at the range's start when the playhead is at or past its end", startPoint(39.8, range) === 30 && startPoint(50, range) === 30);
check("no auto-stop with the switch off", !shouldAutoStop({ t: 45, range, duration: 200, autoStop: false }));
check("auto-stop at the out point", shouldAutoStop({ t: 39.99, range, duration: 200, autoStop: true }) && !shouldAutoStop({ t: 39.5, range, duration: 200, autoStop: true }));
check("auto-stop is held to the song's length", shouldAutoStop({ t: 119.99, range: { in: 0, out: 400 }, duration: 120, autoStop: true }));

// Output
check("16:9 sizes", eq(outputPixels("16:9", "720"), { width: 1280, height: 720 }) && eq(outputPixels("16:9", "1080"), { width: 1920, height: 1080 }));
check("9:16 sizes", eq(outputPixels("9:16", "720"), { width: 720, height: 1280 }) && eq(outputPixels("9:16", "1080"), { width: 1080, height: 1920 }));
check("a take recorded at 4K is 1080p at the same aspect", eq(outputPixels("16:9", "2160"), { width: 1920, height: 1080 }) && eq(outputPixels("9:16", "2160"), { width: 1080, height: 1920 }) && eq(outputPixels("1:1", "2160"), { width: 1080, height: 1080 }));
check("1:1 sizes", eq(outputPixels("1:1", "1080"), { width: 1080, height: 1080 }));
check("an unknown resolution is 1080p", eq(outputPixels("16:9", "480"), { width: 1920, height: 1080 }));
check("bit rate grows with size and rate, held to 2.5..14 Mbps", videoBitrate({ width: 1920, height: 1080 }, 30) === 6220800
  && videoBitrate({ width: 640, height: 360 }, 24) === 2_500_000 && videoBitrate({ width: 3840, height: 2160 }, 30) === 14_000_000);

// The file name
check("title, artist, suffix", exportFileName("Come as You Are", "Nirvana") === "Come as You Are - Nirvana (Performance).mp4");
check("no artist", exportFileName("Happy", "") === "Happy (Performance).mp4");
check("no title", exportFileName("", "Nirvana") === "Performance.mp4" && exportFileName(null, null) === "Performance.mp4");
check("forbidden characters become spaces", exportFileName('AC/DC: "Back" <in> Black?', "a|b*c") === "AC DC Back in Black - a b c (Performance).mp4");
check("path tricks do not survive", !exportFileName("../../etc/passwd", "x").includes("/") && !exportFileName("..\\..\\win", "x").includes("\\"));
check("control characters go", exportFileName("A\u0000B\u001fC", "") === "A B C (Performance).mp4");
check("no dots or spaces at the ends", exportFileName("  ..Song..  ", "") === "Song (Performance).mp4");
check("long names are cut", exportFileName("x".repeat(300), "").length <= 120 + " (Performance).mp4".length);
check("Windows device names are not a file name", exportFileName("CON", "") === "CON song (Performance).mp4");
check("another extension for the raw recording", exportFileName("Song", "A", "webm") === "Song - A (Performance).webm");

// The render request
const mix = { names: ["vocals", "drums", "trk1"], gains: [1, 0.5, 1], pitches: [0, 0, 0] };
// includeClick: an old caller's field, ignored (a take is never rendered with a click).
const body = buildRenderRequest({ mix, exclude: ["trk1"], aspect: "9:16", resolution: "720", fps: 60, range, includeClick: true });
check("the Streamer track is left out of the backing", eq(body.stems, ["vocals", "drums"]) && eq(body.gains, [1, 0.5]));
check("canvas, size, rate, fit fill, no mirror", body.canvas === "9:16" && body.size === "720" && body.fps === 60 && body.fit === "fill" && body.mirror === false);
check("the trim as span range", body.span === "range" && body.range_in === 30 && body.range_out === 40);
check("never a click, even when an old caller asks; take gain 1", body.click === false && body.take_gain === 1);
check("no pitches when nothing is transposed", !("pitches" in body));
check("a take recorded at 4K asks the server for 1080", buildRenderRequest({ mix, range, aspect: "9:16", resolution: "2160", fps: 30 }).size === "1080" && buildRenderRequest({ mix, range, resolution: "720" }).size === "720");
check("pitches when a lane is", eq(buildRenderRequest({ mix: { names: ["a", "b"], gains: [1, 1], pitches: [0, 2] }, range }).pitches, [0, 2]));
const micOnly = buildRenderRequest({ mix, mixedAudio: false, range });
check("mixed audio off: no backing lanes (the take alone)", eq(micOnly.stems, []) && eq(micOnly.gains, []));
check("no range: the take's own span", buildRenderRequest({ mix }).span === "take");
check("fx and master only when given", !("fx" in body) && buildRenderRequest({ mix, fx: "abc", master: "m", range }).fx === "abc");
check("defaults: 16:9, 1080p, 30 fps, click off", (() => {
  const b = buildRenderRequest({ mix, range });
  return b.canvas === "16:9" && b.size === "1080" && b.fps === 30 && b.click === false;
})());
check("the no-encoder refusal is recognised", isNoEncoder(422, "this ffmpeg build cannot encode H.264 video (no libx264)") && isNoEncoder(422, "cannot encode H.264 video (no AAC encoder)") && !isNoEncoder(422, "gain out of range") && !isNoEncoder(500, "libx264"));

// The recorder never asks more than the server keeps (fix pass E): every
// offered size and rate stays under 70% of 20 Mbit/s.
{
  const offered = [];
  for (const aspect of ["16:9", "9:16", "1:1"]) {
    for (const res of ["720", "1080"]) {
      for (const fps of [24, 30, 60]) offered.push([aspect, res, fps, videoBitrate(outputPixels(aspect, res), fps)]);
    }
  }
  const over = offered.filter(([, , , bps]) => bps > CAMERA_CAP_BPS * 0.7);
  check("every offered size and rate asks under the server's cap with a 30% margin", offered.length === 18 && over.length === 0 && BITRATE_MAX <= CAMERA_CAP_BPS * 0.7, JSON.stringify(over));
}
check("closed while starting: the take goes back to idle, nothing recorded", nextRecordState("starting", "abort") === "idle" && nextRecordState("recording", "abort") === "recording");

// Names cut by code points (fix pass 10): an emoji at the cut stays whole,
// and the name can always be put in a URL.
{
  const emoji = String.fromCodePoint(0x1f3b8);
  const long = "a".repeat(119) + emoji + "tail";
  const name = exportFileName(long, "");
  let encodes = true;
  try { encodeURIComponent(name); } catch { encodes = false; }
  check("a name cut at an emoji keeps it whole and encodes", encodes && name.includes(emoji) && !name.includes("tail"), name);
}
check("the raw recording is named after what was recorded", rawExtension("video/mp4") === "mp4" && rawExtension("video/webm;codecs=vp9") === "webm" && rawExtension("") === "webm" && rawExtension("video/x-matroska") === "mkv");

// I play: the render leaves the same part out of the backing.
{
  const mix = { names: ["original", "vocals"], gains: [1, 0.8], pitches: [0, 0] };
  const withPart = buildRenderRequest({ mix, remove: "bass", range: { in: 1, out: 4 } });
  const whole = buildRenderRequest({ mix, range: { in: 1, out: 4 } });
  check("the part played goes to the server as remove_stem, the whole song sends none", withPart.remove_stem === "bass" && !("remove_stem" in whole));
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
