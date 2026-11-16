// timelineTags.js: the Tags bar under the Sections bar (#1075).
//
// A tag is a named point on the song's timeline. Clicking it moves the
// playhead there and nothing else: a section always loops over what it covers
// and cannot hold two start points, which is what people were using sections
// for. Digit keys 1 to 9 jump to the first nine tags, in time order.
//
// Built like the Sections bar (sections.js): the marks sit at percentages of
// an inner track that is the zoomed waveform's width, transport.js slides that
// track by the waveform's scroll (syncRulerScroll), and the whole list is
// saved per song with a debounced PATCH. The bar is a second row of the
// Sections bar, so Collapse > Sections and the bar's own Hide put both away.
//
// A small loop button on each tag loops from that tag to the next one in time
// order (the last one loops to the song's end). It does not run a loop of its
// own: it arms the one loop the transport already has, the same one a section
// click or a ruler drag arms, so two loops can never be on at once. Whether a
// tag's loop is on is read from that loop each time (matchingTagId), not
// remembered here, so a section click, a drag on the loop edges, the Loop
// button or a new song all switch the tag's button off with no extra code.
//
// It imports nothing but i18n, the shortcut rule in libraryDeleteKey.js and
// the loop length rule in loopRegion.js (none touches the DOM), for the same
// reason sections.js does: the transport reaches the DOM at import time, so
// the calls it needs (where the playhead is, move it, and the loop) are
// handed in by main.js.

import { onLanguageChange, t } from "./i18n.js";
import { ignoredByShortcuts } from "./libraryDeleteKey.js";
import { MIN_LOOP_SEC } from "./loopRegion.js";

/** Within this many seconds of a tag, Add renames that tag instead of
 * stacking a second one on the same spot, where it could not be seen. */
export const SAME_SPOT_SEC = 0.05;

/** Digits reach the first nine tags; a tenth has no key. */
export const SHORTCUT_COUNT = 9;

const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

let _trackId = null;
let _duration = 0;
let _tags = [];
let _container = null;
let _saveTimer = null;
let _saveChain = Promise.resolve();
let _bridge = null;

/// Wire the bar to the transport. Called once, from main.js. Unset, Add and
/// seeking do nothing rather than throw.
export function setTimelineTagsBridge(bridge) {
  _bridge = bridge;
}

onLanguageChange(() => _render());

// ─── Pure helpers (tests/js/timeline-tags.test.mjs) ───────

/**
 * The tags a song's state holds, cleaned and in time order. The list comes
 * from a file on disk through the server, which checks each entry, but a
 * record written by hand or by an older build is still possible: an entry
 * without a usable id or time is dropped rather than drawn at NaN, and a
 * second entry with an id already seen is dropped so rename and delete never
 * land on two tags at once.
 */
export function normalizeTags(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const out = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const id = typeof item.id === "string" ? item.id : "";
    const time = Number(item.time);
    if (!ID_RE.test(id) || seen.has(id) || !Number.isFinite(time) || time < 0) continue;
    seen.add(id);
    out.push({ id, name: typeof item.name === "string" ? item.name : "", time });
  }
  return out.sort((a, b) => a.time - b.time);
}

/**
 * Which tag a key reaches, as an index into the time-ordered list, or -1.
 * Read from `code`, not `key`, so the key marked 1 works on every layout:
 * on AZERTY it types "&" unshifted, and on a Mac with Option held it types
 * a symbol. The number pad counts as well, but only while it types digits:
 * with NumLock off it sends the same codes for End, ArrowDown, ArrowLeft and
 * the rest, and `key` is then that key's name, not the digit.
 */
export function shortcutIndex(code, key) {
  const m = /^(Digit|Numpad)([1-9])$/.exec(String(code || ""));
  if (!m) return -1;
  if (m[1] === "Numpad" && key !== m[2]) return -1;
  return Number(m[2]) - 1;
}

/** The tag a key reaches in `tags` (time-ordered), or null. */
export function tagForKey(tags, code, key) {
  const i = shortcutIndex(code, key);
  return i >= 0 && i < tags.length ? tags[i] : null;
}

