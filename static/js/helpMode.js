// Help mode: Guide in the rail, below Trash, turns the studio into its own manual.
// Pointing at any control listed in HELP, or clicking it, outlines it and shows
// what it does; a pill at the bottom says help mode is on. Clicks are taken by help mode while it is on,
// so exploring never starts an extraction or deletes a song. Esc or Guide
// again turns it off.
//
// The box is titled with the control's own label, which the rest of the app
// already translates, and its text comes from the help.* keys in i18n.js.

import { t } from "./i18n.js";

/** Lane controls name their instrument in the text ("Silences the Drums"). */
const laneName = (el) => el.closest(".mx-row, .lane-header")?.querySelector(".mx-name")?.textContent.trim() || "";
const chipName = (el) => el.textContent.trim();
/** The instrument a presence card measures, by its own name ("Drums"), not
 *  the card's heading ("Drum intensity"), so the sentence reads naturally. */
const presenceName = (el) => t(`stem.${el.dataset.stem}`);

// Order matters only where selectors overlap: the first match wins.
const HELP = [
  ["#helpModeBtn", "help.helpMode"],
  [".daw-brand a", "help.website"],
  ["#uploadFileBtn", "help.upload"],
  ["#url", "help.search"],
  ["#stemAllBtn", "help.stemAll"],
  [".stem-choice[data-stem]", "help.stemChip", chipName],
  ['.vocal-mode-btn[data-mode="all"]', "help.vocalCombined"],
  ['.vocal-mode-btn[data-mode="split"]', "help.vocalSplit"],
  ["#autoSectionsBtn", "help.detectStructure"],
  ['.daw-panel-toggle[data-panel="analysis"]', "help.analysisToggle"],
  ['.daw-panel-toggle[data-panel="sections"]', "help.sectionsToggle"],
  ["#submit", "help.extract"],
  ["#fav-btn", "help.favorite"],
  ["#np-details-btn", "help.songDetails"],
  ["#sidebarCollapseBtn", "help.toggleLibrary"],
  [".rail-favorites", "help.favorites"],
  [".rail-library", "help.library"],
  [".rail-lyrics", "help.lyrics"],
  [".rail-trash", "help.trash"],
  [".rail-queue", "help.queue"],
  ["#notifBtn", "help.notifications"],
  ["#friendsBtn", "help.recommend"],
  ["#settingsBtn", "settings"],
  ["#aboutBtn", "help.about"],
  ["#catalogSearch", "help.searchLibrary"],
  ["#newFolderBtn", "help.newFolder"],
  ["#catalogList .cat-item.in-trash", "help.trashedSong"],
  ["#catalogList .cat-item", "help.song"],
  ["#sectionsAddBtn", "help.addSection"],
  [".mx-fader", "help.volume", laneName],
  [".lane-key-step.up", "help.laneKeyUp", laneName],
  [".lane-key-step.down", "help.laneKeyDown", laneName],
  [".lane-key-value", "help.laneKey", laneName, "help.laneKeyTitle"],
  [".mx-btn.mute", "help.mute", laneName],
  [".mx-btn.solo", "help.solo", laneName],
  [".lane-dl", "help.download", laneName],
  ['.daw-meta-card[data-meta="key"]', "help.meta.key"],
  ['.daw-meta-card[data-meta="bpm"]', "help.meta.bpm"],
  ['.daw-meta-card[data-meta="lufs"]', "help.meta.lufs"],
  ['.daw-meta-card[data-meta="duration"]', "help.meta.duration"],
  ['.daw-meta-card[data-meta="dr"]', "help.meta.dr"],
  ['.daw-meta-card[data-meta="stability"]', "help.meta.stability"],
  [".stem-presence-panel .stem-card[data-stem]", "help.presence", presenceName],
  ["#ruler-time", "help.waves", null, "help.wavesTitle"],
  [".waves-column", "help.waves", null, "help.wavesTitle"],
  ["#t-stop", "help.stop"],
  ["#t-play", "help.play"],
  ["#t-loop", "help.loop"],
  ["#t-loop-start", "help.loopStart"],
  ["#t-loop-end", "help.loopEnd"],
  ["#t-speed-05", "help.speedHalf"],
  ["#t-speed-075", "help.speedSlow"],
  ["#t-speed-1", "help.speedNormal"],
  ["#t-pitch-down", "help.keyDown"],
  ["#t-pitch-up", "help.keyUp"],
  ["#t-pitch-reset", "help.keyReset"],
  ["#t-metro", "help.click"],
  ["#t-metro-vol-btn", "help.clickVolume"],
  ["#t-metro-more", "help.clickMore"],
  ["#t-export-btn", "help.export"],
];

