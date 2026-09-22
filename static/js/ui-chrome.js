// Small UI-chrome handlers extracted from inline index.html scripts / onclick
// attributes so the Content-Security-Policy can forbid inline script (#171).
// Loaded as a module (deferred), so the DOM is parsed before this runs.

// Upload button → trigger the hidden file input.
document.getElementById("uploadFileBtn")?.addEventListener("click", () => {
  document.getElementById("fileInput")?.click();
});

// Notification panel: toggle / close / close-on-outside-click.
const notifBtn = document.getElementById("notifBtn");
const notifWrap = notifBtn?.closest(".daw-notif-wrap");
const notifPanel = notifWrap?.querySelector(".daw-notif-panel");

/**
 * Put the panel beside the bell, top edges level.
 *
 * It cannot simply hang off .daw-notif-wrap. The bell lives in the library
 * rail, .sidebar is overflow:hidden, and it narrows to the rail's 66px when
 * the library is collapsed, so an absolutely positioned panel is clipped to
 * nothing in that state. Fixed escapes the clip and pays for it by needing its
 * coordinates set here, which is the same trade the search dropdown and the
 * metronome popover already make.
 *
 * Measured while shown: a display:none panel has no height to clamp against.
 */
function placeNotifPanel() {
  if (!notifBtn || !notifPanel) return;
  const r = notifBtn.getBoundingClientRect();
  // Clear of the rail, not of the button: the button is 40px centred in a 66px
  // column, so measuring from its own right edge tucks the panel back under
  // the rail by the leftover margin.
  const column = notifBtn.closest(".sidebar-rail") || notifBtn;
  notifPanel.style.left = `${Math.round(column.getBoundingClientRect().right + 10)}px`;
  // Level with the bell, and only pulled up from there if the panel would
  // otherwise hang off the bottom of a short window.
  const lowest = window.innerHeight - notifPanel.offsetHeight - 12;
  notifPanel.style.top = `${Math.round(Math.max(12, Math.min(r.top, lowest)))}px`;
}

function setNotifOpen(open) {
  notifWrap?.classList.toggle("open", open);
  notifBtn?.setAttribute("aria-expanded", String(open));
  if (open) placeNotifPanel();
}

// The bell does not move with the window, but the clamp above depends on the
// window's height, so a resize while the panel is open can strand it.
window.addEventListener("resize", () => {
  if (notifWrap?.classList.contains("open")) placeNotifPanel();
});

notifBtn?.addEventListener("click", () => {
  setNotifOpen(!notifWrap?.classList.contains("open"));
});

document
  .querySelector(".daw-notif-close")
  ?.addEventListener("click", () => setNotifOpen(false));

document.addEventListener("click", (e) => {
  if (notifWrap?.classList.contains("open") && !notifWrap.contains(e.target)) {
    setNotifOpen(false);
  }
});

// Panel toggles (#480). Each one hides a region that is useful but not useful
// all the time, and hands its height to the mixer, which is the panel that
// actually runs short: at 1366x768 with six stems the lane stack needs 432px
// and gets 370. Analysis is worth 72px and the timeline 93px, so either alone
// closes that gap.
//
// Same shape as the sidebar collapse: a class on .app, a flag in localStorage,
// no state anywhere else. The lanes re-fit on their own because the wave panel
// is already watched by a ResizeObserver.
const PANEL_STORE_PREFIX = "stemdeck.panel.";

function wirePanelToggles() {
  const app = document.querySelector(".app");
  const toggles = [...document.querySelectorAll(".daw-panel-toggle[data-panel]")];
  if (!app || !toggles.length) return;

  const apply = (name, shown) => {
    app.classList.toggle(`panel-${name}-off`, !shown);
    for (const btn of toggles) {
      if (btn.dataset.panel === name) btn.setAttribute("aria-pressed", String(shown));
    }
  };

  const persist = (name, shown) => {
    try {
      localStorage.setItem(PANEL_STORE_PREFIX + name, shown ? "1" : "0");
    } catch (e) {
      console.warn("[panels] could not persist state:", e);
    }
  };

  for (const btn of toggles) {
    const name = btn.dataset.panel;
    let shown = true;
    try {
      shown = localStorage.getItem(PANEL_STORE_PREFIX + name) !== "0";
    } catch (e) {
      console.warn("[panels] could not read stored state:", e);
    }
    apply(name, shown);
    btn.addEventListener("click", () => {
      const next = app.classList.contains(`panel-${name}-off`);
      apply(name, next);
      persist(name, next);
    });
  }
}

wirePanelToggles();
