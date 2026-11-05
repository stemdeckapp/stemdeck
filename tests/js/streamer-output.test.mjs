// Streamer Mode's recording settings and input meter (static/js/streamer/outputSettings.js).
import {
  SETTINGS_KEY, NONE, RESOLUTIONS, FRAME_RATES,
  defaultRecordingSettings, parseRecordingSettings, serializeRecordingSettings, resolveDeviceId,
  isHeavy, cameraConstraints, applySettings, resolutionOf,
  METER_SEGMENTS, METER_FLOOR_DB, METER_HOLD_MS, METER_DECAY_DB_PER_SEC,
  meterFraction, levelDb, segmentZone, litSegments, holdPeak, newPeakState,
} from "../../static/js/streamer/outputSettings.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const near = (a, b, e = 1e-9) => Math.abs(a - b) < e;

// Defaults and serialization
const d = defaultRecordingSettings();
check("defaults: default devices, 1080p, 30 fps, mixed audio on, auto-stop on", eq(d, {
  videoDeviceId: "", audioDeviceId: "", audioChannels: null, resolution: "1080", fps: 30, recordMixedAudio: true, autoStopAtEnd: true,
}));
check("the settings key", SETTINGS_KEY === "stemdeck:streamer:recording");
check("choices: 720p and 1080p only, no 4K", eq(RESOLUTIONS, ["720", "1080"]) && eq(FRAME_RATES, [24, 30, 60]));
check("garbage gives the defaults", eq(parseRecordingSettings(null), d) && eq(parseRecordingSettings("{nope"), d) && eq(parseRecordingSettings(7), d));
const mine = { videoDeviceId: "cam1", audioDeviceId: NONE, audioChannels: [1], resolution: "720", fps: 24, recordMixedAudio: false, autoStopAtEnd: false };
check("round trip", eq(parseRecordingSettings(JSON.stringify(serializeRecordingSettings(mine))), mine));
check("a wrong value takes its default, the rest is kept", eq(parseRecordingSettings({ ...mine, resolution: "8K", fps: 25 }), { ...mine, resolution: "1080", fps: 30 }));
// Settings saved before the click switch was removed: includeClick (on, off
// or garbage) loads without an error and is dropped; the rest is kept.
for (const old of [true, false, "yes"]) {
  const saved = JSON.stringify({ ...mine, includeClick: old });
  let got = null;
  try { got = parseRecordingSettings(saved); } catch { got = null; }
  check(`old saved includeClick=${JSON.stringify(old)} is ignored`, got !== null && eq(got, mine) && !("includeClick" in got));
}
check("an old includeClick is not written back", !("includeClick" in serializeRecordingSettings({ ...mine, includeClick: true })));
check("numbers as strings are read", parseRecordingSettings({ resolution: 720, fps: "24" }).resolution === "720" && parseRecordingSettings({ fps: "24" }).fps === 24);
check("serialize keeps only the fields", eq(Object.keys(serializeRecordingSettings({ ...d, extra: 1 })).sort(), Object.keys(d).sort()));

// A saved device that is gone
const devices = [{ deviceId: "cam1" }, { deviceId: "cam2" }];
check("a present device is used", resolveDeviceId("cam2", devices) === "cam2");
check("a device that has gone falls back to the default", resolveDeviceId("gone", devices) === "");
check("none stays none, default stays default", resolveDeviceId(NONE, devices) === NONE && resolveDeviceId("", devices) === "");
check("no list yet (no permission): the saved one is tried", resolveDeviceId("cam9", []) === "cam9" && resolveDeviceId("cam9", [{ deviceId: "" }]) === "cam9");

// Heavy warning
check("60 fps is heavy", isHeavy({ resolution: "720", fps: 60 }));
check("1080p30 and 720p24 are not", !isHeavy(d) && !isHeavy({ resolution: "720", fps: 24 }));