// Every option in Settings, by tab: the tab and option names are the dialog's
// own keys, the one-line descriptions are help.set.*.
const SETTINGS = [
  ["settings.tab.general", [
    ["settings.language.title", "help.set.language"],
    ["settings.maxDuration.title", "help.set.maxDuration"],
    ["settings.playlistLimit.title", "help.set.playlistLimit"],
    ["settings.cookies.title", "help.set.cookies"],
    ["settings.stemsLocation.title", "help.set.stemsLocation"],
    ["settings.exportsLocation.title", "help.set.exportsLocation"],
    ["settings.autoDelete.title", "help.set.autoDelete"],
    ["settings.device.title", "help.set.device"],
    ["settings.quality.title", "help.set.quality"],
    ["settings.exportLogs.title", "help.set.exportLogs"],
    ["settings.resetData.title", "help.set.resetData"],
  ]],
  ["settings.tab.songDetails", [
    ["settings.acoustid.title", "help.set.acoustid"],
    ["settings.discogs.title", "help.set.discogs"],
    ["settings.transcribe.title", "help.set.transcribe"],
  ]],
  ["settings.tab.network", [
    ["settings.network.allowTitle", "help.set.network"],
    ["settings.network.port.title", "help.set.port"],
  ]],
  ["settings.tab.export", [
    ["settings.export.sampleRate.title", "help.set.sampleRate"],
    ["settings.export.videoQuality.title", "help.set.videoQuality"],
  ]],
  ["settings.tab.logs", [[null, "help.set.logs"]]],
  ["settings.tab.registry", [[null, "help.set.registry"]]],
];

let on = false;
let tipEl = null;
let pillEl = null;
let ringEl = null;
let introEl = null;
let introTimer = null;
// How long the glow on every explainable control lasts when Guide turns on.
// daw.css reads it through --intro-ms, so the fade and the cleanup agree.
const INTRO_MS = 1600;
let current = null;

export function isHelpModeOn() {
  return on;
}

/** The help entry for whatever is under the pointer, and the element it is for. */
function findEntry(target) {
  if (!(target instanceof Element)) return null;
  for (const [sel, key, nameOf, titleKey] of HELP) {
    const el = target.closest(sel);
    if (el && isVisible(el)) return { el, key, nameOf, titleKey };
  }
  return null;
}

