// What the desktop app does with a press on a link that leaves the app.
//
// In the Tauri webview there is no second tab to open into, so a link left to
// its own devices either opens a bare webview window or navigates the app away
// from StemDeck. main.js therefore sends such links to the shell's open_url.
// It listened to `click` only. A middle-click does not fire `click`; it fires
// `auxclick` with button 1, so the webview was free to do its own default.
// Ctrl, Cmd and Shift clicks do fire `click`, and are handled like a plain one.
//
// The decision lives here, free of the DOM beyond `closest`, so it runs under
// node (tests/js/external-links.test.mjs). main.js owns the side effects.

/** Anchors that leave the app and are handed to the shell. */
export const EXTERNAL_ANCHOR = 'a[target="_blank"]';

/** The stem download links in the lane headers. */
export const LANE_DOWNLOAD = "a.lane-dl";

/** open_url in the shell accepts only these; so does the check here. */
export function isWebUrl(href) {
  return typeof href === "string" && /^https?:\/\//i.test(href);
}

/** Whether this press is one the handler acts on: the primary or middle button. */
export function isLinkPress(type, button) {
  if (type === "click") return true;
  return type === "auxclick" && button === 1;
}

/**
 * What to do with a press on `target`.
 *
 * Returns null when the page should be left alone (not a handled press, no
 * shell to hand the link to, or not an external link). Otherwise
 * `{ action, url?, filename? }` where action is:
 *   "save"  invoke save_audio_file for a stem download
 *   "open"  invoke open_url with an http(s) address
 *   "block" cancel the default and do nothing (an address the shell refuses)
 * Every action also means preventDefault.
 *
 * @param {{ type: string, button?: number, target: any, hasShell: boolean }} press
 */
export function externalLinkAction({ type, button = 0, target, hasShell }) {
  // In a plain browser the browser's own handling is the right one.
  if (!hasShell || !isLinkPress(type, button)) return null;
  const closest = target && typeof target.closest === "function" ? target : null;
  if (!closest) return null;

  const dl = closest.closest(LANE_DOWNLOAD);
  if (dl?.href) {
    // A download attribute is meaningless to the OS handler, so open_url used to
    // hand the stem to a browser or media player instead of saving it (#336).
    // Placeholder rows for absent stems keep href="#" and carry no name.
    if (dl.download && !dl.getAttribute("href").endsWith("#")) {
      return { action: "save", url: dl.href, filename: dl.download };
    }
    return isWebUrl(dl.href) ? { action: "open", url: dl.href } : { action: "block" };
  }

  const anchor = closest.closest(EXTERNAL_ANCHOR);
  if (anchor?.href) {
    return isWebUrl(anchor.href) ? { action: "open", url: anchor.href } : { action: "block" };
  }
  return null;
}
