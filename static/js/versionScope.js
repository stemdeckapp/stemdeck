// Versions of a song (#804): which one is open, and where its records live.
//
// A song keeps one set of stem files. A version is a named choice of stems
// with its own saved mix (app/core/versions.py). The mix and the stem layout
// of every mode are the records the page already keeps per song
// (mixer.js mixKey, partChoice.js choiceKey); a version only changes the
// song part of their key, its "scope":
//
//   the default version ("main")  <jobId>             the keys every song had
//   any other version             <jobId>~<versionId>
//
// So a song made before versions opens its default version with the mix and
// lanes it always had, and nothing is moved or rewritten.
//
// The server says which versions a song has and which one opens with it;
// catalog.js and versions.js hand that here. Pure: no DOM, no store, no fetch,
// testable in Node.

export const DEFAULT_VERSION_ID = "main";
const VERSION_ID_RE = /^[a-z0-9]{1,16}$/;
const BASE = ["vocals", "drums", "bass", "guitar", "piano", "other"];

// jobId -> { versions: [{ id, name, stems, created_at }], active }
const songs = new Map();

/** A version list as the server sends it, made safe: unknown ids dropped,
 * stems in base order. */
export function cleanVersions(list) {
  if (!Array.isArray(list)) return [];
  const seen = new Set();
  const out = [];
  for (const v of list) {
    if (!v || typeof v !== "object" || !VERSION_ID_RE.test(String(v.id || "")) || seen.has(v.id)) continue;
    seen.add(v.id);
    const stems = BASE.filter((n) => Array.isArray(v.stems) && v.stems.includes(n));
    out.push({
      id: v.id,
      name: typeof v.name === "string" && v.name.trim() ? v.name.trim() : null,
      stems: stems.length ? stems : [...BASE],
      created_at: Number.isFinite(v.created_at) ? v.created_at : 0,
    });
  }
  return out;
}

/** Remember what the server says of `jobId`'s versions. */
export function setSongVersions(jobId, versions, active) {
  if (!jobId) return;
  const list = cleanVersions(versions);
  if (!list.length) {
    songs.delete(jobId);
    return;
  }
  const ids = list.map((v) => v.id);
  songs.set(jobId, { versions: list, active: ids.includes(active) ? active : ids[0] });
}

/** `jobId`'s versions, never empty: the implicit default when none is known. */
export function songVersions(jobId) {
  return songs.get(jobId)?.versions.map((v) => ({ ...v, stems: [...v.stems] }))
    ?? [{ id: DEFAULT_VERSION_ID, name: null, stems: null, created_at: 0 }];
}

/** The id of the version open (or opening) for `jobId`. */
export function activeVersionId(jobId) {
  return songs.get(jobId)?.active || DEFAULT_VERSION_ID;
}

/** The stems of `jobId`'s open version, or null when none is known (the
 * song's own selection then applies). */
export function activeVersionStems(jobId) {
  const entry = songs.get(jobId);
  const v = entry?.versions.find((x) => x.id === entry.active);
  return v ? [...v.stems] : null;
}

/** The song part of a record key for `jobId` in version `versionId` (the
 * open one when left out). */
export function scopeFor(jobId, versionId = activeVersionId(jobId)) {
  if (!versionId || versionId === DEFAULT_VERSION_ID) return jobId;
  if (!VERSION_ID_RE.test(versionId)) throw new Error("bad version id");
  return `${jobId}~${versionId}`;
}

/** Two stem lists hold the same base stems. */
export function sameStems(a, b) {
  const x = BASE.filter((n) => (a || []).includes(n));
  const y = BASE.filter((n) => (b || []).includes(n));
  return x.length === y.length && x.every((n, i) => n === y[i]);
}

/** The version of `versions` whose stems are exactly `stems`, or null. */
export function versionWithStems(versions, stems) {
  return (versions || []).find((v) => v.stems && sameStems(v.stems, stems)) || null;
}

/** The number a new unnamed version is called by: one past the count, and
 * past any "N" already used as a name ending. */
export function nextVersionNumber(versions) {
  let n = (versions || []).length + 1;
  const used = new Set((versions || []).map((v) => String(v.name || "")));
  while ([...used].some((name) => name.endsWith(` ${n}`))) n += 1;
  return n;
}
