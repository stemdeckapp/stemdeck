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

// "All" clears the three panels in one press and brings them back in one more.
//
// Deliberately not the library. It used to take that too, and the row could
// not put it back: the three buttons beside it only know about their own
// panels, so after an "All" press you could turn Analysis, Sections and
// Timeline back on, watch "All" light up as though everything had returned,
// and still be looking at a collapsed library with nothing in this row able to
// reach it (#588). The library has its own control in the rail, which is where
// a reader of this row would not think to look for it.
//
// It greys and strikes through like the other three once all three are away.
// The threshold is the whole trick. Read as "is anything hidden", it struck
// itself through the moment Analysis was hidden on its own, which looks exactly
// like you pressed it. It is off only when every panel it governs is off, so it
// describes the row rather than the loudest thing in it.
//
// It stores nothing. The state is derived from .app either way, so there is no
// fourth flag to disagree with the other three.
function wireAllToggle(app, names, apply, persist) {
  const btn = document.querySelector(".daw-panel-toggle[data-panel-all]");
  if (!btn) return;

  const hiddenCount = () =>
    names.filter((name) => app.classList.contains(`panel-${name}-off`)).length;

  const sync = () => btn.setAttribute("aria-pressed", String(hiddenCount() < names.length));

  btn.addEventListener("click", () => {
    // Anything still on screen means the press is asking to clear it away.
    const show = hiddenCount() === names.length;
    for (const name of names) {
      apply(name, show);
      persist(name, show);
    }
  });

  // Each panel can also be moved from its own button, and they all land as a
  // class on .app, so watching that one attribute keeps this button honest
  // without every other handler having to remember it exists. The library's
  // class lands there too and is ignored, which is the point.
  new MutationObserver(sync).observe(app, { attributes: true, attributeFilter: ["class"] });
  sync();
}

wirePanelToggles();
