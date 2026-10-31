// Which song Delete or Backspace moves to the Trash, if any.
//
// The keys used to trash the open song from anywhere on the page: with focus
// on Play, a mute button, a stem switch or a Karaoke queue row, one key press
// sent the song to the Trash with no confirmation. Now they act only where a
// song row is what the user is on: the library list itself, or the page body
// when the last click was in that list (a click on a row does not focus it,
// and opening the song re-renders the list, so focus is on the body then).
//
// No DOM access beyond `closest`, so the rule runs under node (tests/js).

/** The song rows. Not the rail, the search box or the Trash bar above them. */
export const LIBRARY_LIST = "#catalogList, #catalogStrip";

const TEXT_EDITING = "input, textarea, select, [contenteditable=''], [contenteditable='true'], [contenteditable='plaintext-only'], .folder-editor";

/** Whether keys typed at `target` edit text there. */
export function isTextEditingTarget(target) {
  if (!target || typeof target.closest !== "function") return false;
  return Boolean(target.isContentEditable || target.closest(TEXT_EDITING));
}

/** Whether `target` is inside the library's song rows. */
export function inLibraryList(target) {
  return Boolean(target && typeof target.closest === "function" && target.closest(LIBRARY_LIST));
}

/**
 * The track id a Delete or Backspace press should move to the Trash, or null.
 *
 * @param {KeyboardEvent|object} e
 * @param {object} o
 * @param {string|null} o.openTrackId  the song open in the studio
 * @param {boolean} o.lastPointerInList  the last pointer press was in the list
 * @param {object} [o.body]  document.body
 * @param {object} [o.root]  document.documentElement
 */
export function libraryDeleteTrackId(e, { openTrackId = null, lastPointerInList = false, body = null, root = null } = {}) {
  if (!e || (e.key !== "Delete" && e.key !== "Backspace")) return null;
  // Someone else answered it: Streamer deletes its own layer, a queue row
  // removes itself, the EQ removes a band.
  if (e.defaultPrevented) return null;
  const target = e.target;
  if (isTextEditingTarget(target)) return null;
  if (inLibraryList(target)) {
    const row = target.closest(".cat-item[data-id]");
    // A Trash row is already in the Trash, and a queue row is an import.
    if (row && (row.classList?.contains("in-trash") || row.classList?.contains("queue-row"))) return null;
    return row?.dataset?.id || openTrackId || null;
  }
  const onPage = !target || target === body || target === root;
  if (onPage && lastPointerInList) return openTrackId || null;
  return null;
}