/**
 * Which tag a key press reaches, or -1 when the digits must leave it alone.
 * The digits are the studio's keys only, so they do nothing while something
 * else is in front of it or busy with the playhead:
 *
 * - Shift held: Shift+1 types "!" and belongs to whatever wants it.
 * - Ctrl, Cmd, Alt, a text field or a select: ignoredByShortcuts, the rule
 *   the studio's other one-key shortcuts follow.
 * - Karaoke or Performance open (Performance is the "streamer" view): those
 *   views have their own keys and their own picture of the song.
 * - A dialog or the Guide open: a key pressed there is meant for it.
 * - A take recording or a count-in running: a jump would move the playhead
 *   under the take, or start the song before the count ends.
 *
 * Pure: main.js reads each state from its own module and passes it in, so
 * node can test the decision (tests/js/timeline-tags.test.mjs).
 *
 * @param {KeyboardEvent|object} e
 * @param {object} state
 * @param {boolean} [state.karaokeOpen]  isKaraokeOpen()
 * @param {boolean} [state.streamerOpen]  isStreamerOpen(), Performance
 * @param {boolean} [state.dialogOpen]  a dialog is open or has the key
 * @param {boolean} [state.guideOpen]  the Guide (helpMode.js) is on
 * @param {boolean} [state.recording]  isRecording() or isStreamerTakeOpen()
 * @param {boolean} [state.countIn]  isCountInRunning() (transport.js)
 */
export function tagShortcutIndex(e, {
  karaokeOpen = false,
  streamerOpen = false,
  dialogOpen = false,
  guideOpen = false,
  recording = false,
  countIn = false,
} = {}) {
  if (!e || e.repeat || e.shiftKey || ignoredByShortcuts(e)) return -1;
  if (karaokeOpen || streamerOpen || dialogOpen || guideOpen || recording || countIn) return -1;
  return shortcutIndex(e.code, e.key);
}

/** The tag within SAME_SPOT_SEC of `time`, or null. */
export function tagNear(tags, time) {
  return tags.find((tag) => Math.abs(tag.time - time) <= SAME_SPOT_SEC) || null;
}

/** A loop bound read back from the transport matches within this many seconds. */
const LOOP_MATCH_SEC = 0.002;

/**
 * The span a tag's loop covers: from the tag to the next tag later in time, or
 * to the song's end for the last one. Null when the span is shorter than the
 * shortest loop the transport accepts (MIN_LOOP_SEC): two tags almost on top
 * of each other, or the last tag at or past the end, so the button can say it
 * cannot loop instead of arming nothing. A tag sharing another's exact time is
 * skipped over, so it loops to the first tag that is actually later.
 *
 * @param {{id:string,time:number}[]} tags  time-ordered
 * @returns {{start:number,end:number,toEnd:boolean}|null}
 */
export function loopSpanForTag(tags, id, duration, minLoop = MIN_LOOP_SEC) {
  const i = tags.findIndex((tag) => tag.id === id);
  if (i < 0) return null;
  const start = tags[i].time;
  const next = tags.slice(i + 1).find((tag) => tag.time > start);
  const end = next ? next.time : duration;
  if (!(Number.isFinite(end) && end - start >= minLoop)) return null;
  return { start, end, toEnd: !next };
}

/**
 * The id of the tag whose loop is the transport's loop right now, or null.
 * `loop` is {enabled, start, end} as main.js reads it from the state.
 */
export function matchingTagId(tags, loop, duration) {
  if (!loop || !loop.enabled) return null;
  for (const tag of tags) {
    const span = loopSpanForTag(tags, tag.id, duration);
    if (span
      && Math.abs(span.start - loop.start) <= LOOP_MATCH_SEC
      && Math.abs(span.end - loop.end) <= LOOP_MATCH_SEC) return tag.id;
  }
  return null;
}

/** "1:04.25": minutes, seconds and hundredths, as the tag's tooltip reads it. */
export function formatTagTime(seconds) {
  const cs = Math.max(0, Math.round((Number(seconds) || 0) * 100));
  const m = Math.floor(cs / 6000);
  const s = Math.floor((cs % 6000) / 100);
  return `${m}:${String(s).padStart(2, "0")}.${String(cs % 100).padStart(2, "0")}`;
}

// ─── Public API ───────────────────────────────────────────

