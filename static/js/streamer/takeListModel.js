// The take lanes of Performance Mode's timeline (streamer.js paintTakeRows):
// every take of the song's Performance track is its own row under the take
// lane, oldest on top, newest at the bottom, as Logic Pro stacks take lanes.
// One take plays at a time; its row is marked. Pure functions only, so node
// can test them (tests/js/streamer-takes.test.mjs).
//
// The stage covers the mixer, so the Performance track's take folder is out
// of sight in Performance, and Practice and Advanced do not show the track at
// all (modeStems.js shownTracks). These rows are where a take is chosen.

import { takesNewestFirst } from "../capture/takeFolderModel.js";

// One take leaves nothing to choose: the rows show from two takes, so a song
// with one take keeps the timeline as it always was.
export const TAKE_LANES_MIN = 2;

/** The id of the take `track` plays (its active take), or null. */
export function activeTakeId(track) {
  if (!track || typeof track !== "object") return null;
  const a = track.active;
  const id = (a && (a.id ?? a.take_id)) ?? track.active_take_id ?? null;
  return typeof id === "string" && id ? id : null;
}

/**
 * The rows for `track` (a track as takeLanes.getTracks gives it:
 * { takes, active, active_take_id }): [{ id, n, label, active }], oldest
 * first (equal times: earlier in the list first). `n` is the row's number
 * from 1 (the row reads "Take n"); `label` is the take's own name (its id
 * when it has none), for the row's tooltip.
 */
export function takeLaneRows(track) {
  const active = activeTakeId(track);
  return takesNewestFirst(track?.takes).reverse().map((tk, i) => ({
    id: tk.id,
    n: i + 1,
    label: typeof tk.label === "string" && tk.label.trim() ? tk.label : tk.id,
    active: tk.id === active,
  }));
}

/** Whether the rows show. */
export function showsTakeLanes(rows) {
  return Array.isArray(rows) && rows.length >= TAKE_LANES_MIN;
}

/**
 * What choosing `takeId` does:
 *   "select" make it the track's active take (the server), then the lane's
 *   "keep"   it already plays but the take lane shows another: only the
 *            lane follows
 *   "none"   nothing (not a row, already shown, or `locked`: a take is
 *            recording or the last take is exporting)
 * `laneTake` is the take the stage's take lane shows now (or null).
 */
export function chooseAction(rows, takeId, { locked = false, laneTake = null } = {}) {
  if (locked || !takeId || !Array.isArray(rows)) return "none";
  const row = rows.find((r) => r.id === takeId);
  if (!row) return "none";
  if (!row.active) return "select";
  return laneTake === takeId ? "none" : "keep";
}

/**
 * The take the stage's take lane should follow, or null: the take that plays,
 * when the lane shows another row's take or none (it was chosen in Advanced,
 * or in another browser). Never while `locked`, never with the rows hidden,
 * and never while the lane shows a take that is not a row yet (one just
 * recorded, before the song's takes reload; it becomes the take that plays).
 */
export function followTake(rows, laneTake, { locked = false } = {}) {
  if (locked || !showsTakeLanes(rows)) return null;
  const active = rows.find((r) => r.active);
  if (!active || active.id === laneTake) return null;
  if (laneTake && !rows.some((r) => r.id === laneTake)) return null;
  return active.id;
}

/**
 * Which row to scroll into view, or null. `before` is the row ids of the
 * last paint (null: the rows were not shown). Shown anew: the row that plays.
 * Rows added since (a take just recorded): the newest of them.
 */
export function scrollTarget(before, rows) {
  if (!showsTakeLanes(rows)) return null;
  if (!Array.isArray(before)) return rows.find((r) => r.active)?.id ?? null;
  const seen = new Set(before);
  const added = rows.filter((r) => !seen.has(r.id));
  return added.length ? added[added.length - 1].id : null;
}

/** What the rows show, as one string: rebuild only when it changes. */
export function takeLanesSig(rows, locked) {
  const list = Array.isArray(rows) ? rows : [];
  return `${locked ? 1 : 0}|${list.map((r) => `${r.id}:${r.n}:${r.active ? 1 : 0}:${r.label}`).join(",")}`;
}
