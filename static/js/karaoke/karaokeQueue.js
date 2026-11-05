// Karaoke Mode's queue: the songs to sing next, first to play first.
//
// Songs come from the library by drag and drop (a library row carries its
// track id, catalog.js TRACK_DRAG_TYPE), are put in order by dragging them
// inside the queue (or Alt + the arrow keys on a row), and play one after the
// other: when a song ends Karaoke plays the next (karaoke.js). The list is
// saved (a song id each) so it is still there next time. A song that is gone
// from the library, or in the Trash, or not finished, is not shown and is
// dropped from the list when it comes to its turn.

import { t } from "../i18n.js";
import { storeGet, storeSet } from "../utils.js";
import { getTrackSummary, TRACK_DRAG_TYPE } from "../catalog.js";
import { queueInsert, queueRemove, queueClean, dropIndex, clockLong } from "./karaokeModel.js";

const KEY = "stemdeck:karaoke:queue";
// What a row of the queue carries while it is dragged inside it. Not text/plain:
// the library's own drop targets read that as a track id.
const QUEUE_DRAG_TYPE = "application/x-stemdeck-karaoke-queue";

let items = [];
const listeners = new Set();

function changed(save = true) {
  if (save) storeSet(KEY, items).catch((e) => console.warn("[karaoke] could not save the queue:", e));
  for (const cb of listeners) {
    try { cb(); } catch (err) { console.warn("[karaoke] queue listener failed:", err); }
  }
}

export async function loadQueue() {
  try {
    items = queueClean(await storeGet(KEY, []));
  } catch (e) {
    console.warn("[karaoke] could not read the queue:", e);
    items = [];
  }
  changed(false);
}

/** The songs waiting, in order: only those the library can still play. */
export function getQueue() {
  return items.filter((id) => getTrackSummary(id));
}

export function onQueueChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

/** Put `id` in the queue at `index` (the end when omitted); moves it if it is there. */
export function addToQueue(id, index) {
  if (!getTrackSummary(id)) return false;
  // Positions are among the songs shown; the stored list may hold hidden ones.
  const shown = getQueue();
  const next = queueInsert(shown, id, index);
  items = [...next, ...items.filter((x) => !next.includes(x))];
  changed();
  return true;
}

export function removeFromQueue(id) {
  if (!items.includes(id)) return;
  items = queueRemove(items, id);
  changed();
}

/** The song to play next, taken out of the queue; null when none can play. */
export function shiftQueue() {
  while (items.length) {
    const id = items[0];
    items = items.slice(1);
    if (getTrackSummary(id)) {
      changed();
      return id;
    }
  }
  changed();
  return null;
}

// ── The panel ──

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/**
 * Build the queue into `container` (the panel body) and keep it drawn.
 * `onPlay(id)` is a click on a row: that song now. Returns { render }, to be
 * called every frame: it draws only when the queue, a song's details or the
 * language changed.
 */
