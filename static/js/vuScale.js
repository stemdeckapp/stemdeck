// How a lane meter turns an RMS amplitude into a bar length.
//
// This was linear once, `min(1, rms * 2.5)`, which sounds reasonable and is
// not: loudness is logarithmic, and a separated stem sits around -20 to
// -30 dBFS. Measured across a real library the median lane filled 10.4% of its
// meter and the drum and "other" stems sat under 2%, so the top three quarters
// of every bar were decoration.
//
// Its own module so the mapping can be measured directly. player.js cannot be
// imported outside a browser, and a scale nothing can check is how the linear
// version survived as long as it did.

// Lowest level a meter draws. Below this a lane reads as silent.
//
// -60 dB is the usual floor for a small console meter: quiet passages still
// register, and full scale stays reachable without being reached constantly.
export const VU_FLOOR_DB = -60;

/**
 * Map an RMS amplitude onto 0..1, in dB rather than in amplitude.
 *
 * Takes RMS, not peak: peak meters on separated stems are dominated by
 * transients and read as a row of bars slamming to full on every beat.
 */
export function vuLevel(rms) {
  if (!(rms > 0)) return 0;
  const db = 20 * Math.log10(rms);
  return Math.max(0, Math.min(1, (db - VU_FLOOR_DB) / -VU_FLOOR_DB));
}
