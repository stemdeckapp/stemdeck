// The Mode control in the sidebar rail, above Favorites: a brain and the word
// Mode. It opens a small menu of the modes (mode.js): Practice and Advanced to
// choose from, Karaoke and Performance (id "streamer"; karaoke/karaoke.js,
// streamer/streamer.js: views over whichever of the two is in force; this
// menu is the only way into them).
//
// Keyboard: Enter or Space opens the menu with the current mode focused,
// Up and Down move, Enter or Space chooses, Escape closes and returns focus.

import { t, onLanguageChange } from "./i18n.js";
import { MODES, AVAILABLE, getMode, setMode, onModeChange } from "./mode.js";
import { isRecording, closeRecordPanel } from "./capture/recordUi.js";
import { armedTracks, setArmed } from "./capture/takeLanes.js";
import { openKaraoke, closeKaraoke, isKaraokeOpen, onKaraokeChange } from "./karaoke/karaoke.js";
import { openStreamer, closeStreamer, isStreamerOpen, onStreamerChange, isStreamerTakeOpen } from "./streamer/streamer.js";

// Karaoke and Streamer are not modes of mode.js: they are views over Practice
// or Advanced, opened and closed on their own, one at a time. The menu offers
// them, and shows the open one as current.
const SELECTABLE = [...AVAILABLE, "karaoke", "streamer"];
const current = () => (isKaraokeOpen() ? "karaoke" : isStreamerOpen() ? "streamer" : getMode());

let btn = null;
let menu = null;
let note = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function items() {
  return menu ? [...menu.querySelectorAll(".mode-item")] : [];
}

function paint() {
  if (!btn) return;
  const name = t(`mode.${current()}`);
  btn.title = t("mode.buttonTitle", { mode: name });
  btn.setAttribute("aria-label", t("mode.buttonTitle", { mode: name }));
  for (const item of items()) {
    const m = item.dataset.mode;
    const on = m === current();
    item.setAttribute("aria-checked", String(on));
    item.querySelector(".mode-item-name").textContent = t(`mode.${m}`);
    item.querySelector(".mode-item-desc").textContent = t(`mode.${m}Desc`);
    const tag = item.querySelector(".mode-item-tag");
    if (tag) tag.textContent = t("mode.experimental");
  }
  if (menu) menu.setAttribute("aria-label", t("mode.menuAria"));
}

function build() {
  menu = el("div", "mode-menu");
  menu.id = "modeMenu";
  menu.setAttribute("role", "menu");
  menu.hidden = true;
  for (const m of MODES) {
    const item = el("button", "mode-item");
    item.type = "button";
    item.dataset.mode = m;
    item.setAttribute("role", "menuitemradio");
    item.tabIndex = -1;
    const head = el("span", "mode-item-head");
    head.append(el("span", "mode-item-check"), el("span", "mode-item-name"));
    // Practice is the settled mode; the other three are new in 0.21.0 (#810).
    if (m !== "practice") head.append(el("span", "mode-item-tag"));
    item.append(head, el("span", "mode-item-desc"));
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      choose(m);
    });
    menu.append(item);
  }
  note = el("p", "mode-note");
  note.setAttribute("role", "status");
  note.hidden = true;
  menu.append(note);
  menu.addEventListener("keydown", onMenuKey);
  menu.addEventListener("click", (e) => e.stopPropagation());
  document.body.append(menu);
}

function choose(m) {
  if (!SELECTABLE.includes(m)) return;
  if (m === "karaoke") {
    if (isKaraokeOpen() || openKaraoke()) { close(); return; }
    // Refused: a take is recording.
    note.textContent = t("karaoke.stopRecordingFirst");
    note.hidden = false;
    return;
  }
  if (m === "streamer") {
    if (isStreamerOpen() || openStreamer()) { close(); return; }
    // Refused: a take is recording.
    note.textContent = t("streamer.stopRecordingFirst");
    note.hidden = false;
    return;
  }
  // A Streamer take is open: leaving Streamer would stop it with nothing on
  // screen to say so. Refused, as Karaoke is while a take records.
  if (isStreamerOpen() && isStreamerTakeOpen()) {
    note.textContent = t("streamer.rec.stopFirst");
    note.hidden = false;
    return;
  }
  // Practice or Advanced while a view is open: back to the studio, in that mode.
  if (isKaraokeOpen()) closeKaraoke();
  if (isStreamerOpen()) closeStreamer();
  if (m === getMode()) { close(); return; }
  // Practice has no Record button: leaving DAW while a take records would
  // leave nothing on screen to stop it with.
  if (m === "practice" && isRecording()) {
    note.textContent = t("mode.stopRecordingFirst");
    note.hidden = false;
    return;
  }
  setMode(m);
  close();
}

function open() {
  if (!menu) build();
  paint();
  note.hidden = true;
  const r = btn.getBoundingClientRect();
  menu.hidden = false;
  menu.style.left = `${Math.round(r.right + 6)}px`;
  const top = Math.min(r.top, window.innerHeight - menu.offsetHeight - 8);
  menu.style.top = `${Math.max(8, Math.round(top))}px`;
  btn.setAttribute("aria-expanded", "true");
  const focused = items().find((i) => i.dataset.mode === current()) || items()[0];
  focused?.focus();
}

function close({ focusButton = false } = {}) {
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  btn.setAttribute("aria-expanded", "false");
  if (focusButton) btn.focus();
}

function onMenuKey(e) {
  const list = items();
  const i = list.indexOf(document.activeElement);
  if (e.key === "Escape") {
    e.preventDefault();
    close({ focusButton: true });
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const step = e.key === "ArrowDown" ? 1 : -1;
    list[(i + step + list.length) % list.length]?.focus();
  } else if (e.key === "Tab") {
    close();
  }
  // Keys in the menu are the menu's: Space must not start playback.
  e.stopPropagation();
}

// Leaving DAW: nothing it owns may keep running with no control on screen.
// The recording panel closes (which turns its camera and input off) and the
// armed tracks are disarmed (an armed track holds its input open). The EQ,
// the master and the lanes follow the mode in their own modules.
function onLeaveDaw() {
  closeRecordPanel();
  for (const track of armedTracks()) setArmed(track.id, false);
}

export function initModeUi() {
  btn = document.getElementById("modeBtn");
  if (!btn) return;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu && !menu.hidden) close();
    else open();
  });
  btn.addEventListener("keydown", (e) => {
    if (e.code === "Space" || e.key === "Enter") e.stopPropagation();
  });
  document.addEventListener("click", () => close());
  window.addEventListener("resize", () => close());
  onModeChange((m) => {
    if (m === "practice") onLeaveDaw();
    paint();
  });
  onLanguageChange(paint);
  onKaraokeChange(paint);
  onStreamerChange(paint);
  paint();
}
