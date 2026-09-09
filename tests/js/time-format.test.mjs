// The time formatters and the loop-time parser in static/js/utils.js.
//
// Nothing imported this module before. It is small, but everything the
// transport, ruler and loop inputs put on screen goes through it, and two of
// the four functions carry comments about specific rounding bugs -- which is
// exactly the kind of thing that comes back once nothing is asserting it.
//
// parseTimecode is the one with teeth: it reads what the user typed into the
// exact-loop fields, and anything it accepts wrongly becomes a loop point in
// the wrong place rather than a visible error.
//
// Run:  node tests/js/time-format.test.mjs

import { fmtTickLabel, fmtTime, fmtTimeMs, parseTimecode } from "../../static/js/utils.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const eq = (name, actual, expected) =>
  check(name, Object.is(actual, expected), `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);

// ── fmtTime: the transport clock, always mm:ss ──────────────────────────────
{
  eq("zero is zero-padded", fmtTime(0), "00:00");
  eq("seconds below a minute", fmtTime(9), "00:09");
  eq("a whole minute", fmtTime(60), "01:00");
  eq("minutes and seconds", fmtTime(125), "02:05");
  eq("a long track keeps counting past an hour", fmtTime(3600), "60:00");
  eq("a fractional second truncates rather than rounds up", fmtTime(59.9), "00:59");

  // A track whose duration is not known yet reads as a clock, not as NaN.
  eq("an unknown duration reads as zero", fmtTime(NaN), "00:00");
  eq("an infinite duration reads as zero", fmtTime(Infinity), "00:00");
  eq("a negative time reads as zero", fmtTime(-5), "00:00");
  eq("a missing value reads as zero", fmtTime(undefined), "00:00");
}

// ── fmtTickLabel: the ruler, no leading zero on the minutes ─────────────────
{
  eq("under a minute", fmtTickLabel(30), "0:30");
  eq("on the minute", fmtTickLabel(60), "1:00");
  eq("double-digit minutes", fmtTickLabel(750), "12:30");
  eq("seconds stay padded", fmtTickLabel(65), "1:05");
  eq("a bogus tick reads as zero", fmtTickLabel(NaN), "0:00");
  eq("a negative tick reads as zero", fmtTickLabel(-1), "0:00");

  // The ruler and the clock must not disagree about which second it is.
  check(
    "the ruler and the transport clock agree on the second",
    [0, 30, 59.999, 60, 61, 599, 3599].every(
      (s) => fmtTickLabel(s).split(":")[1] === fmtTime(s).split(":")[1],
    ),
  );
}

// ── fmtTimeMs: the exact-loop fields ───────────────────────────────────────
{
  eq("zero", fmtTimeMs(0), "00:00.000");
  eq("milliseconds are kept", fmtTimeMs(1.234), "00:01.234");
  eq("a whole minute", fmtTimeMs(60), "01:00.000");
  eq("minutes, seconds and milliseconds", fmtTimeMs(125.5), "02:05.500");

  // The reason this function does integer-millisecond maths instead of
  // formatting the float directly: naive `(s % 60).toFixed(3)` gives
  // "00:60.000" here, and the carry never happens.
  eq("a value that rounds up to the next second carries", fmtTimeMs(59.9999), "01:00.000");
  eq("a value just under a millisecond rounds to zero", fmtTimeMs(0.0004), "00:00.000");
  eq("rounding is to nearest, not truncation", fmtTimeMs(0.0006), "00:00.001");
  eq("the carry propagates into the minutes", fmtTimeMs(119.9996), "02:00.000");

  eq("a bogus time reads as zero", fmtTimeMs(NaN), "00:00.000");
  eq("a negative time reads as zero", fmtTimeMs(-0.5), "00:00.000");
}

// ── parseTimecode: what the user is allowed to type ────────────────────────
{
  eq("plain decimal seconds", parseTimecode("12.48"), 12.48);
  eq("plain whole seconds", parseTimecode("7"), 7);
  eq("mm:ss", parseTimecode("1:30"), 90);
  eq("mm:ss with milliseconds", parseTimecode("1:30.250"), 90.25);
  eq("a single-digit seconds field", parseTimecode("2:05"), 125);
  eq("many minutes", parseTimecode("120:00"), 7200);
  eq("surrounding whitespace is ignored", parseTimecode("  1:30  "), 90);

  // Round-tripping is the property that matters: whatever the field renders,
  // the field must be able to read back.
  check(
    "every formatted time parses back to itself",
    [0, 0.25, 1.234, 59.999, 60, 125.5, 3599.999].every(
      (s) => Math.abs(parseTimecode(fmtTimeMs(s)) - Math.round(s * 1000) / 1000) < 1e-9,
    ),
  );

  // Rejections. These must be null rather than 0 or NaN: the caller treats
  // null as "keep the current loop point" and a number as "move it there".
  eq("an empty string is not a time", parseTimecode(""), null);
  eq("whitespace alone is not a time", parseTimecode("   "), null);
  eq("null is not a time", parseTimecode(null), null);
  eq("undefined is not a time", parseTimecode(undefined), null);
  eq("words are not a time", parseTimecode("abc"), null);
  eq("a bare colon is not a time", parseTimecode(":"), null);
  eq("a missing minutes field is not a time", parseTimecode(":30"), null);
  eq("a missing seconds field is not a time", parseTimecode("1:"), null);
  eq("a negative value is not a time", parseTimecode("-5"), null);
  eq("a negative mm:ss is not a time", parseTimecode("-1:30"), null);
  eq("60 seconds is not a valid seconds field", parseTimecode("1:60"), null);
  eq("99 seconds is not a valid seconds field", parseTimecode("1:99"), null);
  eq("more than three decimal places is refused", parseTimecode("1:30.2500"), null);
  eq("two colons are not a time", parseTimecode("1:2:3"), null);
  eq("trailing junk is refused", parseTimecode("1:30abc"), null);

  // A number typed into a text input arrives as a string, but a caller
  // passing the number directly must not break.
  eq("a numeric argument works", parseTimecode(12.5), 12.5);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
