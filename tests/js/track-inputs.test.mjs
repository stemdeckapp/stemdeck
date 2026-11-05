// Input choices for recording tracks (static/js/capture/trackInputs.js).
//
// A track records one channel of a device or an adjacent stereo pair, and
// tracks armed together must share a device. A device is a voice input (a
// microphone: one input) or an instrument input (an interface: each channel
// and each pair), named the way the user knows it. These are the pure rules
// behind the input pickers and the arm button. The device labels are the
// ones Chromium and Edge report on Windows for a Logitech StreamCam and a
// Yamaha MG12XU mixer.
//
// Run:  node tests/js/track-inputs.test.mjs

import {
  channelChoices, defaultChannels, sameDevice, channelsNeeded, friendlyName, inputKind, choicesFor,
  normaliseDevices, findDevice, resolveInput, pickDefaultDevice, needsNameUnlock,
} from "../../static/js/capture/trackInputs.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};
const keys = (max) => channelChoices(max).map((c) => c.key).join(",");

check("mono device: one choice", keys(1) === "1", keys(1));
check("stereo device: 1, 2, 1+2", keys(2) === "1,2,1+2", keys(2));
check("4 inputs: each, then 1+2 and 3+4", keys(4) === "1,2,3,4,1+2,3+4", keys(4));
check("odd count: no pair for the last", keys(3) === "1,2,3,1+2", keys(3));
check("pairs are 0-based channel indexes", JSON.stringify(channelChoices(4).at(-1).channels) === "[2,3]");
check("junk counts as one channel", keys(0) === "1" && keys(NaN) === "1");
check("capped at 64 channels", channelChoices(500).filter((c) => c.channels.length === 1).length === 64);
check("default on a stereo count is its first input", JSON.stringify(defaultChannels(2)) === "[0]");
check("default on mono is channel 1", JSON.stringify(defaultChannels(1)) === "[0]");
check("same device by id", sameDevice({ device_id: "a" }, { device_id: "a" }));
check("different devices", !sameDevice({ device_id: "a" }, { device_id: "b" }));
check("null and empty are both the default input", sameDevice(null, { device_id: "" }));
check("channels needed covers the highest channel", channelsNeeded([{ channels: [0] }, { channels: [2, 3] }]) === 4);
check("channels needed is at least 1", channelsNeeded([]) === 1);

// ── Names and kinds, from real Windows labels ──
const caps = (max) => () => ({ channelCount: { min: 1, max } });
const dev = (deviceId, label, groupId, max = 2) => ({ kind: "audioinput", deviceId, label, groupId, getCapabilities: caps(max) });
const EDGE = [
  dev("default", "Default - Microphone (Logitech StreamCam) (046d:0893)", "g-cam"),
  dev("communications", "Communications - Microphone (Logitech StreamCam) (046d:0893)", "g-cam"),
  dev("id-mgxu", "Line (2- MG-XU) (0499:1703)", "g-mg"),
  dev("id-cam", "Microphone (Logitech StreamCam) (046d:0893)", "g-cam"),
];
// Playwright's Chromium has no "Default" string: the alias reads " - Microphone (...)".
const CHROMIUM = [dev("default", " - Microphone (Logitech StreamCam) (046d:0893)", "g-cam"), ...EDGE.slice(1)];

check("microphone name", friendlyName("Microphone (Logitech StreamCam) (046d:0893)") === "Logitech StreamCam", friendlyName("Microphone (Logitech StreamCam) (046d:0893)"));
check("mixer name: no 'Line', no instance number, no USB id", friendlyName("Line (2- MG-XU) (0499:1703)") === "MG-XU", friendlyName("Line (2- MG-XU) (0499:1703)"));
check("alias prefix stripped", friendlyName("Default - Microphone (Logitech StreamCam) (046d:0893)") === "Logitech StreamCam");
check("Chromium's empty alias prefix stripped", friendlyName(" - Microphone (Logitech StreamCam) (046d:0893)") === "Logitech StreamCam");
check("a name without a type word is kept", friendlyName("Scarlett 2i2 USB") === "Scarlett 2i2 USB");
check("macOS microphone name kept", friendlyName("MacBook Pro Microphone") === "MacBook Pro Microphone");
check("a bracket that is not a type word is kept", friendlyName("Analogue 1 + 2 (Focusrite USB Audio)") === "Analogue 1 + 2 (Focusrite USB Audio)");
check("microphone is voice", inputKind("Microphone (Logitech StreamCam) (046d:0893)") === "voice");
check("headset is voice", inputKind("Headset Microphone (Jabra)") === "voice");
check("mixer line input is instrument", inputKind("Line (2- MG-XU) (0499:1703)") === "instrument");
check("interface is instrument", inputKind("Analogue 1 + 2 (Focusrite USB Audio)") === "instrument");
check("Chromium's fake device is instrument (stereo, so 1, 2, 1+2)", inputKind("Fake Default Audio Input") === "instrument");

