// The footer speed control's arithmetic (static/js/speedParse.js).
import {
  SPEED_MIN, SPEED_MAX, FADER_MAX, SPEED_NOTCH, clampSpeed, parseSpeed, nextNotch,
} from "../../static/js/speedParse.js";
import { SPEED_MIN as KK_MIN, SPEED_MAX as KK_MAX } from "../../static/js/karaoke/karaokeModel.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}

// The range
check("the range is Karaoke Mode's", SPEED_MIN === KK_MIN && SPEED_MAX === KK_MAX, `${SPEED_MIN}..${SPEED_MAX} vs ${KK_MIN}..${KK_MAX}`);
check("the fader stops at 1x, notched every 0.05x", FADER_MAX === 1 && SPEED_NOTCH === 0.05);

// Clamping
check("clamps to 0.5..1.5", clampSpeed(0.2) === 0.5 && clampSpeed(1.8) === 1.5 && clampSpeed(0.83) === 0.83);
check("clamps to a narrower range when asked", clampSpeed(1.25, SPEED_MIN, FADER_MAX) === 1 && clampSpeed(0.3, SPEED_MIN, FADER_MAX) === 0.5);
check("rounds to a hundredth, with no float noise", clampSpeed(0.97 - 0.05) === 0.92 && clampSpeed(0.834) === 0.83 && clampSpeed(0.835 + 1e-9) === 0.84);
check("not a number stays not a number", Number.isNaN(clampSpeed(NaN)) && Number.isNaN(clampSpeed("fast")) && Number.isNaN(clampSpeed(undefined)));

// Parsing what is typed
const parses = [
  ["0.83", 0.83], ["0,83", 0.83], ["0.83x", 0.83], ["0.83X", 0.83], ["0.83×", 0.83],
  ["83", 0.83], ["83%", 0.83], [" 83 % ", 0.83], ["1", 1], ["1.25", 1.25], ["125%", 1.25],
  [".5", 0.5], ["2", 2], ["150", 1.5], ["50%", 0.5],
];
for (const [text, want] of parses) {
  const got = parseSpeed(text);
  check(`"${text}" reads as ${want}`, Math.abs(got - want) < 1e-9, `got ${got}`);
}
check("a percentage is a percentage even when small", Math.abs(parseSpeed("1%") - 0.01) < 1e-9);
for (const text of ["", "fast", "0.8.1", "-0.8", "1e2", "x", "%", "0.8xx", null, undefined]) {
  check(`"${text}" is ignored`, Number.isNaN(parseSpeed(text)), `got ${parseSpeed(text)}`);
}
check("typed, then clamped", clampSpeed(parseSpeed("20")) === 0.5 && clampSpeed(parseSpeed("1.8")) === 1.5);

// Notches
check("off a notch, up lands on the next one", nextNotch(0.83, 1) === 0.85);
check("off a notch, down lands on the one below", nextNotch(0.83, -1) === 0.8);
check("on a notch, one notch further", nextNotch(0.85, 1) === 0.9 && nextNotch(0.85, -1) === 0.8 && nextNotch(1, -1) === 0.95);
check("float noise does not skip a notch", nextNotch(0.7, 1) === 0.75 && nextNotch(0.55, -1) === 0.5 && nextNotch(0.6, 1) === 0.65);
check("above 1x, down comes back to the fader's range once clamped", clampSpeed(nextNotch(1.25, -1), SPEED_MIN, FADER_MAX) === 1);
check("not clamped by itself", nextNotch(0.5, -1) === 0.45 && clampSpeed(nextNotch(0.5, -1)) === 0.5);

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
