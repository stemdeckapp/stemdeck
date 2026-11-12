// Suggest EQ in an EQ panel: a "Suggest" button in the panel's toolbar and a
// card that shows what the server proposes, as an ordinary EQ the user can
// see (a ghost curve over the current one, and the reasons in plain words),
// hear (Preview: the suggestion or the current EQ at the same estimated
// loudness, without saving), apply (through the panel's undoable edit state)
// or dismiss. In the Master drawer it also proposes a loudness preset.
//
// Nothing is applied by itself and nothing is hidden: the result is the same
// EQ descriptor every panel edits (eqSuggestMath.js translates), and the
// card says that a target is a taste choice, not a rule.
//
// The analysis runs on the server (GET suggest/eq or suggest/master, about
// 2 s the first time for a song, then cached); the browser only draws.

import * as eq from "../eqEngine.js";
import * as master from "../masterEngine.js";
import { currentJobId } from "../state.js";
import { t } from "../i18n.js";
import { el, button } from "./eqPanelCommon.js";
import {
  canMerge, descriptorFromAnswer, masterParamsFromAnswer, matchedCurrent, presetLine, reasonLines,
  suggestUrl, targetOptions,
} from "./eqSuggestMath.js";

const LISTEN = ["off", "current", "suggested"];
const REFETCH_MS = 300;

/**
 * Wire Suggest into a panel.
 *   lane, kind   the panel's lane and kind
 *   state        the panel's edit state (eqPanelCommon.createEditState)
 *   toolbar      the toolbar row the button goes in (before Reset)
 *   cardHost     the element the card is appended to
 *   onShow(on)   the panel shows or hides the card (layout)
 *   onGhost(d)   the panel draws descriptor `d` as a ghost (null: none)
 *   onApplied()  the panel repaints after Apply
 */
