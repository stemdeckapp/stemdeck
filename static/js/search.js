// Live search in the topbar.
//
// The box already takes a pasted link. Typing anything that is not a link now
// searches instead, so finding a track does not mean leaving StemDeck.
//
// Three things keep this from being expensive:
//
//   * Requests fire on a word boundary, not a keystroke. Typing "daft punk
//     around the world" costs one or two requests, not twenty six. A trailing
//     space fires sooner than a pause, which is what makes it feel live.
//   * Every request carries an AbortController, and starting one aborts the
//     last. A superseded response never lands, so results cannot arrive out of
//     order and overwrite newer ones.
//   * Rows are built into a DocumentFragment and attached once, with a single
//     delegated click listener for the whole list. Thumbnails are lazy and
//     decode off the main thread.
//
// The backend caches for 60 s, so walking backwards through a query (which
// backspacing does constantly) mostly does not hit the network at all.

import { t, applyTranslations, onLanguageChange } from "./i18n.js";

// A pause long enough to mean "stopped typing".
const IDLE_MS = 450;
// A finished word is a much better signal than a pause, so it waits far less.
const WORD_MS = 120;
const MIN_QUERY = 2;
const LIMIT = 8;
// Wide enough for a long title, narrow enough to scan down.
const MAX_PANEL_PX = 620;

const ICON_PLAY = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M6 5h4v14H6zM14 5h4v14h-4z" fill="currentColor"/></svg>';

// One <audio> for the whole panel, not one per row. preload="none" so nothing
// is fetched until a preview is actually opened, and reusing the element means
// opening a second preview releases the first connection immediately.
// The button carries a word, not just a glyph. An unlabelled circle on a
// search result reads as "play this result", which is exactly the thing it
// does not do: it auditions, it does not select.
function setPreviewButton(btn, playing) {
  if (!btn) return;
  btn.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
  const label = document.createElement("span");
  label.textContent = t(playing ? "search.pause" : "search.preview");
  btn.appendChild(label);
  btn.classList.toggle("playing", playing);
  btn.setAttribute("aria-label", label.textContent);
  btn.title = label.textContent;
}

let audio = null;
let previewIndex = -1;
let rafId = 0;

// Anything that could be a link is left alone: it belongs to the import flow,
// not to search. Deliberately loose. A false positive here costs one search
// that would have failed anyway, a false negative sends a URL to YouTube as a
// search term.
const LOOKS_LIKE_URL = /^\s*(https?:\/\/|www\.)|\b(youtube\.com|youtu\.be|soundcloud\.com)\b/i;

const TABS = [
  { source: "youtube", kind: "track", labelKey: "search.tab.ytSongs" },
  { source: "youtube", kind: "playlist", labelKey: "search.tab.ytPlaylists" },
  { source: "soundcloud", kind: "track", labelKey: "search.tab.scSongs" },
];

let panel = null;
let listEl = null;
let statusEl = null;
let tabsEl = null;
let input = null;
let onPick = null;

let timerId = null;
let controller = null;
let activeTab = 0;
let items = [];
let cursor = -1;
let lastQuery = "";

const fmtDuration = (secs) => {
  if (typeof secs !== "number" || !isFinite(secs) || secs <= 0) return "";
  const total = Math.round(secs);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
};

function position() {
  // Anchored to the composer, not the input, so the panel lines up with the
  // pill rather than with the text cursor inside it.
  const anchor = input.closest(".daw-composer") || input;
  const r = anchor.getBoundingClientRect();
  // Capped rather than full composer width. On a wide window the composer is
  // over 1200px, and a result list that wide leaves the title stranded next to
  // a 64px thumbnail with a screen of empty space after it.
  const width = Math.min(r.width, MAX_PANEL_PX);
  panel.style.left = `${Math.round(r.left)}px`;
  panel.style.top = `${Math.round(r.bottom + 6)}px`;
  panel.style.width = `${Math.round(width)}px`;
}

function open() {
  position();
  panel.classList.remove("hidden");
  input.setAttribute("aria-expanded", "true");
}

function close() {
  // Nothing is more jarring than a dropdown that vanishes and keeps playing.
  stopPreview();
  if (timerId) { clearTimeout(timerId); timerId = null; }
  controller?.abort();
  controller = null;
  items = [];
  cursor = -1;
  lastQuery = "";
  panel?.classList.add("hidden");
  input?.setAttribute("aria-expanded", "false");
  input?.removeAttribute("aria-activedescendant");
}

function setStatus(text) {
  stopPreview();
  listEl.textContent = "";
  statusEl.textContent = text;
  statusEl.classList.toggle("hidden", !text);
}

