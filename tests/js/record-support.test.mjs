// Whether the Performance video can be recorded, and the message when it
// cannot (video/recordSupport.js). Stock Ubuntu's WebKitGTK failed silently
// (#787): no MediaRecorder, or one with no encoder until GStreamer's plugins
// are installed. Every key it returns must exist in all ten i18n tables.
//
// Run:  node tests/js/record-support.test.mjs

import {
  platformFromUserAgent,
  supportedRecorderMimes,
  videoRecordBlockKey,
} from "../../static/js/video/recordSupport.js";
import { MIME_LADDER } from "../../static/js/video/videoCodec.js";
import { readFileSync } from "node:fs";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const eq = (name, actual, expected) => check(name, actual === expected, `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);

const UBUNTU_WEBKIT = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko)";
const WIN_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36";
const MAC_SAFARI = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15";
const ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Mobile Safari/537.36";

// platformFromUserAgent
eq("Ubuntu WebKitGTK is linux", platformFromUserAgent(UBUNTU_WEBKIT), "linux");
eq("Windows Chrome is windows", platformFromUserAgent(WIN_CHROME), "windows");
eq("Safari is mac", platformFromUserAgent(MAC_SAFARI), "mac");
eq("Android is not linux", platformFromUserAgent(ANDROID), "other");
eq("no user agent is other", platformFromUserAgent(undefined), "other");

// supportedRecorderMimes
eq("no isTypeSupported is unknown (null)", supportedRecorderMimes(undefined), null);
check("nothing supported is an empty list", Array.isArray(supportedRecorderMimes(() => false)) && supportedRecorderMimes(() => false).length === 0);
const webmOnly = supportedRecorderMimes((type) => type.startsWith("video/webm"));
check("only the supported ladder types", webmOnly.length === 3 && webmOnly.every((type) => MIME_LADDER.includes(type)), JSON.stringify(webmOnly));
const throwing = supportedRecorderMimes((type) => {
  if (type.includes("mp4")) throw new Error("boom");
  return true;
});
check("a type that throws counts as unsupported", throwing.length === 3 && !throwing.some((type) => type.includes("mp4")), JSON.stringify(throwing));

// videoRecordBlockKey
const ok = { hasMediaRecorder: true, hasCanvasCapture: true, supportedMimes: ["video/webm"] };
eq("everything there: no block", videoRecordBlockKey({ ...ok, platform: "linux" }), null);
eq("unknown types do not block", videoRecordBlockKey({ ...ok, supportedMimes: null, platform: "linux" }), null);
eq("no MediaRecorder on Linux names the packages", videoRecordBlockKey({ ...ok, hasMediaRecorder: false, platform: "linux" }), "streamer.rec.linuxPackages");
eq("no MediaRecorder elsewhere says the browser cannot", videoRecordBlockKey({ ...ok, hasMediaRecorder: false, platform: "windows" }), "streamer.rec.unsupported");
eq("no canvas capture elsewhere says the browser cannot", videoRecordBlockKey({ ...ok, hasCanvasCapture: false, platform: "mac" }), "streamer.rec.unsupported");
eq("no encoder on Linux names the packages", videoRecordBlockKey({ ...ok, supportedMimes: [], platform: "linux" }), "streamer.rec.linuxPackages");
eq("no encoder elsewhere says so", videoRecordBlockKey({ ...ok, supportedMimes: [], platform: "windows" }), "streamer.rec.noEncoder");
eq("defaults: only a recorder known missing blocks", videoRecordBlockKey({ hasMediaRecorder: true }), null);

// Every key it can return is in all ten tables, and the Linux one names the packages.
const i18n = readFileSync(new URL("../../static/js/i18n.js", import.meta.url), "utf8");
for (const key of ["streamer.rec.linuxPackages", "streamer.rec.noEncoder", "streamer.rec.unsupported"]) {
  const count = i18n.split(`"${key}":`).length - 1;
  eq(`${key} is in all ten tables`, count, 10);
}
const linuxLines = i18n.split(/\r?\n/).filter((line) => line.includes('"streamer.rec.linuxPackages":'));
for (const pkg of ["gstreamer1.0-plugins-good", "gstreamer1.0-plugins-bad", "gstreamer1.0-plugins-ugly", "gstreamer1.0-libav"]) {
  check(`every Linux message names ${pkg}`, linuxLines.length === 10 && linuxLines.every((line) => line.includes(pkg)));
}

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
