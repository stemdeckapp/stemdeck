// Performance's lane volumes and Monitor level as numbers (static/js/laneLevel.js),
// the Monitor level in the recording settings (streamer/outputSettings.js), and
// the take's volume in the MP4 export's render request (streamer/recordModel.js).
import {
  LANE_VOLUME_MAX, LEVEL_STEP, LEVEL_BIG_STEP, MONITOR_LEVEL_MAX, MONITOR_LEVEL_DEFAULT,
  clampLevel, levelText, levelFraction, levelAt, levelKeyStep, groupLevel, monitorLevelOf,
} from "../../static/js/laneLevel.js";
import {
  defaultRecordingSettings, parseRecordingSettings, serializeRecordingSettings, applySettings,
} from "../../static/js/streamer/outputSettings.js";
import { buildRenderRequest, TAKE_GAIN_MAX } from "../../static/js/streamer/recordModel.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const near = (a, b, e = 1e-9) => Math.abs(a - b) <= e;

// The scale is the Studio fader's.
check("the lane range is the Studio fader's (0 to 2, +6 dB)", LANE_VOLUME_MAX === 2 && levelText(2) === "+6.0");
check("the export's take gain range is the lane range", TAKE_GAIN_MAX === LANE_VOLUME_MAX);

// dB text
check("unity reads 0.0", levelText(1) === "0.0");
check("a hair under unity reads 0.0, never -0.0", levelText(0.99999) === "0.0");
check("half reads -6.0", levelText(0.5) === "-6.0");
check("silence reads -∞", levelText(0) === "-∞" && levelText(-1) === "-∞" && levelText(Number.NaN) === "-∞");
check("a boost is signed", levelText(1.5) === "+3.5");

// Clamping and defaults
check("clamped into the range", clampLevel(3) === 2 && clampLevel(-1) === 0 && clampLevel(0.7) === 0.7);
check("not a number: the fallback (0 dB)", clampLevel(Number.NaN) === 1 && clampLevel("0.5") === 1 && clampLevel(undefined) === 1);
check("a fallback past the top is clamped too", clampLevel(null, 1, 5) === 1);

// Slider position
check("0 dB sits at the middle of a lane slider", levelFraction(1) === 0.5);
check("the ends", levelFraction(0) === 0 && levelFraction(2) === 1 && levelFraction(9) === 1);
check("a fraction gives a level on the 0.01 grid", levelAt(0.5) === 1 && levelAt(0.333) === 0.67 && levelAt(-1) === 0 && levelAt(2) === 2);
check("the Monitor slider's middle is -6 dB", levelAt(0.5, MONITOR_LEVEL_MAX) === 0.5 && levelText(levelAt(0.5, 1)) === "-6.0");

// Keys
check("arrows step", near(levelKeyStep(1, "ArrowRight"), 1 + LEVEL_STEP) && near(levelKeyStep(1, "ArrowLeft"), 1 - LEVEL_STEP));
check("up and down step as right and left", levelKeyStep(1, "ArrowUp") === levelKeyStep(1, "ArrowRight") && levelKeyStep(1, "ArrowDown") === levelKeyStep(1, "ArrowLeft"));
check("Shift and the Page keys step big", near(levelKeyStep(1, "ArrowRight", { shift: true }), 1 + LEVEL_BIG_STEP) && near(levelKeyStep(1, "PageDown"), 1 - LEVEL_BIG_STEP));
check("Home is silence, End the top", levelKeyStep(1, "Home") === 0 && levelKeyStep(1, "End") === 2 && levelKeyStep(0.3, "End", { max: 1 }) === 1);
check("steps stop at the ends", levelKeyStep(2, "ArrowRight") === 2 && levelKeyStep(0, "ArrowLeft") === 0 && levelKeyStep(1, "PageUp", { max: 1 }) === 1);
check("steps land on the grid (no float drift)", levelKeyStep(0.07, "ArrowRight") === 0.11);
check("other keys are not the slider's", levelKeyStep(1, "Enter") === null && levelKeyStep(1, " ") === null && levelKeyStep(1, "a") === null);

// The Backing track's one level
check("lanes that agree give their level", groupLevel([0.5, 0.5, 0.5]) === 0.5);
check("lanes that do not give the loudest", groupLevel([0.5, 1.2, 0.8]) === 1.2);
check("no lanes: 0 dB", groupLevel([]) === 1 && groupLevel(null) === 1);
check("a lane without a number is left out", groupLevel([undefined, 0.25, Number.NaN]) === 0.25);
check("clamped to the range", groupLevel([7]) === 2);

// The Monitor level: never louder than Hear myself always played, never loud by default.
check("it tops out at unity and starts there", MONITOR_LEVEL_MAX === 1 && MONITOR_LEVEL_DEFAULT === 1);
check("a stored level past the cap is the cap", monitorLevelOf(4) === 1 && monitorLevelOf(1.0001) === 1);
check("a wrong stored level is the default", monitorLevelOf("loud") === MONITOR_LEVEL_DEFAULT && monitorLevelOf(Number.POSITIVE_INFINITY) === MONITOR_LEVEL_DEFAULT && monitorLevelOf(undefined) === MONITOR_LEVEL_DEFAULT);
check("a quiet level is kept", monitorLevelOf(0.25) === 0.25 && monitorLevelOf(0) === 0);

// The Monitor level in the recording settings
{
  const d = defaultRecordingSettings();
  check("the default settings carry the default Monitor level", d.monitorLevel === MONITOR_LEVEL_DEFAULT);
  check("an old record without one reads the default", parseRecordingSettings({ fps: 24 }).monitorLevel === MONITOR_LEVEL_DEFAULT);
  check("a stored level past the cap reads the cap", parseRecordingSettings({ monitorLevel: 3 }).monitorLevel === 1);
  const set = applySettings(d, { monitorLevel: 0.4 });
  check("a set level survives a round trip", parseRecordingSettings(JSON.stringify(serializeRecordingSettings(set))).monitorLevel === 0.4);
  check("Hear myself is still never a setting", !("hear" in set) && !("monitor" in parseRecordingSettings({ monitor: true, monitorLevel: 0.5 })));
}

// The take's volume in the render request
{
  const mix = { names: ["drums", "bass", "trk1"], gains: [0.5, 0.5, 1], pitches: [0, 0, 0] };
  const body = buildRenderRequest({ mix, exclude: ["trk1"], takeGain: 0.5 });
  check("the backing lanes carry their volumes", JSON.stringify(body.stems) === JSON.stringify(["drums", "bass"]) && JSON.stringify(body.gains) === JSON.stringify([0.5, 0.5]));
  check("the take carries its lane volume", body.take_gain === 0.5);
  check("no take volume given: unity", buildRenderRequest({ mix, exclude: ["trk1"] }).take_gain === 1);
  check("the take volume is clamped to the lane range", buildRenderRequest({ mix, takeGain: 9 }).take_gain === 2 && buildRenderRequest({ mix, takeGain: -1 }).take_gain === 0);
  check("not a number: unity", buildRenderRequest({ mix, takeGain: Number.NaN }).take_gain === 1 && buildRenderRequest({ mix, takeGain: "0.5" }).take_gain === 1);
  check("the take alone (no mixed audio) is exported as recorded", buildRenderRequest({ mix, mixedAudio: false, takeGain: 0.25 }).take_gain === 1);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
