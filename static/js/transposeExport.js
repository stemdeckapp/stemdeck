// Whether an export that carries a key change can be made (#858).
//
// A lane transposed in the studio is exported through FFmpeg's rubberband
// filter, and plenty of FFmpeg builds have none (the macOS download among
// them). /api/health reports `transpose_export`; on false, every export that
// would send a key change is held back with its reason instead of failing
// with a 422 once the user has already picked where to save it. A mix with
// every key at 0 sends no key change at all, so it always stays available.
//
// Pure, so tests/js/transpose-export.test.mjs runs it without a browser.

/**
 * The health answer as true, false, or null (unknown: an older backend, or no
 * FFmpeg to ask). Only a real `false` ever holds an export back.
 */
export function transposeExportFromHealth(health) {
  const v = health?.transpose_export;
  return v === true || v === false ? v : null;
}

/** Whether any lane's key is moved, as the export would send it. */
export function isTransposed(pitches) {
  return Array.isArray(pitches) && pitches.some((p) => (Number(p) || 0) !== 0);
}

/**
 * Whether an export of a mix with these per-lane keys must be held back.
 * `available` is transposeExportFromHealth's answer: unknown leaves the export
 * offered, and the backend still refuses it with a clear message.
 */
export function transposeExportBlocked(pitches, available) {
  return available === false && isTransposed(pitches);
}
