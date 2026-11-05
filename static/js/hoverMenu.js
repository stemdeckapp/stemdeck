// Hover-to-open for a button and its menu (the Mode menu in modeUi.js).
// Pure: no DOM, no timers. The caller feeds events and acts on the returned
// action, so the rules can be tested in node.
//
// The rules:
// - A mouse or pen entering the button opens the menu ("hover" open).
// - Leaving both the button and the menu closes a hover-opened menu after
//   HOVER_CLOSE_MS, so the gap between them never closes it. Entering either
//   again cancels that close.
// - A click on a closed menu opens it ("click" open, as before; touch and the
//   keyboard only ever take this path). A click on a hover-opened menu pins it
//   open (it no longer closes on leave), so the click that follows a hover
//   never shuts what the hover just opened. A click on a pinned or
//   click-opened menu closes it.
// - "closed" reports a close from elsewhere (Escape, a click outside, an
//   option chosen), so the state follows it.

export const HOVER_CLOSE_MS = 200;

export function initialHoverMenu() {
  return { open: false, by: null, overButton: false, overMenu: false };
}

/**
 * @param {{open: boolean, by: null|"hover"|"click", overButton: boolean, overMenu: boolean}} s
 * @param {{type: "enter-button"|"leave-button"|"enter-menu"|"leave-menu"|"click"|"timer"|"closed", allowOpen?: boolean}} ev
 *   allowOpen on "enter-button": false while hover must not open (the Guide is on).
 * @returns {{state: object, action: "open-hover"|"open-click"|"pin"|"close"|"schedule-close"|"cancel-close"|"none"}}
 */
export function hoverMenuStep(s, ev) {
  const next = { ...s };
  const outside = () => !next.overButton && !next.overMenu;
  switch (ev.type) {
    case "enter-button":
      next.overButton = true;
      if (!s.open) {
        if (ev.allowOpen === false) return { state: next, action: "none" };
        next.open = true;
        next.by = "hover";
        next.overMenu = false;
        return { state: next, action: "open-hover" };
      }
      return { state: next, action: "cancel-close" };
    case "enter-menu":
      next.overMenu = true;
      return { state: next, action: s.open ? "cancel-close" : "none" };
    case "leave-button":
    case "leave-menu":
      if (ev.type === "leave-button") next.overButton = false;
      else next.overMenu = false;
      if (next.open && next.by === "hover" && outside()) return { state: next, action: "schedule-close" };
      return { state: next, action: "none" };
    case "click":
      if (!s.open) {
        next.open = true;
        next.by = "click";
        next.overMenu = false;
        return { state: next, action: "open-click" };
      }
      if (s.by === "hover") {
        next.by = "click";
        return { state: next, action: "pin" };
      }
      next.open = false;
      next.by = null;
      return { state: next, action: "close" };
    case "timer":
      if (next.open && next.by === "hover" && outside()) {
        next.open = false;
        next.by = null;
        return { state: next, action: "close" };
      }
      return { state: next, action: "none" };
    case "closed":
      // A hidden menu cannot be under the pointer.
      next.open = false;
      next.by = null;
      next.overMenu = false;
      return { state: next, action: "none" };
    default:
      return { state: next, action: "none" };
  }
}