function render(maxDurationSec) {
  // The rows about to be destroyed own the running preview.
  stopPreview();
  statusEl.classList.add("hidden");
  listEl.textContent = "";
  if (!items.length) {
    setStatus(t("search.noResults"));
    return;
  }
  const mins = Math.round((maxDurationSec || 1200) / 60);
  // One fragment, one insertion. Appending rows individually would lay out the
  // panel once per row.
  const frag = document.createDocumentFragment();
  items.forEach((item, i) => {
    const row = document.createElement("li");
    row.className = "search-row";
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", "false");
    row.id = `search-row-${i}`;
    row.dataset.index = String(i);
    if (item.too_long) row.classList.add("too-long");

    // The clickable part. The expanded player lives beside it in the same <li>
    // so opening a preview does not move the row.
    const main = document.createElement("div");
    main.className = "search-row-main";

    if (item.thumbnail) {
      const img = document.createElement("img");
      img.className = "search-thumb";
      img.src = item.thumbnail;
      img.alt = "";
      img.loading = "lazy";
      img.decoding = "async";
      main.appendChild(img);
    } else {
      const ph = document.createElement("span");
      ph.className = "search-thumb search-thumb-empty";
      main.appendChild(ph);
    }

    const text = document.createElement("span");
    text.className = "search-text";
    const title = document.createElement("span");
    title.className = "search-title";
    title.textContent = item.title;
    const meta = document.createElement("span");
    meta.className = "search-meta";
    const bits = [];
    if (item.uploader) bits.push(item.uploader);
    const dur = fmtDuration(item.duration);
    if (dur) bits.push(dur);
    meta.textContent = bits.join(" · ");
    text.append(title, meta);
    main.appendChild(text);

    if (item.too_long) {
      // Over the limit the pipeline refuses the job outright, so this is a
      // "cannot", not a "may be slow". Saying it here saves the user queueing
      // something that can only fail.
      const warn = document.createElement("span");
      warn.className = "search-warn";
      // Terse inline, because it shares a 620px row with a title, an uploader,
      // a duration and the Preview pill. The part the user can act on lives in
      // the tooltip rather than squeezing the title it sits next to.
      warn.textContent = t("search.tooLong", { mins });
      warn.title = t("search.tooLongHint", { mins });
      main.appendChild(warn);
    }

    // Playlists have no single stream to audition. The kind comes from the
    // active tab: results do not carry one.
    if (TABS[activeTab].kind !== "playlist") {
      const prev = document.createElement("button");
      prev.type = "button";
      prev.className = "search-preview-btn";
      prev.dataset.preview = String(i);
      setPreviewButton(prev, false);
      main.appendChild(prev);
    }

    row.appendChild(main);
    frag.appendChild(row);
  });
  listEl.appendChild(frag);
  cursor = -1;
  // Height just changed, and the panel may have been sized while it still held
  // the "Searching..." line.
  position();
}

const fmtClock = (s) => (isFinite(s) && s >= 0 ? fmtDuration(s) || "0:00" : "0:00");

function stopPreview() {
  if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
  if (audio) {
    audio.pause();
    // Drop the src as well as pausing: a paused <audio> keeps its connection
    // and buffer, which for a proxied stream means holding a socket open on
    // the backend for a preview nobody is listening to.
    audio.removeAttribute("src");
    audio.load();
  }
  previewIndex = -1;
  if (!listEl) return;
  listEl.querySelector(".search-player")?.remove();
  for (const b of listEl.querySelectorAll(".search-preview-btn")) setPreviewButton(b, false);
}

function buildPlayer(item) {
  const wrap = document.createElement("div");
  wrap.className = "search-player";

  const play = document.createElement("button");
  play.type = "button";
  play.className = "search-play";
  play.innerHTML = ICON_PAUSE;
  play.setAttribute("aria-label", t("search.pause"));

  const bar = document.createElement("input");
  bar.type = "range";
  bar.className = "search-seek";
  bar.min = "0";
  bar.max = "1000";
  bar.value = "0";
  bar.setAttribute("aria-label", t("search.seek"));

  const clock = document.createElement("span");
  clock.className = "search-clock";
  clock.textContent = `0:00 / ${fmtClock(item.duration)}`;

  wrap.append(play, bar, clock);
  return { wrap, play, bar, clock };
}