export function initTimelineTags(trackId, tags, duration) {
  // A save still waiting out its debounce belongs to the song open until now.
  // Opening another song from the library comes here through wireUpAudio and
  // never calls destroyPlayer, so destroyTimelineTags has not sent it: send it
  // now, while _trackId and _tags are still that song's. Left to the timer,
  // it would fire after the reassignment below and save this song's own list
  // under this song, and the edit on the other one would be lost.
  if (_saveTimer !== null) {
    clearTimeout(_saveTimer);
    _saveTimer = null;
    _queueSaveSnapshot();
  }
  // "Saving", "Saved" or "Tags not saved" spoke of the song open before.
  if (trackId !== _trackId) {
    _hideSaveIndicator();
    // A loop between tags of the song before means nothing on this one.
    if (_trackId !== null) _clearOwnLoop();
  }
  _trackId = trackId;
  _duration = Math.max(1, duration || 0);
  _tags = normalizeTags(tags);
  _container = document.getElementById("daw-tags-track");
  const addBtn = document.getElementById("tagsAddBtn");
  if (addBtn && !addBtn.dataset.tagsWired) {
    addBtn.dataset.tagsWired = "1";
    addBtn.addEventListener("click", () => _addTag());
  }
  if (_container) {
    // Where the waveform is scrolled to now; transport.js keeps it in step
    // from here on.
    const wave = document.getElementById("wave-scroll");
    _container.style.transform = `translateX(${-(wave?.scrollLeft || 0)}px)`;
  }
  _render();
}

export function destroyTimelineTags() {
  // A pending save goes out before the state is cleared, so closing the
  // studio never drops a tag. Opening another song does not come through
  // here: initTimelineTags sends a pending save itself. The body is built
  // before the first await.
  if (_saveTimer !== null) {
    clearTimeout(_saveTimer);
    _saveTimer = null;
    _queueSaveSnapshot();
  }
  _hideSaveIndicator();
  _clearOwnLoop();
  _trackId = null;
  _tags = [];
  _duration = 0;
  if (_container) _container.replaceChildren();
  _container = null;
}

/** The open song's tags in time order, as copies. */
export function getTimelineTags() {
  return _tags.map((tag) => ({ ...tag }));
}

/**
 * Move the playhead to the tag a digit key reaches. True when the key was a
 * tag's, so the caller can take it; false leaves it for anything else.
 * `state` is what tagShortcutIndex needs to know about the rest of the app.
 */
export function seekToTagForKey(e, state) {
  if (!_trackId) return false;
  const i = tagShortcutIndex(e, state);
  const tag = i >= 0 && i < _tags.length ? _tags[i] : null;
  if (!tag) return false;
  _bridge?.seek?.(tag.time);
  return true;
}

export function flushTimelineTagsSave() {
  if (_saveTimer !== null) {
    clearTimeout(_saveTimer);
    _saveTimer = null;
  }
  return _queueSaveSnapshot();
}

// ─── Rendering ────────────────────────────────────────────

function _render() {
  if (!_container) return;
  _container.replaceChildren(..._tags.map((tag, i) => _makeTagEl(tag, i)));
  refreshTagLoopState();
}

/// The id of the tag whose loop is armed, or null.
function _activeLoopTagId() {
  return matchingTagId(_tags, _bridge?.getLoop?.(), _duration);
}

/// Switch a tag's loop off, but only when it is the loop that is armed: a
/// section's loop or a hand-drawn one is not this bar's to clear.
function _clearOwnLoop() {
  if (_activeLoopTagId() !== null) _bridge?.clearLoop?.();
}

/// Show which tag's loop is on. Called from main.js on every loop change, so a
/// section click, a drag on the region or the Loop button moves it too.
export function refreshTagLoopState() {
  if (!_container) return;
  const active = _activeLoopTagId();
  for (const btn of _container.querySelectorAll(".tag-loop")) {
    const on = btn.closest(".tag-mark")?.dataset.id === active;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }
}

/// A tag was added or deleted. If the armed loop was a tag's, the tag after it
/// may have changed, so loop the span it has now, or switch the loop off when
/// the tag is gone or the span is too short. `ownerId` is read before the edit.
function _reloopAfterEdit(ownerId) {
  if (ownerId === null) return;
  const span = loopSpanForTag(_tags, ownerId, _duration);
  if (!span) _bridge?.clearLoop?.();
  else _bridge?.setLoopRange?.(span.start, span.end);
}