for (const [browser, list] of [["Edge", EDGE], ["Chromium", CHROMIUM]]) {
  const n = normaliseDevices(list);
  check(`${browser}: aliases dropped, two devices`, n.length === 2, JSON.stringify(n.map((d) => d.deviceId)));
  check(`${browser}: the mixer is an instrument named MG-XU`, n.some((d) => d.deviceId === "id-mgxu" && d.name === "MG-XU" && d.kind === "instrument" && d.channels === 2));
  check(`${browser}: the webcam mic is voice`, n.some((d) => d.deviceId === "id-cam" && d.name === "Logitech StreamCam" && d.kind === "voice"));
  check(`${browser}: the system default is marked`, n.find((d) => d.systemDefault)?.deviceId === "id-cam");
}
const twoEndpoints = normaliseDevices([
  dev("a", "Analogue 1 + 2 (Focusrite USB Audio)", "f"),
  dev("b", "Analogue 3 + 4 (Focusrite USB Audio)", "f"),
]);
check("two endpoints of one interface keep distinct names", new Set(twoEndpoints.map((d) => d.name)).size === 2, JSON.stringify(twoEndpoints.map((d) => d.name)));

const devices = normaliseDevices(EDGE);
const mg = devices.find((d) => d.kind === "instrument");
const cam = devices.find((d) => d.kind === "voice");
check("a microphone offers one input", JSON.stringify(choicesFor(cam).map((c) => c.channels)) === "[[0]]");
check("the mixer offers 1, 2 and 1+2", choicesFor(mg).map((c) => c.key).join(",") === "1,2,1+2");
check("a new microphone track records its one input", JSON.stringify(defaultChannels(cam)) === "[0]");
check("a new interface track records one input, not a half-silent pair", JSON.stringify(defaultChannels(mg)) === "[0]");
check("Settings default: remembered first", pickDefaultDevice(devices, "id-mgxu") === mg);
check("Settings default: else the system default", pickDefaultDevice(devices, "") === cam);
check("Settings default: a forgotten id falls to the system default", pickDefaultDevice(devices, "gone") === cam);

check("found by id", findDevice({ device_id: "id-mgxu" }, devices) === mg);
check("found by name when the id changed (another browser)", findDevice({ device_id: "other-browser-id", device_label: "MG-XU" }, devices) === mg);
check("found by a stored raw label", findDevice({ device_id: "x", device_label: "Line (2- MG-XU) (0499:1703)" }, devices) === mg);
check("unplugged: not found", findDevice({ device_id: "x", device_label: "Scarlett 2i2" }, devices) === null);

const r1 = resolveInput({ device_id: "other-browser-id", device_label: "MG-XU", channels: [0] }, devices, "");
check("resolve: a moved id records from the device found by name", r1.device_id === "id-mgxu" && !r1.missing && JSON.stringify(r1.channels) === "[0]");
const r2 = resolveInput({ device_id: "gone", device_label: "Scarlett 2i2", channels: [1] }, devices, "");
check("resolve: an unplugged device is missing, its id kept", r2.missing && r2.device_id === "gone" && r2.device_label === "Scarlett 2i2");
const r3 = resolveInput({ device_id: "id-cam", device_label: "Logitech StreamCam", channels: [0, 1] }, devices, "");
check("resolve: a microphone records one channel even if a pair was stored", JSON.stringify(r3.channels) === "[0]" && r3.kind === "voice");
const r4 = resolveInput(null, devices, "id-mgxu");
check("resolve: no input of its own is the Settings device, on one input", r4.device_id === "id-mgxu" && JSON.stringify(r4.channels) === "[0]");
const r5 = resolveInput({ device_id: "gone", channels: [0] }, [], "");
check("resolve: no device list yet is not reported missing", !r5.missing);

// The desktop app's page starts every load with nameless devices (its grant
// is never saved), as Chromium reports them before any getUserMedia.
const nameless = [
  { kind: "audioinput", deviceId: "", label: "" },
  { kind: "videoinput", deviceId: "", label: "" },
  { kind: "audiooutput", deviceId: "", label: "" },
];
const named = [{ kind: "audioinput", deviceId: "id-mgxu", label: "Line (2- MG-XU) (0499:1703)" }];
check("unlock: nameless inputs in the desktop app open the mic once", needsNameUnlock(nameless, { desktop: true }));
check("unlock: never in a browser, which would prompt", !needsNameUnlock(nameless, { desktop: false }));
check("unlock: not twice in one page", !needsNameUnlock(nameless, { desktop: true, tried: true }));
check("unlock: not when the names are already there", !needsNameUnlock(named, { desktop: true }));
check(
  "unlock: not with no input at all",
  !needsNameUnlock([{ kind: "videoinput", deviceId: "", label: "" }], { desktop: true }) && !needsNameUnlock(null, { desktop: true }),
);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
