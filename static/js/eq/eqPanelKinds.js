// EQ panels, the pure part: which panel a lane gets, how tall its row is,
// where the row sits in the lane stack, and the remembered open/closed state.
// No DOM and no imports, so tests/js/eq-panels.test.mjs runs it in node.
//
// Every mixer lane has an EQ tab in its header, in the mixer column.
// The tab opens the lane's EQ panel as an extra row right under the lane, in
// both columns (mixer and waveforms), the way a take folder opens its takes.
// eqRows.js draws the rows; player.js counts their height into the stack.

/** Panel kinds. "graph" and "drum" are the same graphical editor. */
export const PANEL_KINDS = Object.freeze(["graph", "drum", "bass", "guitar", "keys", "voice"]);

const VOICE_LANES = new Set(["vocals", "lead_vocals", "backing_vocals"]);

/**
 * The one panel kind a lane gets.
 *   bass -> Bass EQ, guitar -> Guitar EQ, drums -> Drum EQ (graphical),
 *   piano -> Keys EQ, vocals / lead / backing -> the voice placeholder,
 *   original, other and recording tracks -> the general graphical EQ.
 * A recording track whose input is a microphone (`inputKind` "voice") gets
 * the voice placeholder, like a vocal stem.
 */
export function panelKindFor(lane, { isTrack = false, inputKind = null } = {}) {
  if (isTrack) return inputKind === "voice" ? "voice" : "graph";
  if (lane === "bass") return "bass";
  if (lane === "guitar") return "guitar";
  if (lane === "drums") return "drum";
  if (lane === "piano") return "keys";
  if (VOICE_LANES.has(lane)) return "voice";
  return "graph";
}

/**
 * Height of a panel row in px, the same in both columns. Fixed rather than a
 * multiple of the lane height: lanes stretch to fill a tall window, and a
 * graph editor six lanes tall would push everything else off screen.
 */
export const PANEL_HEIGHT = Object.freeze({
  graph: 304,
  drum: 304,
  bass: 256,
  guitar: 256,
  keys: 212,
  voice: 156,
});

export function panelHeight(kind) {
  return PANEL_HEIGHT[kind] || PANEL_HEIGHT.graph;
}

/** Lane ids that never get a panel (the master slot has its own place). */
export function laneHasPanel(lane) {
  return typeof lane === "string" && lane !== "" && !lane.startsWith("__");
}

/**
 * The CSS order of a lane's EQ row: right under the lane. Lanes are ordered
 * in steps of 100 (takeFolderModel.LANE_ORDER_STEP); the EQ row takes +1 and a
 * take folder's lanes follow it from +2.
 */
export function eqRowOrder(laneOrder) {
  return laneOrder + 1;
}

/**
 * Extra height the open panels add to the lane stack, in px.
 * `lanes`: the lanes on screen; `isOpen(lane)`; `kindOf(lane)`.
 */
export function openPanelsHeight(lanes, isOpen, kindOf) {
  let px = 0;
  for (const lane of lanes || []) if (laneHasPanel(lane) && isOpen(lane)) px += panelHeight(kindOf(lane));
  return px;
}

/**
 * Lane height when the panel stack shares the wave panel with the lanes: the
 * lanes divide what the open panels leave, never under `minRow` and, when
 * `maxRow` is given, never over it (the stack then ends above the panel's
 * bottom instead of stretching every row).
 * Returns {rowH, stackH} where stackH = count * rowH + panelsPx.
 */
export function laneLayout(panelH, count, panelsPx, minRow, maxRow = Infinity) {
  const free = Math.max(0, (panelH || 0) - (panelsPx || 0));
  const fill = panelH > 0 && count > 0 ? Math.floor(free / count) : minRow;
  const rowH = Math.max(minRow, Math.min(maxRow, fill));
  return { rowH, stackH: count * rowH + (panelsPx || 0) };
}

// ─── Remembered open state ───

export const OPEN_STORE_KEY = "stemdeck.eqPanels";
// Oldest entries go first so the stored object cannot grow forever.
export const OPEN_STORE_MAX = 300;

/** The store key of one lane of one song. */
export function openKey(jobId, lane) {
  return `${jobId || ""}:${lane}`;
}

/** Read the stored open choices; anything unreadable is "nothing stored". */
export function parseOpenStore(raw) {
  if (typeof raw !== "string" || !raw) return {};
  try {
    const v = JSON.parse(raw);
    if (!v || typeof v !== "object" || Array.isArray(v)) return {};
    const out = {};
    for (const [k, open] of Object.entries(v)) if (open === true) out[k] = true;
    return out;
  } catch {
    return {};
  }
}

/** The store with one choice changed (closed lanes are simply absent). */
export function withOpenChoice(store, key, open) {
  const out = { ...(store || {}) };
  delete out[key];
  if (open) out[key] = true;
  const keys = Object.keys(out);
  for (let i = 0; i < keys.length - OPEN_STORE_MAX; i++) delete out[keys[i]];
  return out;
}
