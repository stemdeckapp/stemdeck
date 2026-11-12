// Settings dialog: the pure parts of its navigation and search.
//
// No DOM here, so the matching, the highlight ranges and the arrow-key moves
// are pinned by tests/js/settings-nav.test.mjs without a browser. catalog.js
// builds the entries from the live dialog and renders what these return.

/**
 * Lower-case, accent-free copy of `text`, with `map[i]` giving the index in
 * `text` that normalised character `i` came from. Searching "loschen" finds
 * "Löschen", and a match is still highlighted on the original letters.
 */
export function normalizeText(text) {
  const src = String(text ?? "");
  let norm = "";
  const map = [];
  // for...of walks code points, so a surrogate pair stays one unit.
  let i = 0;
  for (const ch of src) {
    const folded = ch.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
    for (const f of folded) {
      norm += f;
      map.push(i);
    }
    i += ch.length;
  }
  map.push(src.length);
  return { norm, map };
}

/** The search words: normalised, split on white space, empty ones dropped. */
export function queryTerms(query) {
  return normalizeText(query).norm.split(/\s+/).filter(Boolean);
}

/**
 * Every place a term occurs in `text`, as merged [start, end) ranges on the
 * original string, sorted.
 */
export function findRanges(text, terms) {
  const { norm, map } = normalizeText(text);
  const raw = [];
  for (const term of terms) {
    if (!term) continue;
    let from = 0;
    for (;;) {
      const at = norm.indexOf(term, from);
      if (at < 0) break;
      raw.push([map[at], map[at + term.length - 1] + 1]);
      from = at + term.length;
    }
  }
  raw.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged = [];
  for (const r of raw) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  }
  return merged;
}

/** Split `text` into plain and matched parts, for rendering with textContent. */
export function highlightParts(text, ranges) {
  const src = String(text ?? "");
  const parts = [];
  let pos = 0;
  for (const [start, end] of ranges) {
    if (start > pos) parts.push({ text: src.slice(pos, start), match: false });
    if (end > start) parts.push({ text: src.slice(start, end), match: true });
    pos = Math.max(pos, end);
  }
  if (pos < src.length) parts.push({ text: src.slice(pos), match: false });
  return parts;
}

/**
 * Settings that match `query`. An entry is { title, desc, group, section, ... }
 * and matches when every word occurs somewhere in those four. Titles that hold
 * every word come first, then titles that hold some, then the rest, each in
 * the order given (which is the order of the dialog).
 *
 * Returns [{ entry, titleRanges, descRanges }], or [] for an empty query.
 */
export function searchSettings(entries, query) {
  const terms = queryTerms(query);
  if (!terms.length) return [];
  const scored = [];
  entries.forEach((entry, order) => {
    const hay = normalizeText(
      [entry.title, entry.desc, entry.group, entry.section].filter(Boolean).join(" \n "),
    ).norm;
    if (!terms.every((term) => hay.includes(term))) return;
    const title = normalizeText(entry.title).norm;
    const inTitle = terms.filter((term) => title.includes(term)).length;
    const score = inTitle === terms.length ? 2 : inTitle > 0 ? 1 : 0;
    scored.push({
      entry,
      order,
      score,
      titleRanges: findRanges(entry.title, terms),
      descRanges: findRanges(entry.desc, terms),
    });
  });
  scored.sort((a, b) => b.score - a.score || a.order - b.order);
  return scored.map(({ entry, titleRanges, descRanges }) => ({ entry, titleRanges, descRanges }));
}

/** How many results fall in each section, as { [section id]: count }. */
export function countBySection(results) {
  const counts = {};
  for (const r of results) {
    const id = r.entry.pane;
    counts[id] = (counts[id] || 0) + 1;
  }
  return counts;
}

/**
 * Where a key press moves focus in a list of `count` tabs, from `index`, or
 * null for a key that does not move it. Both arrow pairs work: the list is
 * vertical in the wide dialog and a horizontal row in a narrow one, and a user
 * should not have to know which. Moves wrap at the ends.
 */
export function nextTabIndex(index, key, count) {
  if (count <= 0) return null;
  switch (key) {
    case "ArrowDown":
    case "ArrowRight":
      return (index + 1) % count;
    case "ArrowUp":
    case "ArrowLeft":
      return (index - 1 + count) % count;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}
