// Take folders, the pure part: which takes a track's folder lists, in what
// order, whether the folder is open, and where its lanes sit in the stack.
// No DOM and no imports, so tests/js/take-folders.test.mjs runs it in node.
//
// A recording track is a take folder, as in a DAW: its main row plays one take
// (the active one), and a disclosure arrow opens one lane per take under it,
// newest on top. takeLanes.js draws the mixer rows, player.js the waveform
// lanes; both place them with the order numbers from here.

export const FOLDER_STORE_KEY = "stemdeck.takeFolders";
// Enough for every track a song can hold (TRACK_MAX_PER_JOB is 32) many times
// over; the oldest entries go first so the stored object cannot grow forever.
export const FOLDER_STORE_MAX = 200;
// Lanes are ordered in steps of this, so a folder's take lanes fit between its
// track and the next lane. A song holds at most 50 takes (TAKE_MAX_PER_JOB).
export const LANE_ORDER_STEP = 100;

/** The takes of one track, newest first (equal times: later in the list first). */
export function takesNewestFirst(takes) {
  if (!Array.isArray(takes)) return [];
  return takes
    .map((tk, i) => ({ tk, i }))
    .filter(({ tk }) => tk && typeof tk.id === "string")
    .sort((a, b) => ((Number(b.tk.created_at) || 0) - (Number(a.tk.created_at) || 0)) || (b.i - a.i))
    .map(({ tk }) => tk);
}

/** Read the stored open/closed choices; anything unreadable is "nothing stored". */
export function parseFolderStore(raw) {
  if (typeof raw !== "string" || !raw) return {};
  try {
    const v = JSON.parse(raw);
    if (!v || typeof v !== "object" || Array.isArray(v)) return {};
    const out = {};
    for (const [k, open] of Object.entries(v)) if (typeof open === "boolean") out[k] = open;
    return out;
  } catch {
    return {};
  }
}

/**
 * Whether a track's folder shows its take lanes.
 *   - no takes: closed, there is nothing to show;
 *   - one take: closed unless opened in this session (`session`), since the
 *     main row already shows that take;
 *   - two or more: what the user last chose for this track, open by default
 *     (a second take is the moment the choice between takes starts to matter).
 */
export function isFolderOpen({ trackId, takeCount, store, session }) {
  if (!takeCount) return false;
  if (takeCount === 1) return session?.[trackId] === true;
  const v = store?.[trackId];
  return typeof v === "boolean" ? v : true;
}

/** The store with one choice changed, trimmed to FOLDER_STORE_MAX entries. */
export function withFolderChoice(store, trackId, open) {
  const out = { ...(store || {}) };
  delete out[trackId];
  out[trackId] = !!open;
  const keys = Object.keys(out);
  for (let i = 0; i < keys.length - FOLDER_STORE_MAX; i++) delete out[keys[i]];
  return out;
}

/** The CSS order of a lane at position `index` in the stack (stems and tracks). */
export function laneOrder(index) {
  return index * LANE_ORDER_STEP;
}

/**
 * The CSS order of the k-th take lane (0 = newest) under the lane at `index`.
 * +1 is the track's EQ panel row (eq/eqPanelKinds.js eqRowOrder), so the
 * takes start at +2.
 */
export function takeLaneOrder(index, k) {
  return index * LANE_ORDER_STEP + 2 + Math.min(k, LANE_ORDER_STEP - 3);
}

/**
 * The take lanes to show, per open folder, in stack order:
 * [{ trackId, takes: [{ take, playing }] }]. `tracks` are the job's tracks in
 * track order, each with `takes` and `active` (takeLanes.js's model);
 * `isOpen(track)` decides.
 */
export function openFolders(tracks, isOpen) {
  const out = [];
  for (const tr of tracks || []) {
    if (!tr?.takes?.length || !isOpen(tr)) continue;
    out.push({
      trackId: tr.id,
      takes: takesNewestFirst(tr.takes).map((take) => ({ take, playing: take.id === tr.active?.id })),
    });
  }
  return out;
}

/** How many rows the open folders add to the stack. */
export function folderRowCount(folders) {
  return (folders || []).reduce((n, f) => n + (f.takes?.length || 0), 0);
}

/** "+12 ms" / "-3 ms" / "" for a take's nudge, the way the row shows it. */
export function nudgeText(ms) {
  const n = Math.round(Number(ms) || 0);
  if (!n) return "";
  return `${n > 0 ? "+" : "−"}${Math.abs(n)} ms`;
}
