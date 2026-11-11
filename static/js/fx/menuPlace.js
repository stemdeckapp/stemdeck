// Where a popup menu goes beside the button that opens it (the chain strip's
// "+ Add effect", fx/chainStrip.js). No DOM, so tests/js/performance-fx.test.mjs
// runs it in node.

// The menu's distance from its button and from the window's edges, and the
// least height it is given before it scrolls instead.
export const MENU_GAP = 4;
export const MENU_EDGE = 8;
export const MENU_MIN_H = 96;

/**
 * Where the menu goes for a button at `btn` (a rect: left, top, bottom) and a
 * menu of `w` x `h` in a `vw` x `vh` window: below the button when it fits,
 * else above when that fits, else on the roomier side with a maximum height
 * (it scrolls). Left-aligned with the button, kept inside the window.
 * Returns {left, top, maxHeight} in window pixels (maxHeight null: no limit).
 */
export function placeMenu(btn, w, h, vw, vh) {
  const below = vh - btn.bottom - MENU_GAP - MENU_EDGE;
  const above = btn.top - MENU_GAP - MENU_EDGE;
  const left = Math.max(MENU_EDGE, Math.min(btn.left, vw - w - MENU_EDGE));
  if (h <= below) return { left, top: btn.bottom + MENU_GAP, maxHeight: null };
  if (h <= above) return { left, top: btn.top - MENU_GAP - h, maxHeight: null };
  if (below >= above) return { left, top: btn.bottom + MENU_GAP, maxHeight: Math.max(MENU_MIN_H, below) };
  const room = Math.max(MENU_MIN_H, above);
  return { left, top: Math.max(MENU_EDGE, btn.top - MENU_GAP - room), maxHeight: room };
}
