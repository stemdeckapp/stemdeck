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