async function startPreview(index) {
  const item = items[index];
  const row = listEl.children[index];
  if (!item || !row) return;
  if (previewIndex === index) { stopPreview(); return; }
  stopPreview();
  previewIndex = index;

  if (!audio) {
    audio = new Audio();
    audio.preload = "none";
  }

  const { wrap, play, bar, clock } = buildPlayer(item);
  row.appendChild(wrap);
  const btn = row.querySelector(".search-preview-btn");
  setPreviewButton(btn, true);
  wrap.classList.add("loading");
  position();

  audio.src = `/api/search/preview?url=${encodeURIComponent(item.url)}`;

  let seeking = false;
  // A single rAF loop while playing, rather than a timeupdate listener: the
  // bar then moves with the display instead of at the 4/sec timeupdate fires.
  const tick = () => {
    if (previewIndex !== index) return;
    const dur = audio.duration || item.duration || 0;
    if (!seeking && dur) bar.value = String(Math.round((audio.currentTime / dur) * 1000));
    clock.textContent = `${fmtClock(audio.currentTime)} / ${fmtClock(dur)}`;
    rafId = requestAnimationFrame(tick);
  };

  const setPaused = (paused) => {
    play.innerHTML = paused ? ICON_PLAY : ICON_PAUSE;
    play.setAttribute("aria-label", t(paused ? "search.play" : "search.pause"));
    setPreviewButton(btn, !paused);
  };

  audio.onplaying = () => { wrap.classList.remove("loading"); setPaused(false); if (!rafId) tick(); };
  audio.onpause = () => { setPaused(true); if (rafId) { cancelAnimationFrame(rafId); rafId = 0; } };
  audio.onended = () => { setPaused(true); bar.value = "0"; };
  audio.onerror = () => {
    wrap.classList.remove("loading");
    wrap.classList.add("failed");
    clock.textContent = t("search.previewFailed");
  };

  play.addEventListener("click", (e) => {
    e.stopPropagation();
    if (audio.paused) audio.play().catch(() => {}); else audio.pause();
  });
  bar.addEventListener("pointerdown", () => { seeking = true; });
  const commit = () => {
    const dur = audio.duration || item.duration || 0;
    if (dur) audio.currentTime = (Number(bar.value) / 1000) * dur;
    seeking = false;
  };
  bar.addEventListener("change", commit);
  bar.addEventListener("pointerup", commit);
  // The player is inside the row, and the row picks the track on mousedown.
  wrap.addEventListener("mousedown", (e) => e.stopPropagation());
  wrap.addEventListener("click", (e) => e.stopPropagation());

  try { await audio.play(); } catch { /* autoplay refused: the play button still works */ }
}

function highlight(next) {
  const rows = listEl.children;
  if (!rows.length) return;
  if (cursor >= 0 && rows[cursor]) {
    rows[cursor].classList.remove("active");
    rows[cursor].setAttribute("aria-selected", "false");
  }
  input.removeAttribute("aria-activedescendant");
  cursor = (next + rows.length) % rows.length;
  const row = rows[cursor];
  row.classList.add("active");
  row.setAttribute("aria-selected", "true");
  input.setAttribute("aria-activedescendant", row.id);
  row.scrollIntoView({ block: "nearest" });
}

function pick(index) {
  const item = items[index];
  if (!item || item.too_long) return;
  close();
  onPick?.(item);
}

async function run(query) {
  const tab = TABS[activeTab];
  controller?.abort();
  controller = new AbortController();
  const mine = controller;
  setStatus(t("search.searching"));
  open();
  try {
    const r = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, source: tab.source, kind: tab.kind, limit: LIMIT }),
      signal: mine.signal,
    });
    // A newer search started while this was in flight. Its result is the only
    // one that should reach the DOM.
    if (mine.signal.aborted || mine !== controller) return;
    if (!r.ok) { items = []; setStatus(t("search.failed")); lastQuery = ""; return; }
    const data = await r.json();
    if (mine !== controller) return;
    items = Array.isArray(data.items) ? data.items : [];
    render(data.max_duration_sec);
  } catch (e) {
    if (e?.name === "AbortError") return;
    items = [];
    setStatus(t("search.failed"));
    lastQuery = "";
  }
}

function schedule(force = false) {
  const raw = input.value;
  const query = raw.trim();
  if (timerId) { clearTimeout(timerId); timerId = null; }
  if (query.length < MIN_QUERY || LOOKS_LIKE_URL.test(raw)) { close(); return; }
  if (!force && query === lastQuery) return;
  // A trailing space means a finished word, which is a far stronger signal
  // than "has not typed for a moment".
  const delay = force ? 0 : /\s$/.test(raw) ? WORD_MS : IDLE_MS;
  timerId = setTimeout(() => {
    timerId = null;
    // run() clears lastQuery itself on failure, so an identical retry is
    // allowed through. Without that a transient error sticks until the user
    // edits the text, because schedule() short-circuits on an equal query.
    lastQuery = query;
    run(query);
  }, delay);
}