export function mountQueue(container, { onPlay }) {
  const label = el("span", "kk-label kk-up-next");
  const list = el("div", "kk-q-items");
  list.setAttribute("role", "list");
  const hint = el("div", "kk-q-hint");
  container.append(label, list, hint);

  let lastSig = null;
  let focusId = null;
  let draggingId = null;

  const rows = () => [...list.querySelectorAll(".kk-q-row")];

  function clearMarks() {
    for (const r of rows()) r.classList.remove("kk-q-before");
    hint.classList.remove("kk-q-before");
    container.classList.remove("kk-drop");
  }

  function mark(index, among) {
    clearMarks();
    container.classList.add("kk-drop");
    if (index < among.length) among[index].classList.add("kk-q-before");
    else hint.classList.add("kk-q-before");
  }

  const accepts = (e) => {
    const types = [...(e.dataTransfer?.types || [])];
    return types.includes(QUEUE_DRAG_TYPE) || types.includes(TRACK_DRAG_TYPE);
  };

  // Rows other than the one being dragged, where the drop is measured.
  const others = () => rows().filter((r) => r.dataset.id !== draggingId);
  const indexAt = (e) => {
    const among = others();
    return { among, index: dropIndex(among.map((r) => {
      const b = r.getBoundingClientRect();
      return { top: b.top, height: b.height };
    }), e.clientY) };
  };

  container.addEventListener("dragover", (e) => {
    if (!accepts(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = [...e.dataTransfer.types].includes(QUEUE_DRAG_TYPE) ? "move" : "copy";
    const { among, index } = indexAt(e);
    mark(index, among);
  });
  container.addEventListener("dragleave", (e) => {
    if (!container.contains(e.relatedTarget)) clearMarks();
  });
  container.addEventListener("drop", (e) => {
    if (!accepts(e)) return;
    e.preventDefault();
    const id = e.dataTransfer.getData(QUEUE_DRAG_TYPE) || e.dataTransfer.getData(TRACK_DRAG_TYPE);
    const { index } = indexAt(e);
    clearMarks();
    draggingId = null;
    if (id) addToQueue(id, index);
  });

  function row(id, position, count) {
    const s = getTrackSummary(id);
    const r = el("div", "kk-q-row");
    r.dataset.id = id;
    r.draggable = true;
    r.tabIndex = 0;
    r.setAttribute("role", "listitem");
    r.setAttribute("aria-label", t("karaoke.queuePlay", { title: s.title }));
    const art = el("span", "kk-q-art");
    if (s.thumb) {
      const img = el("img", "kk-art-img");
      img.alt = "";
      img.referrerPolicy = "no-referrer";
      img.src = s.thumb;
      img.addEventListener("error", () => img.remove());
      art.append(img);
    }
    const text = el("span", "kk-q-text");
    text.append(
      el("span", "kk-q-title", s.title),
      el("span", "kk-q-sub", [s.artist, s.duration ? clockLong(s.duration) : ""].filter(Boolean).join(" · ")),
    );
    const remove = el("button", "kk-q-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", t("karaoke.queueRemove", { title: s.title }));
    remove.title = t("karaoke.queueRemove", { title: s.title });
    remove.addEventListener("click", (e) => {
      e.stopPropagation();
      removeFromQueue(id);
    });
    r.append(art, text, remove);
    r.addEventListener("click", () => onPlay(id));
    r.addEventListener("keydown", (e) => {
      if (e.target !== r) return;
      if (e.key === "Enter") {
        e.preventDefault();
        e.stopPropagation();
        onPlay(id);
      } else if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        e.stopPropagation();
        focusId = rows()[position + 1]?.dataset.id ?? rows()[position - 1]?.dataset.id ?? null;
        removeFromQueue(id);
      } else if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
        e.preventDefault();
        e.stopPropagation();
        const to = Math.max(0, Math.min(count - 1, position + (e.key === "ArrowUp" ? -1 : 1)));
        if (to !== position) {
          focusId = id;
          addToQueue(id, to);
        }
      }
    });
    r.addEventListener("dragstart", (e) => {
      draggingId = id;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData(QUEUE_DRAG_TYPE, id);
      r.classList.add("kk-q-dragging");
    });
    r.addEventListener("dragend", () => {
      draggingId = null;
      r.classList.remove("kk-q-dragging");
      clearMarks();
    });
    return r;
  }

  function render() {
    const ids = getQueue();
    const sig = ids.map((id) => {
      const s = getTrackSummary(id);
      return `${id}|${s.title}|${s.artist}|${s.duration}|${s.thumb}`;
    }).join("\n") + `\n${t("karaoke.queueHint")}|${t("karaoke.upNext")}`;
    if (sig === lastSig) return;
    lastSig = sig;
    label.textContent = t("karaoke.upNext");
    hint.textContent = t("karaoke.queueHint");
    list.replaceChildren(...ids.map((id, i) => row(id, i, ids.length)));
    if (focusId) {
      list.querySelector(`[data-id="${CSS.escape(focusId)}"]`)?.focus();
      focusId = null;
    }
  }

  return { render, invalidate: () => { lastSig = null; } };
}