// Camera constraints
check("1080p30 asks for 1920 x 1080 at 30, ideal", eq(cameraConstraints(d), { width: { ideal: 1920 }, height: { ideal: 1080 }, frameRate: { ideal: 30 } }));
check("720p60", eq(cameraConstraints({ resolution: "720", fps: 60 }), { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 60 } }));
check("a 4K setting asks 1080p", eq(cameraConstraints({ resolution: "2160", fps: 24 }), { width: { ideal: 1920 }, height: { ideal: 1080 }, frameRate: { ideal: 24 } }));
check("a chosen camera is asked for exactly", eq(cameraConstraints(d, "cam1").deviceId, { exact: "cam1" }));
check("the default or none asks for no device", !("deviceId" in cameraConstraints(d, "")) && !("deviceId" in cameraConstraints(d, NONE)));
check("unknown settings ask 1080p30", eq(cameraConstraints({ resolution: "x", fps: 7 }), cameraConstraints(d)));

// Meter
check("36 segments", METER_SEGMENTS === 36);
const zones = Array.from({ length: METER_SEGMENTS }, (_, i) => segmentZone(i));
check("green up to 70%, yellow to 86%, red above", zones.filter((z) => z === "green").length === 25 && zones.filter((z) => z === "yellow").length === 5 && zones.filter((z) => z === "red").length === 6);
check("zones run green, yellow, red in order", zones.join(",").replace(/(green,)+/, "").startsWith("yellow") && zones.at(-1) === "red" && zones[0] === "green");
check("dB scale: silence is empty, 0 dBFS full, -30 half", meterFraction(0) === 0 && near(meterFraction(1), 1) && near(meterFraction(10 ** (-30 / 20)), 0.5) && meterFraction(-1) === 0 && meterFraction(2) === 1);
check("readout in whole dB, -60 to 0", levelDb(0) === METER_FLOOR_DB && levelDb(1) === 0 && levelDb(10 ** (-12 / 20)) === -12 && levelDb(1e-9) === -60 && levelDb(4) === 0);
check("lit segments", litSegments(0) === 0 && litSegments(1) === 36 && litSegments(0.5) === 18 && litSegments(2) === 36 && litSegments(-1) === 0);
const st = newPeakState();
holdPeak(st, 0.8, 1000);
check("the peak rises at once", near(st.value, 0.8));
holdPeak(st, 0.1, 1000 + METER_HOLD_MS - 10);
check("and holds", near(st.value, 0.8));
holdPeak(st, 0.1, 1000 + METER_HOLD_MS + 500);
check("then falls by the decay rate", near(st.value, 0.8 * 10 ** (-METER_DECAY_DB_PER_SEC * 0.5 / 20), 1e-6), String(st.value));
holdPeak(st, 0, 1000 + METER_HOLD_MS + 60000);
check("and reaches silence", st.value < 1e-6);
const same = newPeakState();
check("updated in place (no allocation per frame)", holdPeak(same, 0.5, 1) === same);

// 4K is no longer offered (0.21.0): settings saved at 4K read as 1080p,
// every other field kept, without an error.
{
  const old4k = { ...mine, resolution: "2160", fps: 30 };
  let threw = false;
  let got = null;
  try { got = parseRecordingSettings(JSON.stringify(old4k)); } catch { threw = true; }
  check("saved settings at 4K load as 1080p, the rest kept, no error", !threw && eq(got, { ...old4k, resolution: "1080" }));
  check("a saved 4K at 60 reads as 1080p at 60", eq(parseRecordingSettings({ resolution: "2160", fps: 60 }), { ...d, resolution: "1080", fps: 60 }));
  check("resolutionOf: 2160 and unknown are 1080", resolutionOf("2160") === "1080" && resolutionOf(2160) === "1080" && resolutionOf("720") === "720" && resolutionOf(undefined) === "1080");
}
check("applySettings applies a patch, kept to what is offered", eq(applySettings(d, { fps: 60 }), { ...d, fps: 60 }) && applySettings(d, { resolution: "2160" }).resolution === "1080");
check("1080p at 60 and 720p at 60 still warn, 1080p30 does not", isHeavy({ resolution: "1080", fps: 60 }) && isHeavy({ resolution: "720", fps: 60 }) && !isHeavy({ resolution: "1080", fps: 30 }));

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