function _toggleTagLoop(id) {
  if (_activeLoopTagId() === id) {
    _bridge?.clearLoop?.();
    return;
  }
  const span = loopSpanForTag(_tags, id, _duration);
  if (!span) return;
  // Arms the one loop the transport has, replacing whatever was in it.
  _bridge?.setLoopRange?.(span.start, span.end);
}

function _makeTagEl(tag, index) {
  const pct = (Math.min(tag.time, _duration) / _duration) * 100;
  const el = document.createElement("div");
  el.className = "tag-mark";
  el.dataset.id = tag.id;
  el.style.left = `${pct.toFixed(4)}%`;

  const name = tag.name || t("tags.defaultName");
  const time = formatTagTime(tag.time);
  const seek = document.createElement("button");
  seek.type = "button";
  seek.className = "tag-seek";
  // Off the Tab order like a section's buttons: the digit keys reach the
  // tags, and a stop per tag would sit between the bar and the mixer.
  seek.tabIndex = -1;
  const label = index < SHORTCUT_COUNT
    ? t("tags.seekKeyAria", { name, time, key: index + 1 })
    : t("tags.seekAria", { name, time });
  seek.setAttribute("aria-label", label);
  seek.title = label;
  if (index < SHORTCUT_COUNT) {
    const num = document.createElement("span");
    num.className = "tag-num";
    num.setAttribute("aria-hidden", "true");
    num.textContent = String(index + 1);
    seek.append(num);
  }
  const text = document.createElement("span");
  text.className = "tag-label";
  text.textContent = name;
  seek.append(text);
  seek.addEventListener("click", () => _bridge?.seek?.(tag.time));

  const del = document.createElement("button");
  del.type = "button";
  del.className = "tag-del";
  del.tabIndex = -1;
  del.setAttribute("aria-label", t("tags.deleteAria"));
  del.title = t("tags.deleteAria");
  del.textContent = "×";
  del.addEventListener("click", (e) => {
    e.stopPropagation();
    _deleteTag(tag.id);
  });

  const span = loopSpanForTag(_tags, tag.id, _duration);
  const loop = document.createElement("button");
  loop.type = "button";
  loop.className = "tag-loop";
  // In the Tab order, unlike the seek and delete buttons: the digit keys do
  // not reach a loop, so this is its keyboard way in.
  const loopLabel = !span
    ? t("tags.loopTooShort", { name })
    : span.toEnd
      ? t("tags.loopEndAria", { name })
      : t("tags.loopAria", { name });
  loop.setAttribute("aria-label", loopLabel);
  loop.title = loopLabel;
  loop.setAttribute("aria-pressed", "false");
  if (!span) loop.setAttribute("aria-disabled", "true");
  const glyph = document.createElement("span");
  glyph.setAttribute("aria-hidden", "true");
  glyph.textContent = "↻";
  loop.append(glyph);
  loop.addEventListener("click", (e) => {
    e.stopPropagation();
    _toggleTagLoop(tag.id);
  });

  el.append(seek, loop, del);
  // On the mark rather than the label, the way sections.js binds it: the
  // two clicks of a double-click have already moved the playhead to this tag,
  // which is where the rename is about.
  el.addEventListener("dblclick", (e) => {
    if (e.target.closest(".tag-del, .tag-loop")) return;
    e.stopPropagation();
    _openRename(tag.id);
  });
  return el;
}

// ─── Editing ──────────────────────────────────────────────

function _addTag() {
  if (!_trackId) return;
  const now = Number(_bridge?.getTime?.());
  if (!Number.isFinite(now)) return;
  const time = Math.round(Math.max(0, Math.min(_duration, now)) * 1000) / 1000;
  // One already here: name that one rather than hide a second under it.
  const existing = tagNear(_tags, time);
  if (existing) {
    _openRename(existing.id);
    return;
  }
  const tag = { id: _nextId(), name: t("tags.defaultName"), time };
  const ownerId = _activeLoopTagId();
  _tags = normalizeTags([..._tags, tag]);
  _reloopAfterEdit(ownerId);
  _render();
  _scheduleSave();
  _scrollIntoView(tag.time);
  _openRename(tag.id);
}

function _deleteTag(id) {
  const ownerId = _activeLoopTagId();
  _tags = _tags.filter((tag) => tag.id !== id);
  // The loop of the tag deleted goes with it (no span is left to loop); one of
  // the tag before it now runs on to the tag after.
  _reloopAfterEdit(ownerId);
  _render();
  _scheduleSave();
}

