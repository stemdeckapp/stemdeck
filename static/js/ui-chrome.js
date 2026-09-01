// Small UI-chrome handlers extracted from inline index.html scripts / onclick
// attributes so the Content-Security-Policy can forbid inline script (#171).
// Loaded as a module (deferred), so the DOM is parsed before this runs.

import { setSidebarCollapsed, isSidebarCollapsed } from "./catalog.js";

// Upload button → trigger the hidden file input.
document.getElementById("uploadFileBtn")?.addEventListener("click", () => {
  document.getElementById("fileInput")?.click();
});

// Notification panel: toggle / close / close-on-outside-click.
const notifBtn = document.getElementById("notifBtn");
const notifWrap = notifBtn?.closest(".daw-notif-wrap");

function setNotifOpen(open) {
  notifWrap?.classList.toggle("open", open);
  notifBtn?.setAttribute("aria-expanded", String(open));
}

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

  wireAllToggle(app, toggles.map((btn) => btn.dataset.panel), apply, persist);
}

// "All" clears the studio down to the mixer in one press, and brings it back
// in one more. Clearing the three panels while the library still holds its
// column barely changes what you see, which is what made a fourth button
// worth having rather than three presses.
//
// It keeps no state of its own: it drives the same apply/persist the
// individual toggles use, and reads its own pressed state back off .app. A
// fourth flag would be a fourth thing to disagree with the other three the
// moment the library was collapsed from its own button instead.
function wireAllToggle(app, names, apply, persist) {
  const btn = document.querySelector(".daw-panel-toggle[data-panel-all]");
  if (!btn) return;

  const everythingShown = () =>
    names.every((name) => !app.classList.contains(`panel-${name}-off`)) && !isSidebarCollapsed();

  const sync = () => btn.setAttribute("aria-pressed", String(everythingShown()));

  btn.addEventListener("click", () => {
    // Anything hidden means the press is asking for everything back.
    const show = !everythingShown();
    for (const name of names) {
      apply(name, show);
      persist(name, show);
    }
    setSidebarCollapsed(!show);
  });

  // The panels and the sidebar can each be moved from their own controls, and
  // both land as a class on .app, so watching that one attribute keeps this
  // button honest without every other handler having to remember it exists.
  new MutationObserver(sync).observe(app, { attributes: true, attributeFilter: ["class"] });
  sync();
}

wirePanelToggles();
