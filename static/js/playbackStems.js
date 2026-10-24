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
 * @param {Set<string>} [exclude] Stems left out entirely, as a lane and as a
 *   part of "original" (Streamer Mode's "I play": the musician plays it).
 */
export function buildPlaybackStems(rawStems, visibleStems, baseStemNames, exclude = new Set()) {
  const rawByName = new Map(rawStems.filter(playable).map((stem) => [stem.name, stem]));
  const visible = visibleStems.filter((stem) => playable(stem) && !exclude.has(stem.name));
  const dedicated = visible.filter((stem) => stem.name !== "original");
  const result = dedicated.map((stem) => sourceSpec(stem));

  if (!visible.some((stem) => stem.name === "original")) return result;

  const dedicatedNames = new Set(dedicated.map((stem) => stem.name));
  // A completed lead/backing split replaces the base vocals source. Treat the
  // base stem as selected so it is not added to the complement as well.
  if (dedicatedNames.has("lead_vocals") && dedicatedNames.has("backing_vocals")) {
    dedicatedNames.add("vocals");
  }

  const complementNames = baseStemNames.filter((name) => !dedicatedNames.has(name) && !exclude.has(name));
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
  // With a stem excluded the rendered mix would bring it back: nothing then.
  const original = rawByName.get("original");
  if (original && exclude.size === 0) result.push(sourceSpec(original, "original", false));
  return result;
}

/**
 * The waveform points of a lane that sums `names` (an Original a mode's
 * layout made): per point, the sum of the stems' [min, max]. An envelope at
 * or above the true one, close enough for a lane picture, and no server work.
 * Null when none of them has peaks; a stem without peaks counts as silence.
 */
export function sumPeaks(peaks, names) {
  const lists = (names || []).map((n) => peaks?.[n]).filter((p) => Array.isArray(p) && p.length);
  if (!lists.length) return null;
  const len = Math.min(...lists.map((p) => p.length));
  const out = new Array(len);
  for (let i = 0; i < len; i++) {
    let mn = 0;
    let mx = 0;
    for (const p of lists) {
      mn += p[i][0];
      mx += p[i][1];
    }
    out[i] = [Math.max(-1, mn), Math.min(1, mx)];
  }
  return out;
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

/**
 * Whether any of these stems is stored compressed (MP3, #783), by the
 * extension its URL ends in. The chunked engine streams PCM straight out of
 * a WAV by Range request and cannot read an MP3, so a song stored as MP3
 * opens on the full-decode engine instead, whose decodeAudioData honours the
 * MP3's encoder delay and padding and lines the stems up to the sample (the
 * server checks the same before it keeps an MP3).
 *
 * @param {{url?:string}[]} stems
 */
export function hasCompressedStems(stems) {
  return (stems || []).some((stem) => stemFileExt(stem?.url) === "mp3");
}

/**
 * The file extension a stem downloads with: "mp3" for a stem stored as MP3,
 * else "wav". The desktop app saves a lane download under the name the page
 * gives it, so the name has to say what the bytes are.
 *
 * @param {string|undefined} url
 */
export function stemFileExt(url) {
  const path = String(url || "").split(/[?#]/)[0];
  return /\.mp3$/i.test(path) ? "mp3" : "wav";
}