function isVisible(el) {
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

/** The control's own label, which the app already translates. Visible text
 *  first, unless it is a letter or a sign (M, S, +): a heading that repeats
 *  "M" teaches nothing, so those take their full label ("Mute Drums"). */
function titleOf(entry) {
  const { el } = entry;
  if (entry.titleKey) return t(entry.titleKey, { name: entry.nameOf ? entry.nameOf(el) : "" });
  const text = (el.innerText || "").trim().split("\n")[0].trim();
  if (text.length > 2 && text.length <= 40) return text;
  return el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("title")
    || el.querySelector("[aria-label]")?.getAttribute("aria-label") || text;
}

function fillTip(entry) {
  tipEl.replaceChildren();
  tipEl.classList.toggle("long", entry.key === "settings");
  const title = document.createElement("b");
  title.textContent = titleOf(entry);
  tipEl.append(title);
  if (entry.key !== "settings") {
    const name = entry.nameOf ? entry.nameOf(entry.el) : "";
    tipEl.append(t(entry.key, { name }));
    return;
  }
  for (const [tabKey, rows] of SETTINGS) {
    const h = document.createElement("div");
    h.className = "help-tip-h";
    h.textContent = t(tabKey);
    tipEl.append(h);
    for (const [nameKey, descKey] of rows) {
      const row = document.createElement("div");
      row.className = "help-tip-row";
      if (nameKey) {
        const n = document.createElement("span");
        n.textContent = t(nameKey);
        row.append(n, " ");
      }
      row.append(t(descKey));
      tipEl.append(row);
    }
  }
}

/**
 * What the eye sees of a control. A button is often much bigger than the icon
 * in it (the heart is a 76px tall strip around a 16px icon), and a ring around
 * the whole box reads as a stray bar. When the content is clearly smaller than
 * the box, the ring goes around the content instead.
 */
function visibleRect(el) {
  const r = el.getBoundingClientRect();
  const range = document.createRange();
  range.selectNodeContents(el);
  const c = range.getBoundingClientRect();
  const small = c.width > 0 && c.height > 0 && (c.width < r.width * 0.6 || c.height < r.height * 0.6);
  // Otherwise the box and its content together: a rail label can hang below
  // its button's own box, and the ring has to take it in.
  const box = small ? c : c.width > 0 && c.height > 0
    ? { left: Math.min(r.left, c.left), top: Math.min(r.top, c.top), right: Math.max(r.right, c.right), bottom: Math.max(r.bottom, c.bottom) }
    : r;
  const pad = small ? 6 : 3;
  const left = box.left - pad, top = box.top - pad, right = box.right + pad, bottom = box.bottom + pad;
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

/** The ring is its own layer over the page, so no panel can clip it. */
function placeRing(rect) {
  ringEl.style.left = `${Math.round(rect.left)}px`;
  ringEl.style.top = `${Math.round(rect.top)}px`;
  ringEl.style.width = `${Math.round(rect.right - rect.left)}px`;
  ringEl.style.height = `${Math.round(rect.bottom - rect.top)}px`;
  ringEl.hidden = false;
}

/** Beside the control, on whichever side has room, never over it. */
function placeTip(r) {
  const tr = tipEl.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight, gap = 10, m = 8;
  const fits = (x, y) => x >= m && y >= m && x + tr.width <= vw - m && y + tr.height <= vh - m;
  const cx = Math.min(Math.max(m, r.left + r.width / 2 - tr.width / 2), vw - tr.width - m);
  const cy = Math.min(Math.max(m, r.top + r.height / 2 - tr.height / 2), vh - tr.height - m);
  const options = [
    [r.right + gap, cy],
    [r.left - gap - tr.width, cy],
    [cx, r.bottom + gap],
    [cx, r.top - gap - tr.height],
  ];
  // A large area, like the waveforms, takes the box inside its top edge.
  const [x, y] = options.find(([x, y]) => fits(x, y)) ?? [cx, Math.max(m, Math.min(r.top + 12, vh - tr.height - m))];
  tipEl.style.left = `${Math.round(x)}px`;
  tipEl.style.top = `${Math.round(y)}px`;
}

function show(entry) {
  if (current?.el === entry.el) return;
  hide();
  current = entry;
  entry.el.classList.add("help-current");
  fillTip(entry);
  tipEl.hidden = false;
  const rect = visibleRect(entry.el);
  placeRing(rect);
  placeTip(rect);
}

function hide() {
  current?.el.classList.remove("help-current");
  current = null;
  if (tipEl) tipEl.hidden = true;
  if (ringEl) ringEl.hidden = true;
}

/** Mark everything help mode can explain, including lanes added since. */
function markTargets() {
  for (const el of document.querySelectorAll(".help-target")) el.classList.remove("help-target");
  if (!on) return;
  for (const [sel] of HELP) {
    for (const el of document.querySelectorAll(sel)) if (isVisible(el)) el.classList.add("help-target");
  }
}

/**
 * When Guide turns on, every control it can explain glows once and fades, so
 * the user sees where to point without the screen staying busy. One ring per
 * control, drawn the same way as the pointer's ring, so no panel clips them.
 * Large areas (the waveforms, the song facts) are left out: a glowing box
 * across half the screen says nothing about where to point.
 */
function playIntro() {
  stopIntro();
  const vw = window.innerWidth, vh = window.innerHeight;
  const big = vw * vh * 0.04;
  for (const el of document.querySelectorAll(".help-target")) {
    const r = visibleRect(el);
    if (r.width * r.height > big || r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
    const ring = document.createElement("div");
    ring.className = "help-intro-ring";
    ring.style.left = `${Math.round(r.left)}px`;
    ring.style.top = `${Math.round(r.top)}px`;
    ring.style.width = `${Math.round(r.width)}px`;
    ring.style.height = `${Math.round(r.height)}px`;
    introEl.appendChild(ring);
  }
  introEl.hidden = false;
  introTimer = setTimeout(stopIntro, INTRO_MS);
}

function stopIntro() {
  clearTimeout(introTimer);
  introTimer = null;
  if (!introEl) return;
  introEl.hidden = true;
  introEl.replaceChildren();
}

function onPointerOver(e) {
  const entry = findEntry(e.target);
  if (entry) show(entry);
  else if (!tipEl.contains(e.target)) hide();
}

// Help mode takes every press except Guide itself, so pointing around the
// studio can never act on it. A click shows that control's box, which is also
// how help mode works on a touch screen.
function swallow(e) {
  if (e.target instanceof Element && e.target.closest("#helpModeBtn")) return;
  e.preventDefault();
  e.stopPropagation();
  if (e.type === "click") {
    const entry = findEntry(e.target);
    if (entry) show(entry);
    else hide();
  }
}

function onFocus(e) {
  const entry = findEntry(e.target);
  if (entry) show(entry);
}

function onKey(e) {
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    setHelpMode(false);
  }
}

const PRESS_EVENTS = ["pointerdown", "mousedown", "click", "dblclick", "dragstart", "contextmenu"];

export function setHelpMode(next) {
  if (next === on) return;
  on = next;
  document.body.classList.toggle("help-mode", on);
  if (pillEl) {
    pillEl.textContent = t("help.pill");
    pillEl.hidden = !on;
  }
  const btn = document.getElementById("helpModeBtn");
  btn?.setAttribute("aria-pressed", String(on));
  if (on) {
    document.addEventListener("pointerover", onPointerOver, true);
    for (const type of PRESS_EVENTS) document.addEventListener(type, swallow, true);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("focusin", onFocus, true);
    window.addEventListener("resize", hide);
    document.addEventListener("scroll", hide, true);
    markTargets();
    playIntro();
  } else {
    document.removeEventListener("pointerover", onPointerOver, true);
    for (const type of PRESS_EVENTS) document.removeEventListener(type, swallow, true);
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("focusin", onFocus, true);
    window.removeEventListener("resize", hide);
    document.removeEventListener("scroll", hide, true);
    hide();
    stopIntro();
    markTargets();
  }
}

export function initHelpMode() {
  const btn = document.getElementById("helpModeBtn");
  if (!btn || btn.dataset.ready === "1") return;
  btn.dataset.ready = "1";
  tipEl = document.createElement("div");
  tipEl.className = "help-tip";
  tipEl.id = "helpTip";
  tipEl.setAttribute("role", "tooltip");
  tipEl.hidden = true;
  pillEl = document.createElement("div");
  pillEl.className = "help-pill";
  pillEl.setAttribute("role", "status");
  pillEl.hidden = true;
  ringEl = document.createElement("div");
  ringEl.className = "help-ring";
  ringEl.setAttribute("aria-hidden", "true");
  ringEl.hidden = true;
  introEl = document.createElement("div");
  introEl.className = "help-intro";
  introEl.setAttribute("aria-hidden", "true");
  introEl.style.setProperty("--intro-ms", `${INTRO_MS}ms`);
  introEl.hidden = true;
  document.body.append(introEl, ringEl, tipEl, pillEl);
  btn.addEventListener("click", () => setHelpMode(!on));
}
