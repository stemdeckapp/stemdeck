// The speed control's arithmetic, kept apart from transport.js so it can be
// tested in Node: the range, what a typed speed means, and the fader's notches.
// Nothing here holds state or touches the page.

// The range any speed may take. Karaoke Mode's presets and its 5% steps reach
// 1.5x (karaoke/karaokeModel.js takes these two from here), and the box in
// the footer takes the same range, so a speed set there can be read and
// nudged in the footer too.
export const SPEED_MIN = 0.5;
export const SPEED_MAX = 1.5;
// The footer's fader is for slowing a part down to practise it (#269
// follow-up), so it stops at 1x. A faster speed pins it at that end.
export const FADER_MAX = 1;
// The fader is notched every SPEED_NOTCH, and the wheel over it jumps notch to notch.
export const SPEED_NOTCH = 0.05;

/** `rate` held to `min`..`max` and rounded to a hundredth; NaN stays NaN. */
export function clampSpeed(rate, min = SPEED_MIN, max = SPEED_MAX) {
  const r = Number(rate);
  if (!Number.isFinite(r)) return NaN;
  return Math.round(Math.min(max, Math.max(min, r)) * 100) / 100;
}

/**
 * What a typed speed means, as a rate, before clamping. Takes "0.83", "0,83"
 * and "0.83x" as a rate, "83%" as a percentage, and a bare number above 2
 * ("83") as a percentage too, since no speed is above 2x. NaN for anything
 * else, so nonsense can be ignored rather than read as some speed.
 */
export function parseSpeed(text) {
  let s = String(text ?? "").trim().replace(",", ".");
  const percent = s.endsWith("%");
  s = s.replace(/[x×%]$/i, "").trim();
  if (!/^\d*\.?\d+$/.test(s)) return NaN;
  const n = parseFloat(s);
  return percent || n > 2 ? n / 100 : n;
}

/**
 * The next notch above (`dir` > 0) or below `rate`, so a speed between
 * notches (0.83x) lands on one (0.85x or 0.80x) rather than staying off them.
 * Not clamped.
 */
export function nextNotch(rate, dir) {
  // Rounded first, so 0.85 / 0.05 = 16.999... still counts as on notch 17.
  const notch = Math.round((rate / SPEED_NOTCH) * 100) / 100;
  const next = dir > 0 ? Math.floor(notch) + 1 : Math.ceil(notch) - 1;
  return Math.round(next * SPEED_NOTCH * 100) / 100;
}
