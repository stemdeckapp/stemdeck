// Streamer's input and take lane (static/js/streamer/inputModel.js): the
// studio's input and channels, the auto-pick, the live waveform buckets and
// the take lane's state.
import {
  NONE, AUTO_FRAMES, cleanChannels, inputValue, resolveInput, autoPickStep,
  createLivePeaks, resetLivePeaks, addLivePeak, fillLiveBars, takeLaneState,
} from "../../static/js/streamer/inputModel.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const cam = { deviceId: "cam", name: "Logitech StreamCam", kind: "voice", channels: 2 };
const focus = { deviceId: "foc", name: "Focusrite USB Audio", kind: "instrument", channels: 2 };
const devices = [cam, focus];

// The input
check("channels: one or two indexes, else null", eq(cleanChannels([1]), [1]) && eq(cleanChannels([0, 1]), [0, 1]) && cleanChannels([]) === null && cleanChannels([0, 1, 2]) === null && cleanChannels(["x"]) === null && cleanChannels(null) === null);
check("the option value is trackInputs'", inputValue("foc", [0, 1]) === "foc\u00000,1" && inputValue("foc") === "foc\u00000");
const auto = resolveInput({ audioDeviceId: "", audioChannels: null }, devices, focus);
check("never picked: the Settings input (the interface), Input 1, auto", auto.deviceId === "foc" && eq(auto.channels, [0]) && auto.auto === true);
check("not the system's default microphone", resolveInput({ audioDeviceId: "" }, devices, focus).deviceId !== "cam");
const picked = resolveInput({ audioDeviceId: "foc", audioChannels: [1] }, devices, cam);
check("picked by hand: that device and channel, not auto", picked.deviceId === "foc" && eq(picked.channels, [1]) && picked.auto === false);
check("both inputs", eq(resolveInput({ audioDeviceId: "foc", audioChannels: [0, 1] }, devices, null).channels, [0, 1]));
check("a microphone is one input", eq(resolveInput({ audioDeviceId: "cam", audioChannels: [0, 1] }, devices, null).channels, [0]));
check("a channel the device lacks reads as Input 1", eq(resolveInput({ audioDeviceId: "foc", audioChannels: [5] }, devices, null).channels, [0]));
check("no input", resolveInput({ audioDeviceId: NONE }, devices, focus) === null);
const gone = resolveInput({ audioDeviceId: "unplugged", audioChannels: [1] }, devices, focus);
check("an unplugged choice falls back to the Settings input, said as missing", gone.deviceId === "foc" && gone.missing === true && gone.auto === true);
check("no devices yet: nothing to resolve", resolveInput({ audioDeviceId: "" }, [], null) === null);

// Auto-pick: input 1 silent, input 2 with a drum loop, for a second
let st = null;
let pick = -1;
const level = (ch) => (ch === 1 ? 0.3 : 0);
for (let i = 0; i < AUTO_FRAMES; i++) ({ state: st, pick } = autoPickStep(st, [0], level, 2));
check("a silent Input 1 moves to Input 2 after a second of signal there", pick === 1);
({ pick } = autoPickStep(null, [0], level, 2));
check("not at once", pick === -1);
check("a sounding input stays", autoPickStep(null, [0], () => 0.3, 2).pick === -1 && autoPickStep(null, [0], () => 0.3, 2).state === null);
check("a pair or a one-channel device is never moved", autoPickStep(null, [0, 1], level, 2).pick === -1 && autoPickStep(null, [0], level, 1).pick === -1);

// Live waveform buckets
const lp = createLivePeaks(100);
resetLivePeaks(lp, 10);
check("a new store records nothing", lp.from === -1 && lp.to === -1 && lp.peaks.every((v) => v === 0));
check("a peak lands in its stretch of the song", addLivePeak(lp, 2.0, 0.5) === 20 && lp.peaks[20] === 0.5);
addLivePeak(lp, 2.05, 0.2);
check("the loudest in a stretch is kept", lp.peaks[20] === 0.5);
addLivePeak(lp, 3.0, 0.8);
check("it grows left to right", lp.from === 20 && lp.to === 30);
check("outside the song or without one: nothing", addLivePeak(lp, 11, 1) === -1 && addLivePeak(lp, -1, 1) === -1 && addLivePeak(createLivePeaks(10), 1, 1) === -1);
const out = new Float32Array(10);
fillLiveBars(lp, out, 0, 1);
check("bars: recorded stretches only, the rest 0", out[2] === 0.5 && Math.abs(out[3] - 0.8) < 1e-6 && out[0] === 0 && out[9] === 0);
check("bars fill the caller's array (no allocation)", fillLiveBars(lp, out, 0, 1) === out);
const zoom = new Float32Array(5);
fillLiveBars(lp, zoom, 0.2, 0.3);
check("a zoomed stretch", zoom[0] === 0.5 && zoom.every((v) => v >= 0));
resetLivePeaks(lp, 10);
check("a new take starts empty", lp.peaks.every((v) => v === 0) && lp.to === -1);

// The take lane
check("recording: live", takeLaneState({ recording: true, lastTake: "t1", takePart: "drums", part: "drums" }) === "live");
check("a take with this part: saved", takeLaneState({ recording: false, lastTake: "t1", takePart: "drums", part: "drums" }) === "saved");
check("another part: empty (never the stem's wave)", takeLaneState({ recording: false, lastTake: "t1", takePart: "drums", part: "bass" }) === "empty");
check("no take: empty", takeLaneState({ recording: false, lastTake: null, takePart: null, part: "drums" }) === "empty");
check("an older take without a part counts for the whole song", takeLaneState({ recording: false, lastTake: "t0", takePart: null, part: "" }) === "saved");

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