export function createSuggest({ lane, kind, state, toolbar, cardHost, onShow, onGhost, onApplied }) {
  const isMaster = lane === eq.MASTER;
  const mergeable = canMerge(kind, lane);

  const btn = button("eq-btn eq-suggest-btn", "");
  btn.setAttribute("aria-haspopup", "true");
  btn.setAttribute("aria-expanded", "false");
  const reset = toolbar.querySelector(".eq-reset");
  toolbar.insertBefore(btn, reset || null);

  // ─── The card ───
  const card = el("section", "eq-suggest-card");
  card.hidden = true;
  const head = el("div", "eq-suggest-head");
  const title = el("h3", "eq-suggest-title");
  title.id = `eq-suggest-title-${Math.random().toString(36).slice(2, 9)}`;
  card.setAttribute("aria-labelledby", title.id);
  const closeBtn = button("eq-btn eq-suggest-close", "×");
  head.append(title, closeBtn);

  const pick = el("div", "eq-suggest-pick");
  const targetLabel = el("label", "eq-suggest-label");
  const targetName = el("span", "eq-suggest-label-text");
  const targetSel = el("select", "eq-preset eq-suggest-target");
  targetLabel.append(targetName, targetSel);
  pick.append(targetLabel);
  let modeSeg = null;
  let modeBtns = [];
  if (mergeable) {
    modeSeg = el("div", "eq-seg eq-suggest-mode");
    modeSeg.setAttribute("role", "group");
    modeBtns = ["replace", "merge"].map((m) => {
      const b = button("eq-seg-btn", "");
      b.dataset.mode = m;
      b.addEventListener("click", () => setMode(m));
      modeSeg.append(b);
      return b;
    });
    pick.append(modeSeg);
  }

  const status = el("p", "eq-suggest-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const preset = el("p", "eq-suggest-preset");
  preset.hidden = true;
  const why = el("ul", "eq-suggest-why");
  const taste = el("p", "eq-suggest-taste");

  const listenRow = el("div", "eq-suggest-listen");
  const listenName = el("span", "eq-suggest-label-text");
  const listenSeg = el("div", "eq-seg");
  listenSeg.setAttribute("role", "group");
  const listenBtns = LISTEN.map((m) => {
    const b = button("eq-seg-btn", "");
    b.dataset.listen = m;
    b.addEventListener("click", () => setListen(m));
    listenSeg.append(b);
    return b;
  });
  listenRow.append(listenName, listenSeg);

  const actions = el("div", "eq-suggest-actions");
  const applyBtn = button("eq-btn eq-suggest-apply", "");
  const dismissBtn = button("eq-btn eq-suggest-dismiss", "");
  actions.append(applyBtn, dismissBtn);

  card.append(head, pick, status, preset, why, taste, listenRow, actions);
  cardHost.append(card);

  // ─── State ───
  let open = false;
  let target = "";
  let mode = "replace";
  let listen = "off";
  let answer = null;
  let loading = false;
  let failed = false;
  let ctl = null;
  const answers = new Map();
  // After a master Apply: undoing the EQ back to `before` restores the
  // master preset that was there, so the panel's undo undoes the whole Apply.
  let masterUndo = null;
  let refetchTimer = 0;

  function suggestion() {
    return answer?.status === "ok" ? descriptorFromAnswer(answer, kind, state.current(), mode) : null;
  }

  function requestUrl() {
    if (isMaster) {
      const src = master.currentMixSource();
      if (!src) throw new Error("no mix");
      return suggestUrl({ jobId: src.jobId, lane, target, mode, mixQuery: src.query });
    }
    if (!currentJobId) throw new Error("no song");
    return suggestUrl({ jobId: currentJobId, lane, kind, target, mode, laneEq: state.current() });
  }

  async function fetchAnswer() {
    let url;
    try {
      url = requestUrl();
    } catch (err) {
      console.warn("[eq] no suggestion without a song:", err);
      failed = true;
      answer = null;
      paint();
      return;
    }
    if (answers.has(url)) {
      answer = answers.get(url);
      applyListen();
      paint();
      return;
    }
    ctl?.abort();
    const mine = new AbortController();
    ctl = mine;
    loading = true;
    failed = false;
    paint();
    try {
      const r = await fetch(url, { signal: mine.signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = await r.json();
      if (ctl !== mine) return;
      answer = body;
      // The first request names no target: the server's default is the one.
      target = body.target || target;
      answers.set(url, body);
    } catch (err) {
      if (mine.signal.aborted) return;
      console.warn("[eq] the suggestion failed:", err);
      failed = true;
      answer = null;
    } finally {
      if (ctl === mine) {
        ctl = null;
        loading = false;
      }
    }
    applyListen();
    paint();
  }

  function setOpen(next) {
    if (next === open) return;
    open = next;
    card.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    btn.setAttribute("aria-pressed", String(open));
    onShow?.(open);
    if (open) {
      answers.clear();
      listen = "off";
      fetchAnswer();
      requestAnimationFrame(() => targetSel.focus({ preventScroll: true }));
    } else {
      ctl?.abort();
      ctl = null;
      loading = false;
      setListen("off");
      onGhost?.(null);
    }
    paint();
  }

  function setMode(m) {
    if (m === mode) return;
    mode = m;
    fetchAnswer();
  }

  function setListen(m) {
    listen = LISTEN.includes(m) ? m : "off";
    applyListen();
    paintListen();
  }

  function applyListen() {
    const sug = suggestion();
    if (listen === "suggested" && sug) eq.previewLaneEq(lane, sug);
    else if (listen === "current" && answer?.status === "ok") eq.previewLaneEq(lane, matchedCurrent(state.current(), answer));
    else eq.previewLaneEq(lane, null);
  }

  function apply() {
    const sug = suggestion();
    if (!sug) return;
    const before = state.current();
    let prevMaster = null;
    if (isMaster && answer.master) {
      // A master that was off comes back off (bypassed) on undo.
      prevMaster = { ...master.getMaster(), bypass: !master.isMasterOn() };
      const params = masterParamsFromAnswer(answer);
      master.setMaster({ ...prevMaster, bypass: false, params: { ...prevMaster.params, ...params } });
    }
    listen = "off";
    eq.previewLaneEq(lane, null);
    const applied = state.apply(sug);
    masterUndo = prevMaster ? { before: JSON.stringify(before), after: JSON.stringify(applied), prevMaster } : null;
    setOpen(false);
    onApplied?.();
  }

  const offChange = eq.onChange(({ laneId }) => {
    if (laneId === null) {
      masterUndo = null;
      if (open) setOpen(false);
      return;
    }
    if (laneId !== lane) return;
    if (masterUndo && JSON.stringify(eq.getLaneEq(lane)) === masterUndo.before) {
      master.setMaster(masterUndo.prevMaster);
      masterUndo = null;
    }
    // The lane's EQ changed under an open card: the matched current and a
    // merge depend on it. Asked again once the change settles (the server
    // answers from its cache).
    if (open) {
      clearTimeout(refetchTimer);
      refetchTimer = setTimeout(() => { if (open) fetchAnswer(); }, REFETCH_MS);
    }
  });

  btn.addEventListener("click", () => setOpen(!open));
  closeBtn.addEventListener("click", () => setOpen(false));
  dismissBtn.addEventListener("click", () => setOpen(false));
  applyBtn.addEventListener("click", apply);
  targetSel.addEventListener("change", () => {
    target = targetSel.value;
    fetchAnswer();
  });
  card.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      setOpen(false);
      btn.focus();
    }
  });

  // ─── Painting ───

  function paintListen() {
    for (const b of listenBtns) b.setAttribute("aria-pressed", String(b.dataset.listen === listen));
    const ok = answer?.status === "ok" && !!answer.bands?.length;
    for (const b of listenBtns) b.disabled = b.dataset.listen !== "off" && !ok;
  }

  function paint() {
    btn.textContent = t("suggest.button");
    btn.title = t(isMaster ? "suggest.buttonTitleMaster" : "suggest.buttonTitle");
    if (!open) return;
    const opts = answer ? targetOptions(answer, t) : [];
    const sig = opts.map((o) => `${o.id}:${o.label}`).join("|");
    if (targetSel.dataset.sig !== sig) {
      targetSel.replaceChildren(...opts.map((o) => {
        const opt = el("option", null, o.label);
        opt.value = o.id;
        return opt;
      }));
      targetSel.dataset.sig = sig;
    }
    if (answer?.target) targetSel.value = answer.target;
    targetSel.disabled = !opts.length;
    for (const b of modeBtns) b.setAttribute("aria-pressed", String(b.dataset.mode === mode));

    const ok = answer?.status === "ok";
    why.replaceChildren();
    if (loading) {
      status.textContent = t("suggest.measuring");
    } else if (failed) {
      status.textContent = t("suggest.failed");
    } else if (answer?.status === "quiet") {
      status.textContent = t("suggest.why.quiet");
    } else if (ok) {
      status.textContent = answer.bands.length ? t("suggest.ready", { n: answer.bands.length }) : t("suggest.nothing");
      for (const line of reasonLines(answer, t)) why.append(el("li", null, line));
    } else {
      status.textContent = "";
    }
    const line = ok && !loading ? presetLine(answer, t) : "";
    preset.textContent = line;
    preset.hidden = !line;
    card.classList.toggle("is-loading", loading);
    applyBtn.disabled = !ok || loading || (!answer.bands.length && !answer.master);
    paintListen();
    onGhost?.(ok && !loading ? suggestion() : null);
  }

  function relabel() {
    title.textContent = t(isMaster ? "suggest.titleMaster" : "suggest.title");
    closeBtn.setAttribute("aria-label", t("suggest.dismiss"));
    closeBtn.title = t("suggest.dismiss");
    targetName.textContent = t("suggest.target");
    if (modeSeg) {
      modeSeg.setAttribute("aria-label", t("suggest.mode"));
      for (const b of modeBtns) {
        b.textContent = t(`suggest.mode.${b.dataset.mode}`);
        b.title = t(`suggest.mode.${b.dataset.mode}Title`);
      }
    }
    taste.textContent = t("suggest.taste");
    listenName.textContent = t("suggest.preview");
    listenSeg.setAttribute("aria-label", t("suggest.previewAria"));
    for (const b of listenBtns) {
      b.textContent = t(`suggest.listen.${b.dataset.listen}`);
      b.title = t(`suggest.listen.${b.dataset.listen}Title`);
    }
    applyBtn.textContent = t(isMaster ? "suggest.applyMaster" : "suggest.apply");
    applyBtn.title = t("suggest.applyTitle");
    dismissBtn.textContent = t("suggest.dismiss");
    targetSel.dataset.sig = "";
    paint();
  }

  relabel();

  return {
    button: btn,
    card,
    isOpen: () => open,
    open: () => setOpen(true),
    close: () => setOpen(false),
    relabel,
    /** The current answer (tests and the Guide). */
    answer: () => answer,
    destroy() {
      ctl?.abort();
      clearTimeout(refetchTimer);
      if (open) eq.previewLaneEq(lane, null);
      offChange();
      card.remove();
      btn.remove();
    },
  };
}
