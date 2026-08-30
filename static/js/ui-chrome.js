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
      try {
        localStorage.setItem(PANEL_STORE_PREFIX + name, next ? "1" : "0");
      } catch (e) {
        console.warn("[panels] could not persist state:", e);
      }
    });
  }
}

wirePanelToggles();
