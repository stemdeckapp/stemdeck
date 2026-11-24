// Sound and picture sync in Performance's export step (streamer.js
// buildSide): how the last take's picture was placed against its sound, in
// words, and a nudge to move the picture earlier or later, saved on the take
// (POST .../sync) so the next export and the review use it. The wording and
// the route are in video/avSync.js.
//
// A slider (5 ms steps, a second either way), a step button each side, and
// Reset. A slider move is shown at once and saved when it is let go; step
// presses are saved together once they pause. One save at a time: a value
// changed while one is on its way is saved after it.

import { t } from "../i18n.js";
import {
  NUDGE_MIN_MS, NUDGE_MAX_MS, NUDGE_STEP_MS, clampNudgeMs, readSync, syncView, signedMs, postSync, refusalText,
} from "../video/avSync.js";

const STEP_SAVE_MS = 400;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function button(cls) {
  const b = el("button", cls);
  b.type = "button";
  return b;
}

/**
 * Build the control. `onSaved(jobId, entry)` is called with the take entry
 * the server answered after a change (the caller puts it where the take is
 * read from). Returns { el, paint({ jobId, take, locked }) }: `take` is the
 * server's take entry (or null), `locked` disables changes (an export runs,
 * a take records). paint does nothing when nothing it shows changed.
 */
export function createSyncControl({ onSaved = () => {} } = {}) {
  const root = el("div", "sm-sync");
  root.setAttribute("role", "group");
  root.hidden = true;
  const title = el("p", "sm-sync-title");
  root.setAttribute("aria-labelledby", "smSyncTitle");
  title.id = "smSyncTitle";
  const state = el("p", "sm-sync-state");
  const offset = el("p", "sm-sync-offset");
  const warn = el("p", "sm-sync-warn");
  warn.hidden = true;

  const label = el("label", "sm-sync-label");
  label.htmlFor = "smSyncRange";
  const row = el("div", "sm-sync-row");
  const minus = button("sm-ib sm-sync-step sm-sync-earlier");
  minus.textContent = "−";
  const range = el("input", "sm-sync-range");
  range.type = "range";
  range.id = "smSyncRange";
  range.min = String(NUDGE_MIN_MS);
  range.max = String(NUDGE_MAX_MS);
  range.step = String(NUDGE_STEP_MS);
  range.value = "0";
  const plus = button("sm-ib sm-sync-step sm-sync-later");
  plus.textContent = "+";
  const value = el("output", "sm-sync-value");
  value.htmlFor = "smSyncRange";
  row.append(minus, range, plus, value);
  const hint = el("p", "sm-sync-hint");
  hint.id = "smSyncHint";
  range.setAttribute("aria-describedby", "smSyncHint");

  const actions = el("div", "sm-sync-actions");
  const reset = button("sm-ib sm-sync-reset");
  const measure = button("sm-ib sm-sync-measure");
  actions.append(reset, measure);
  const status = el("p", "sm-sync-status");
  status.setAttribute("role", "status");

  root.append(title, state, offset, warn, label, row, hint, actions, status);

  // What is shown: the take it is for, the value the person is moving (null
  // when it follows the take), and the save on its way.
  let cur = { jobId: null, take: null, locked: false };
  let draft = null;
  let saving = false;
  let queued = null;
  let note = "";
  let stepTimer = 0;
  let sig = "";

  const sync = () => readSync(cur.take);

  function shownNudge() {
    if (draft !== null) return draft;
    return sync()?.nudgeMs ?? 0;
  }

  function render() {
    const s = sync();
    const view = syncView(s, t);
    const nudge = shownNudge();
    const busy = saving || cur.locked;
    const next = [
      cur.take?.id ?? "", view.show, view.state, view.offset, view.warn, nudge, busy, note,
      view.canReset, view.canRemeasure, t("avsync.title"),
    ].join("|");
    if (next === sig) return;
    sig = next;
    root.hidden = !view.show;
    if (!view.show) return;
    title.textContent = t("avsync.title");
    state.textContent = view.state;
    offset.textContent = view.offset;
    warn.textContent = view.warn;
    warn.hidden = !view.warn;
    root.dataset.source = s.source;
    label.textContent = t("avsync.nudge");
    hint.textContent = t("avsync.nudgeHint");
    range.value = String(clampNudgeMs(nudge));
    const text = t("avsync.ms", { n: signedMs(nudge) });
    value.textContent = text;
    range.setAttribute("aria-valuetext", text);
    for (const [b, key] of [[minus, "avsync.earlier"], [plus, "avsync.later"], [reset, "avsync.resetTitle"]]) {
      b.setAttribute("aria-label", t(key));
      b.title = t(key);
    }
    reset.textContent = t("avsync.reset");
    measure.textContent = t("avsync.measureAgain");
    reset.hidden = !(view.canReset || nudge !== 0);
    measure.hidden = !view.canRemeasure;
    for (const c of [minus, plus, range, reset, measure]) c.disabled = busy;
    minus.disabled = busy || nudge <= NUDGE_MIN_MS;
    plus.disabled = busy || nudge >= NUDGE_MAX_MS;
    status.textContent = note;
    status.hidden = !note;
  }

  async function send(body, busyKey) {
    const { jobId, take } = cur;
    if (!jobId || !take) return;
    saving = true;
    note = t(busyKey);
    render();
    try {
      const entry = await postSync(jobId, take.id, body);
      if (entry && typeof entry === "object" && entry.id === take.id) onSaved(jobId, entry);
      note = t("avsync.saved");
    } catch (err) {
      console.warn("[streamer] saving the sync failed:", err);
      note = refusalText(err, t);
      // The take's own value again: nothing was saved.
      queued = null;
      draft = null;
    } finally {
      saving = false;
    }
    if (queued !== null && cur.take?.id === take.id) {
      const v = queued;
      queued = null;
      saveNudge(v);
      return;
    }
    // The slider follows the take again, now that it holds what was sent.
    if (queued === null) draft = null;
    render();
  }

  function saveNudge(v) {
    const nudge = clampNudgeMs(v);
    draft = nudge;
    if (saving) {
      queued = nudge;
      render();
      return;
    }
    if (nudge === (sync()?.nudgeMs ?? 0)) {
      draft = null;
      render();
      return;
    }
    send({ nudgeMs: nudge }, "avsync.saving");
  }

  function step(d) {
    draft = clampNudgeMs(shownNudge() + d);
    note = "";
    render();
    clearTimeout(stepTimer);
    stepTimer = setTimeout(() => saveNudge(draft), STEP_SAVE_MS);
  }

  minus.addEventListener("click", () => step(-NUDGE_STEP_MS));
  plus.addEventListener("click", () => step(NUDGE_STEP_MS));
  // Shown while it moves, saved when it is let go (the keyboard's arrows
  // fire change at each press).
  range.addEventListener("input", () => {
    draft = clampNudgeMs(range.value);
    note = "";
    render();
  });
  range.addEventListener("change", () => {
    clearTimeout(stepTimer);
    saveNudge(range.value);
  });
  reset.addEventListener("click", () => {
    clearTimeout(stepTimer);
    saveNudge(0);
  });
  measure.addEventListener("click", () => {
    if (saving) return;
    send({ remeasure: true }, "avsync.measuring");
  });

  function paint({ jobId = null, take = null, locked = false } = {}) {
    if (take?.id !== cur.take?.id || jobId !== cur.jobId) {
      // Another take: what was being moved and said was about the last one.
      clearTimeout(stepTimer);
      draft = null;
      queued = null;
      note = "";
    }
    cur = { jobId, take, locked };
    render();
  }

  return { el: root, paint };
}
