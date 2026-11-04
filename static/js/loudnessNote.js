// The note under the analysis cards after an older song's loudness was
// measured again with the 0.21.0 method (#923). The LUFS and Dynamic Range
// numbers moved, and nothing else said why.
//
// This module owns only the note's DOM. Whether a song's note was already
// dismissed lives on the song's library row (catalog.js), which passes it in,
// and is told through the callback given to wireLoudnessNote.

import { t } from "./i18n.js";

const KEYS = { source: "meta.loudnessNote.source", stems: "meta.loudnessNote.stems" };

/**
 * The i18n key of the note to show, or null for none. ``loudness`` is the
 * song's {remeasured, basis, version}; ``seenVersion`` is the loudness
 * version the note was dismissed for (undefined if never), so a later change
 * of method would show it again.
 */
export function loudnessNoteKey(loudness, seenVersion) {
  if (!loudness?.remeasured) return null;
  const key = KEYS[loudness.basis];
  if (!key) return null;
  return seenVersion === (loudness.version ?? null) ? null : key;
}

let shownFor = null;

/** Show the note for ``trackId`` when loudnessNoteKey says so, else hide it. */
export function paintLoudnessNote(trackId, loudness, seenVersion) {
  const note = document.getElementById("loudness-note");
  const text = document.getElementById("loudness-note-text");
  if (!note || !text) return;
  const key = loudnessNoteKey(loudness, seenVersion);
  shownFor = key ? { trackId, version: loudness.version ?? null } : null;
  note.hidden = !key;
  if (key) {
    // data-i18n, so a language switch (applyTranslations) rewrites it.
    text.dataset.i18n = key;
    text.textContent = t(key);
  }
}

export function hideLoudnessNote() {
  paintLoudnessNote(null, null);
}

/** ``onDismiss(trackId, version)`` keeps the dismissal; the note then hides. */
export function wireLoudnessNote(onDismiss) {
  document.getElementById("loudness-note-dismiss")?.addEventListener("click", () => {
    const dismissed = shownFor;
    hideLoudnessNote();
    if (dismissed) onDismiss(dismissed.trackId, dismissed.version);
  });
}
