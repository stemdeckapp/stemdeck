// The icon an uploaded track shows where artwork would go: a file, with its
// format on a band coloured per format (#690).
//
// Only uploads get one. A link import brings its own thumbnail or has no file
// of its own to describe, so it keeps the note. The format comes from the
// server as `source_format`, taken from the extension the upload was validated
// against. It used to be read out of `sourceUrl`, which is "local:<title>"
// with the extension already removed, so there was never anything to read.
//
// A module of its own because the library (catalog.js) and the now-playing
// square it shares with the player (player.js) both draw it, and player.js
// cannot import from catalog.js without a cycle.

// Mirrors _ALLOWED_EXTS in app/api/jobs.py. Anything else is not a format this
// app accepts, so it is not drawn: the value reaches markup.
const FORMATS = new Set(["mp3", "wav", "flac", "mp4", "m4a", "ogg", "opus"]);

/** The upload's format, lower case, or "" when there is no icon to draw. */
export function trackFormat(track) {
  if (!track || track.thumb) return "";
  if (!(track.sourceUrl || "").startsWith("local:")) return "";
  const format = String(track.sourceFormat || "").toLowerCase();
  return FORMATS.has(format) ? format : "";
}

/**
 * The icon for a format from FORMATS, as SVG markup.
 *
 * Drawn on the note icon's 24-unit grid with its stroke weight, so the two sit
 * together as one set. The band's colour comes from the stylesheet
 * (.format-icon[data-format] in daw.css), so the palette lives in one place.
 */
export function formatIconSvg(format) {
  return `<svg class="format-icon" data-format="${format}" viewBox="0 0 24 24" aria-hidden="true">`
    + `<path class="format-icon-page" d="M7 3h7l4 4v14H7z"/>`
    + `<path class="format-icon-page" d="M14 3v4h4"/>`
    + `<rect class="format-icon-band" x="2.5" y="11.5" width="19" height="7.2" rx="1.6"/>`
    + `<text class="format-icon-label" x="12" y="17.1" text-anchor="middle">${format.toUpperCase()}</text>`
    + `</svg>`;
}

let _noteHtml = null;

/**
 * Draw the now-playing square's placeholder: the format icon for an upload,
 * the note otherwise. The note is whatever the markup shipped, taken once, so
 * it is not written out a second time here.
 */
export function paintNowPlayingArt(format) {
  const el = document.querySelector("#np-art .np-art-placeholder");
  if (!el) return;
  _noteHtml ??= el.innerHTML;
  el.innerHTML = FORMATS.has(format) ? formatIconSvg(format) : _noteHtml;
}
