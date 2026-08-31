// Loop-region geometry, kept apart from transport.js so it can be tested
// without a DOM (same reason playbackStems.js is its own module).

// Shortest loop worth having. Also the threshold that separates a click from a
// drag, so a press that barely moves seeks instead of resizing.
export const MIN_LOOP_SEC = 0.2;

/// Where a loop-region drag lands, given where it started and where the pointer
/// is now (#538, discussion #507).
///
/// `mode` is "start" or "end" to move one edge alone, or "move" to slide the
/// whole region. The edge cases here -- an edge crossing its partner, a region
/// pushed against either end of the track -- are where this goes wrong, not in
/// the event plumbing, which is why this is a pure function.
export function loopDragResult({
  mode,
  pointerTime,
  grabTime,
  fromStart,
  fromEnd,
  duration,
  minLoop = MIN_LOOP_SEC,
}) {
  if (mode === "start") {
    // Cannot cross the far edge: minLoop is what keeps a loop audible.
    return {
      start: Math.max(0, Math.min(pointerTime, fromEnd - minLoop)),
      end: fromEnd,
    };
  }
  if (mode === "end") {
    return {
      start: fromStart,
      end: Math.min(duration, Math.max(pointerTime, fromStart + minLoop)),
    };
  }
  // Move: clamp the region as a unit. Clamping each edge on its own would
  // squash the loop against the track boundary instead of stopping it there.
  const length = fromEnd - fromStart;
  const shift = Math.max(-fromStart, Math.min(pointerTime - grabTime, duration - fromEnd));
  return { start: fromStart + shift, end: fromStart + shift + length };
}