function _openRename(id) {
  const tag = _tags.find((x) => x.id === id);
  const el = _container?.querySelector(`.tag-mark[data-id="${id}"]`);
  // The whole seek button is swapped for the field, not only its label: a
  // field inside a button is not valid, and the button would take its keys.
  const seekEl = el?.querySelector(".tag-seek");
  if (!tag || !seekEl) return;
  el.classList.add("tag-renaming");

  const input = document.createElement("input");
  input.className = "tag-rename-input";
  input.type = "text";
  input.maxLength = 64;
  input.setAttribute("aria-label", t("tags.renameAria"));
  const original = tag.name || t("tags.defaultName");
  input.value = original;
  seekEl.replaceWith(input);
  input.focus();
  input.select();

  const commit = () => {
    const next = input.value.trim();
    if (next && next !== original) {
      tag.name = next;
      _render();
      _scheduleSave();
      return;
    }
    _render();
  };
  input.addEventListener("blur", commit, { once: true });
  // The studio's own keys (Space plays, digits jump) already leave a text
  // field alone: ignoredByShortcuts in libraryDeleteKey.js.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") {
      e.preventDefault();
      input.removeEventListener("blur", commit);
      _render();
    }
  });
  // A double-click in the field selects a word; it must not open a second
  // rename on the mark around it.
  input.addEventListener("dblclick", (e) => e.stopPropagation());
}

/**
 * Bring a time into view by scrolling the waveform, as sections.js does: the
 * bar cannot scroll (overflow: clip) and slides with the waveform, so a tag
 * added off screen would open its name field where nobody can see it.
 */
function _scrollIntoView(time) {
  const wave = document.getElementById("wave-scroll");
  if (!wave || !_duration || wave.scrollWidth <= wave.clientWidth) return;
  const at = (time / _duration) * wave.scrollWidth;
  if (at >= wave.scrollLeft + 24 && at <= wave.scrollLeft + wave.clientWidth - 160) return;
  wave.scrollLeft = Math.max(0, Math.min(at - wave.clientWidth / 3, wave.scrollWidth - wave.clientWidth));
}

// ─── Persistence ──────────────────────────────────────────

let _savedTimer = null;

function _indicator() {
  return document.getElementById("tagsSaveIndicator");
}

function _showSaving() {
  const el = _indicator();
  if (!el) return;
  clearTimeout(_savedTimer);
  el.textContent = t("sections.saving");
  el.className = "sections-save-indicator";
}

function _showSaved() {
  const el = _indicator();
  if (!el) return;
  el.textContent = t("sections.saved");
  el.className = "sections-save-indicator saved";
  _savedTimer = setTimeout(() => {
    el.className = "sections-save-indicator hidden";
  }, 1800);
}

function _hideSaveIndicator() {
  const el = _indicator();
  if (el) el.className = "sections-save-indicator hidden";
  clearTimeout(_savedTimer);
}

function _scheduleSave() {
  clearTimeout(_saveTimer);
  _showSaving();
  _saveTimer = setTimeout(() => {
    _saveTimer = null;
    _queueSaveSnapshot();
  }, 600);
}

function _body() {
  return JSON.stringify({ timeline_tags: _tags });
}

function _queueSaveSnapshot() {
  if (!_trackId) return _saveChain;
  const id = _trackId;
  const body = _body();
  _saveChain = _saveChain.then(() => _sendSave(id, body));
  return _saveChain;
}

async function _sendSave(id, body) {
  try {
    const res = await fetch(`/api/jobs/${id}/timeline-tags`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => String(res.status));
      console.warn("[tags] save failed:", res.status, detail);
      if (id === _trackId) _showFailed();
      return;
    }
    if (id === _trackId && body === _body() && _saveTimer === null) _showSaved();
  } catch (e) {
    console.warn("[tags] save failed:", e);
    if (id === _trackId) _showFailed();
  }
}

// A save that did not reach the server says so where "Saving" was shown,
// rather than vanishing: the tag is on screen and would be gone on the next
// open, which is the one failure here that loses something.
function _showFailed() {
  const el = _indicator();
  if (!el) return;
  clearTimeout(_savedTimer);
  el.textContent = t("tags.saveFailed");
  el.className = "sections-save-indicator notice";
}

function _nextId() {
  return `g${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}
