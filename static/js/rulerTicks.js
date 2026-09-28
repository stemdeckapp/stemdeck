// Spacing of the timeline's labelled ticks, kept apart from transport.js so it
// can be tested without a DOM (the same reason loopRegion.js is its own module).
// Shared by the ruler above the lanes and the one on the footer waveform: the
// two strips are the same width and start at the same x, so a time has to land
// at the same place in both.

// Label spacing the ruler will not go below, comfortably wider than a "10:00"
// label so neighbours never crowd each other.
export const MIN_TICK_PX = 110;
export const TICK_LADDER = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
// At 1x, never more labelled ticks than this across the whole track. A song
// keeps the 15/30/60-second steps it always had (a minute apart up to 12
// minutes); a long mix or a full album steps up the ladder instead, where a
// minute apart piled 59 labels on top of each other for an hour-long video.
export const MAX_BASE_TICKS = 12;

export function baseTickStep(durationSec) {
  const floor = durationSec < 90 ? 15 : durationSec < 300 ? 30 : 60;
  return (
    TICK_LADDER.find((step) => step >= floor && durationSec / step <= MAX_BASE_TICKS) ??
    TICK_LADDER[TICK_LADDER.length - 1]
  );
}

// `contentWidthPx` is the width the ticks will actually occupy. Omitted (the
// footer strip, which always shows the whole track) the step is the plain
// duration-based one, which is also what 1x has always used.
export function tickStepAt(durationSec, contentWidthPx, zoom) {
  const base = baseTickStep(durationSec);
  // Zoom is the only thing that subdivides it. Spreading the same handful of
  // ticks across five screen widths would make the ruler less useful the
  // further in you went, which is backwards.
  if (zoom <= 1 || !contentWidthPx || !durationSec) return base;
  const pxPerSec = contentWidthPx / durationSec;
  for (const step of TICK_LADDER) {
    if (step > base) break;
    if (step * pxPerSec >= MIN_TICK_PX) return step;
  }
  return base;
}
