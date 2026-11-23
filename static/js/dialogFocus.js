// Focus inside the rail's modal dialogs (Donate, We Recommend): what can take
// focus, and Tab kept inside the open dialog. Opening moves focus to the
// dialog's close button, so Esc reaches the dialog's own keydown handler, and
// closing gives focus back to what opened it. Each dialog wires that part; the
// shared rules live here so the two cannot drift apart.

/** The controls in `root` that Tab can reach, in document order. */
export function focusables(root) {
  return [...root.querySelectorAll("button, [href], input, select, textarea, [tabindex]")].filter(
    (el) => el.tabIndex >= 0 && !el.disabled && !el.hidden && el.getClientRects().length > 0,
  );
}

/**
 * Keeps Tab and Shift+Tab inside `dialog`: past the last control focus goes
 * to the first, and before the first to the last. Call it from the dialog's
 * keydown handler; it ignores every other key.
 */
export function keepTabInside(dialog, e) {
  if (e.key !== "Tab") return;
  const items = focusables(dialog);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}