function buildPanel(wrap) {
  panel = document.createElement("div");
  panel.className = "search-panel hidden";
  panel.id = "search-panel";

  tabsEl = document.createElement("div");
  tabsEl.className = "search-tabs";
  tabsEl.setAttribute("role", "tablist");
  TABS.forEach((tab, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "search-tab" + (i === 0 ? " active" : "");
    b.setAttribute("role", "tab");
    b.dataset.index = String(i);
    b.setAttribute("data-i18n", tab.labelKey);
    // Set now as well as marking it: this panel is built after the initial
    // applyTranslations pass, so relying on the attribute alone would render
    // the raw key until the user changed language.
    b.textContent = t(tab.labelKey);
    tabsEl.appendChild(b);
  });

  listEl = document.createElement("ul");
  listEl.className = "search-list";
  listEl.setAttribute("role", "listbox");

  statusEl = document.createElement("div");
  statusEl.className = "search-status hidden";
  statusEl.setAttribute("role", "status");

  panel.append(tabsEl, statusEl, listEl);
  // Deliberately NOT inside .url-wrap. Its ancestor .daw-composer sets
  // overflow:hidden to clip the rounded pill, which clipped the whole dropdown
  // out of existence -- it rendered, it was just never visible. Living on
  // <body> with fixed positioning sidesteps that and any future ancestor that
  // grows a clip or a transform.
  document.body.appendChild(panel);
  // Re-translate the tabs on a language switch. Rows are rebuilt on the next
  // search, so they need nothing here.
  onLanguageChange(() => applyTranslations(panel));
}

/**
 * Wire live search onto the topbar URL box.
 * @param {(item: {url: string, title: string}) => void} onSelect
 *   Called with the chosen result. Filling the box and submitting is the
 *   caller's job, so this module never has to know about the import flow.
 */
export function initSearch(onSelect) {
  input = document.getElementById("url");
  const wrap = input?.closest(".url-wrap");
  if (!input || !wrap) return;
  onPick = onSelect;

  // The input is already type="text" in the markup. Do NOT set it here: the
  // topbar styling keys off #url now, but mutating the element's type at
  // runtime is exactly the kind of thing that silently unstyles it again.
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", "search-panel");

  buildPanel(wrap);

  input.addEventListener("input", () => schedule());
  input.addEventListener("focus", () => { if (items.length) open(); });

  // Fixed positioning means the panel does not follow its anchor on its own.
  // Passive listeners, and only while it is actually open.
  const reflow = (e) => {
    if (panel.classList.contains("hidden")) return;
    // The panel's own list scrolls. Repositioning on that is pure waste: a
    // forced layout and three style writes per scroll event, on the one
    // element whose position did not move.
    if (e && e.target !== document && panel.contains(e.target)) return;
    position();
  };
  window.addEventListener("resize", reflow, { passive: true });
  window.addEventListener("scroll", reflow, { passive: true, capture: true });

  input.addEventListener("keydown", (e) => {
    const open = !panel.classList.contains("hidden");
    if (e.key === "Escape") { close(); return; }
    if (e.key === "ArrowDown") { if (open) { e.preventDefault(); highlight(cursor + 1); } return; }
    if (e.key === "ArrowUp") { if (open) { e.preventDefault(); highlight(cursor - 1); } return; }
    if (e.key === "Enter" && open && cursor >= 0) { e.preventDefault(); pick(cursor); }
  });

  // One listener for every row, now and later.
  listEl.addEventListener("mousedown", (e) => {
    // Auditioning a track is not choosing it. Handled on click below so the
    // button behaves like a button.
    if (e.target.closest(".search-preview-btn, .search-player")) { e.preventDefault(); return; }
    const row = e.target.closest(".search-row");
    if (!row) return;
    e.preventDefault(); // keep focus in the input so blur does not race the click
    pick(Number(row.dataset.index));
  });

  listEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".search-preview-btn");
    if (!btn) return;
    e.stopPropagation();
    startPreview(Number(btn.dataset.preview));
  });

  // Keep focus in the input: a tab is a button, and letting it take focus
  // would blur the box the user is still typing into.
  tabsEl.addEventListener("mousedown", (e) => {
    if (e.target.closest(".search-tab")) e.preventDefault();
  });

  tabsEl.addEventListener("click", (e) => {
    const b = e.target.closest(".search-tab");
    if (!b) return;
    activeTab = Number(b.dataset.index);
    for (const el of tabsEl.children) el.classList.toggle("active", el === b);
    // Switching tabs re-asks the same question of a different service, so the
    // previous results go now rather than lingering under the new tab while
    // the request is in flight.
    items = [];
    lastQuery = "";
    schedule(true);
  });

  // The panel is a sibling of the composer on <body>, not a descendant of
  // wrap, so it has to be named explicitly here. Checking wrap alone meant a
  // mousedown on a tab counted as an outside click and closed the dropdown
  // before the tab's own click handler ever ran.
  document.addEventListener("mousedown", (e) => {
    if (panel.classList.contains("hidden")) return;
    if (wrap.contains(e.target) || panel.contains(e.target)) return;
    close();
  });
}

export function closeSearch() {
  close();
}
