// Hover-to-open for the Mode menu (static/js/hoverMenu.js): pointing opens,
// the gap between button and menu never closes it, leaving both closes it
// after the delay, a click after a hover pins it open, and touch and the
// keyboard keep the plain click toggle.
//
// Pure functions, no DOM.
//
// Run:  node tests/js/hover-menu.test.mjs

import { HOVER_CLOSE_MS, initialHoverMenu, hoverMenuStep } from "../../static/js/hoverMenu.js";

let pass = 0;
let fail = 0;
function check(name, ok) {
  if (ok) pass++;
  else { fail++; console.error(`FAIL: ${name}`); }
}

/** Feed events in order; return the final state and every action. */
function run(events, start = initialHoverMenu()) {
  let state = start;
  const actions = [];
  for (const ev of events) {
    const r = hoverMenuStep(state, typeof ev === "string" ? { type: ev } : ev);
    state = r.state;
    actions.push(r.action);
  }
  return { state, actions };
}

check("the close delay is between 150 and 250 ms", HOVER_CLOSE_MS >= 150 && HOVER_CLOSE_MS <= 250);

{
  const { state, actions } = run(["enter-button"]);
  check("entering the button opens the menu by hover", actions[0] === "open-hover" && state.open && state.by === "hover");
}

{
  const { actions } = run([{ type: "enter-button", allowOpen: false }]);
  check("entering while hover is not allowed (the Guide) does not open", actions[0] === "none");
}

{
  const { state, actions } = run(["enter-button", "leave-button", "enter-menu"]);
  check("leaving the button for the menu schedules a close, entering the menu cancels it",
    actions[1] === "schedule-close" && actions[2] === "cancel-close" && state.open);
  const t = hoverMenuStep(state, { type: "timer" });
  check("a timer that fires while the pointer is on the menu does nothing", t.action === "none" && t.state.open);
}

{
  const { state, actions } = run(["enter-button", "leave-button", "enter-button"]);
  check("coming back to the button cancels the close", actions[2] === "cancel-close" && state.open);
}

{
  const { state, actions } = run(["enter-button", "leave-button", "enter-menu", "leave-menu", "timer"]);
  check("leaving both closes after the delay", actions[3] === "schedule-close" && actions[4] === "close" && !state.open);
}

{
  const { state, actions } = run(["enter-button", "click"]);
  check("a click on a hover-opened menu pins it open", actions[1] === "pin" && state.open && state.by === "click");
  const after = run(["leave-button", "timer"], state);
  check("a pinned menu does not close when the pointer leaves", after.actions[0] === "none" && after.state.open);
  const again = hoverMenuStep(state, { type: "click" });
  check("a second click closes a pinned menu", again.action === "close" && !again.state.open);
}

{
  const { state, actions } = run(["click", "click"]);
  check("touch or keyboard: a click opens, the next click closes", actions[0] === "open-click" && actions[1] === "close" && !state.open);
}

{
  const { state } = run(["enter-button", "leave-button", "enter-menu", "closed"]);
  check("a close from elsewhere resets the state and forgets the menu under the pointer", !state.open && state.by === null && !state.overMenu);
  const reopen = run(["leave-button", "enter-button", "leave-button"], state);
  check("after a close from elsewhere, hovering opens again and leaving schedules a close",
    reopen.actions[1] === "open-hover" && reopen.actions[2] === "schedule-close");
}

{
  const { state, actions } = run(["enter-button", "click", "click"]);
  check("closing by click while still over the button does not reopen it", actions[2] === "close" && !state.open);
}

{
  const s = initialHoverMenu();
  hoverMenuStep(s, { type: "enter-button" });
  check("a step never changes the state it was given", !s.open && !s.overButton);
}

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
