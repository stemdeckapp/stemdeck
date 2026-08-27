// Build the source list used by the Web Audio engines.
//
// The visible "original" lane is a mix of every base stem the user did not
// select. Feeding original.wav through a pitch shifter would also transpose
// any drums inside it. Current jobs retain the individual Demucs stems, so the
// engine can rebuild that lane as a control group and route drums separately.

function playable(stem) {
  return !!stem?.name && !!stem?.url;
}

function sourceSpec(stem, controlName = stem.name, pitched = stem.name !== "drums") {
  return {
    name: stem.name,
    url: stem.url,
    controlName,
    // Never allow the drums stem onto the pitched bus, even if a future caller
    // supplies a mistaken override.
    pitched: stem.name !== "drums" && pitched !== false,
  };
}

/**
 * Convert visible mixer lanes into independently routable playback sources.
 *
 * @param {{name:string,url:string}[]} rawStems Every source returned by the job.
 * @param {{name:string,url:string}[]} visibleStems Lanes shown in the mixer.
 * @param {string[]} baseStemNames Canonical separation stems for this server.
 */
export function buildPlaybackStems(rawStems, visibleStems, baseStemNames) {
  const rawByName = new Map(rawStems.filter(playable).map((stem) => [stem.name, stem]));
  const visible = visibleStems.filter(playable);
  const dedicated = visible.filter((stem) => stem.name !== "original");
  const result = dedicated.map((stem) => sourceSpec(stem));

  if (!visible.some((stem) => stem.name === "original")) return result;

  const dedicatedNames = new Set(dedicated.map((stem) => stem.name));
  // A completed lead/backing split replaces the base vocals source. Treat the
  // base stem as selected so it is not added to the complement as well.
  if (dedicatedNames.has("lead_vocals") && dedicatedNames.has("backing_vocals")) {
    dedicatedNames.add("vocals");
  }

  const complementNames = baseStemNames.filter((name) => !dedicatedNames.has(name));
  if (complementNames.length === 0) return result;

  const canReconstruct = complementNames.every((name) => rawByName.has(name));
  if (canReconstruct) {
    for (const name of complementNames) {
      result.push(sourceSpec(rawByName.get(name), "original"));
    }
    return result;
  }

  // Older or partial jobs may not expose every component. Their rendered mix
  // cannot be separated client-side, so keep the entire lane unpitched. This
  // is intentionally conservative: melodic content may remain in the old key,
  // but drums are never resampled and no source is played twice.
  const original = rawByName.get("original");
  if (original) result.push(sourceSpec(original, "original", false));
  return result;
}

/**
 * Full-decode mode also supplies AudioBuffers for waveform rendering. Add any
 * visible source that playback reconstructed from other files as decode-only.
 */
export function addVisualOnlyStems(playbackStems, visibleStems) {
  const loadedNames = new Set(playbackStems.map((stem) => stem.name));
  return [
    ...playbackStems,
    ...visibleStems
      .filter((stem) => playable(stem) && !loadedNames.has(stem.name))
      .map((stem) => ({ ...stem, controlName: stem.name, pitched: false, visualOnly: true })),
  ];
}
