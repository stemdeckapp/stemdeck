// Performance's live effects: the musician's voice or instrument heard
// through a chain of pedals while "Hear myself through StemDeck" is on, in
// the Effects tab beside the Timeline tab (streamerTabs.js).
//
//   input -> its channels (mono in the centre) -> [trim -> pedals] ->
//     monitor gain -> output
//
// Everything audible is the studio's, reused: the capture graph's monitor
// route (capture/captureGraph.js) on the input Performance already opens
// (streamerDevices.js), one inputFx controller (capture/inputFx.js
// createInputFx, surface "performance"), the lanes' chain host and pedals,
// and the strip (fx/chainStrip.js, left to right as on a lane).
//
// What the effect affects:
// - Heard live: the input through the chain (nothing while Hear myself is
//   off: no host is built).
// - Recorded: the clean input. The take's recorder taps the input before any
//   route (captureGraph.js), exactly as in Advanced.
// - The take keeps a copy of the chain as it was when it began (its take
//   record, streamerRecord.takeInfo `fx`), and its lane, the Performance
//   track's, plays it through that copy (fxEngine.laneChainAllowed): the take
//   that plays chooses the chain, so an older take plays with its own.
// - The MP4 export prints the take through that copy in the browser
//   (fx/fxPrint.js, the take id as the print's lane) and the server renders
//   the print in place of the take's audio (app/api/takes.py take_print).
//
// What is kept: the chain (instrument and trim included) and Low latency, in
// Performance's recording settings (outputSettings.js inputFx). Hear myself
// is not kept: it is off at every page load and whenever Performance closes
// (a monitor that comes back on by itself next to a microphone and speakers
// can feed back). Which tab shows is kept by streamerTabs.js; the strip is
// drawn only while the Effects tab shows (setStreamerFxShown), and nothing
// audible depends on it: the monitor and its chain play on either tab.

import { t, onLanguageChange } from "../i18n.js";
import * as fx from "../fxEngine.js";
import { createInputFx } from "../capture/inputFx.js";
import { liveInputSource, latencyText, speakersWarning } from "../capture/liveFxShared.js";
import { SETTINGS_EVENT } from "../capture/captureSettings.js";
import { mountChainStrip } from "../fx/chainStrip.js";
import { inputDefaultInstrument, inputFxIsSet } from "../fx/inputChainModel.js";
import { chainIsActive, chainIsSet, emptyChain, normalizeChain, chainInstrument } from "../fx/chainModel.js";
import { mixerState, trackIndex } from "../state.js";
import { getMixMode } from "../mixer.js";
import { isMixSwitching } from "../mixMode.js";
import { getStreamerMedia } from "./streamerDevices.js";
import { takeInfo, setTakeFx } from "./streamerRecord.js";

/** The one route Performance monitors: its input. */
export const ROUTE = "performance";

// From streamer.js (initStreamerFx): the input's channels and kind, the
// stored record and how to store one.
let host = {
  channels: () => null,
  kind: () => null,
  read: () => null,
  write: () => {},
};

/** Performance's live input effects controller. */
export const perfFx = createInputFx({
  surface: "performance",
  tag: "streamerFx",
  routes: () => {
    const ch = host.channels();
    return Array.isArray(ch) && ch.length ? [{ id: ROUTE, channels: ch }] : [];
  },
  read: () => host.read() || undefined,
  write: (_id, rec) => host.write(inputFxIsSet(rec) ? { chain: rec.chain, low: rec.low === true } : null),
  defaultInst: () => inputDefaultInstrument(host.kind() || null),
});

let hear = false;
let attached = null;
let routeSig = "";
let ui = null;
let strip = null;
// The Effects tab shows (streamer.js, from its tabs).
let shown = false;
let latencyToken = 0;
let speakersToken = 0;
let viewOpen = false;

/** Hear myself is on (the switch; off at every page load). */
export function isHearing() {
  return hear;
}

/** Hear myself on or off: the chains first, then the monitor's gain. */
export function setHearing(on) {
  hear = !!on;
  // As the recording panel: the chain is built (or taken out, ducked, while
  // the monitor still plays) before the monitor's gain moves.
  perfFx.setMonitoring(hear);
  attached?.setMonitor(hear);
  ui?.hearSwitch.set(hear);
  paintNotes();
  paintSpeakers();
  paintLatency();
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function rawPeak() {
  const c = perfFx.inputCapture();
  const ch = host.channels();
  if (!c || !Array.isArray(ch) || !ch.length) return 0;
  return c.channelLevel(ch);
}

/**
 * Build the controls once (streamer.js buildTimeline, the Effects tab):
 * `makeSwitch` and `tx` are the view's own (the design's switch, and a text
 * set again on a language change). Returns { monitor, effects, badge }: the
 * Hear myself block, the Live effects section under it, and the "On" badge
 * for the Effects tab (lit while the chain changes the sound, so it reads
 * from the Timeline tab too).
 */
export function buildStreamerFx({ makeSwitch, tx }) {
  const monitor = el("div", "sm-monitor");
  const hearSwitch = makeSwitch("sm-monitor-switch", "rec.monitor", hear, (on) => setHearing(on));
  hearSwitch.b.id = "streamerHear";
  // The headphones note shows while Hear myself is on, when it applies.
  const note = tx(el("p", "sm-fx-note sm-monitor-note"), "rec.headphonesNote");
  note.id = "streamerHeadphones";
  note.hidden = !hear;
  const warn = el("p", "sm-fx-warn");
  warn.id = "streamerSpeakersWarn";
  warn.setAttribute("role", "alert");
  warn.hidden = true;
  monitor.append(hearSwitch.b, note, warn);

  const section = el("section", "sm-fx");
  section.id = "streamerFx";
  const title = tx(el("h3", "sm-fx-title"), "rec.liveFx.title");
  title.id = "streamerFxTitle";
  section.setAttribute("aria-labelledby", "streamerFxTitle");
  const dry = tx(el("p", "sm-fx-note sm-fx-dry"), "streamer.fx.dry");
  const state = el("p", "sm-fx-note sm-fx-state");
  state.id = "streamerFxState";
  // The input's side (power, instrument, trim, meter, Clip), Low latency and
  // the latency readout on the left; the chain and the open pedal on the right.
  const grid = el("div", "sm-fx-grid");
  const controls = el("div", "sm-fx-controls");
  const side = el("div", "eq-side fx-side sm-fx-side");
  const low = makeSwitch("sm-fx-low", "rec.liveFx.low", perfFx.getInputFx(ROUTE).low, (on) => perfFx.setInputLowLatency(ROUTE, on));
  const latency = el("p", "sm-fx-note sm-fx-latency");
  latency.id = "streamerFxLatency";
  latency.hidden = true;
  controls.append(side, low.b, latency);
  const body = el("div", "sm-fx-body");
  body.setAttribute("role", "region");
  grid.append(controls, body);
  section.append(title, dry, state, grid);
  const badge = el("span", "sm-tab-badge");
  badge.setAttribute("aria-hidden", "true");
  badge.hidden = true;
  ui = { hearSwitch, note, warn, badge, section, state, side, low, latency, body };
  paintSection();
  return { monitor, effects: section, badge };
}

/**
 * The Effects tab shows (true) or the Timeline tab does: the strip draws
 * while it shows and goes while it hides (its meter stops reading). Hear
 * myself, the monitor route and its chain stay as they are either way.
 */
export function setStreamerFxShown(on) {
  shown = !!on;
  paintSection();
}

function source() {
  return liveInputSource(perfFx, ROUTE, {
    key: "input:performance",
    name: () => t("streamer.fx.inputName"),
    rawPeak,
  });
}

function unmount() {
  if (!strip) return;
  strip.destroy();
  strip = null;
  ui?.side.replaceChildren();
  ui?.body.replaceChildren();
}

/** The tab's badge, and the strip drawn while the Effects tab shows. */
function paintSection() {
  if (!ui) return;
  const active = chainIsActive(perfFx.getInputFx(ROUTE).chain);
  ui.badge.hidden = !active;
  ui.badge.textContent = active ? t("fx.on") : "";
  if (shown && viewOpen) {
    if (!strip) strip = mountChainStrip(source(), ui.side, ui.body, ui.body);
    else strip.refresh();
  } else unmount();
  ui.low.set(perfFx.getInputFx(ROUTE).low);
  paintNotes();
  paintLatency();
}

function paintNotes() {
  if (!ui) return;
  ui.note.hidden = !hear;
  const hasInput = !!perfFx.inputCapture();
  const key = !hasInput ? "streamer.fx.noInput" : !hear ? "rec.liveFx.monitorOff" : "";
  ui.state.hidden = !key;
  ui.state.textContent = key ? t(key) : "";
}

async function paintLatency() {
  if (!ui) return;
  const mine = ++latencyToken;
  const text = shown ? latencyText(await perfFx.heardLatency(ROUTE)) : "";
  if (mine !== latencyToken || !ui) return;
  ui.latency.textContent = text;
  ui.latency.hidden = !text;
}

/** The speakers warning under Hear myself (the recording panel's rule). */
async function paintSpeakers() {
  if (!ui) return;
  const mine = ++speakersToken;
  if (!hear) { ui.warn.hidden = true; return; }
  const text = await speakersWarning();
  if (mine !== speakersToken) return;
  ui.warn.hidden = !(hear && text);
  if (hear && text) ui.warn.textContent = text;
}

/**
 * Each frame while Performance is open: follow the input (a new capture, a
 * new choice of channels or kind) and the take that plays.
 */
export function paintStreamerFx() {
  const cap = getStreamerMedia().capture || null;
  if (cap !== attached) {
    attached = cap && !cap.closed ? cap : null;
    perfFx.attachCapture(attached);
    // A new input starts silent (captureGraph.js): it plays only if Hear
    // myself is on, once its routes are built.
    attached?.setMonitor(hear);
    paintSection();
  }
  const ch = host.channels();
  const sig = `${Array.isArray(ch) ? ch.join(",") : ""}|${host.kind() || ""}`;
  if (sig !== routeSig) {
    routeSig = sig;
    perfFx.apply();
    strip?.refresh();
    paintLatency();
  }
}

/** The settings were read (or changed elsewhere): the chain follows them. */
export function streamerFxSettingsChanged() {
  perfFx.apply();
  paintSection();
}

/** Performance opened: the strip draws while its section shows. */
export function streamerFxOpened() {
  viewOpen = true;
  paintSection();
}

/**
 * Performance closed: the input closes with it (streamerDevices.stopDevices),
 * Hear myself goes off and the strip goes (its meter stops reading).
 */
export function streamerFxClosed() {
  viewOpen = false;
  setHearing(false);
  attached = null;
  perfFx.attachCapture(null);
  unmount();
}

/**
 * The live effects a take beginning now is heard through, for its take
 * record: {chain, inst} when the chain is on and changing the sound and the
 * take records an input, else null (the take then plays dry).
 */
export function liveTakeFx(hasInput) {
  if (!hasInput || !fx.inputChainsAllowed("performance")) return null;
  const rec = perfFx.getInputFx(ROUTE);
  if (!chainIsActive(rec.chain)) return null;
  return { chain: rec.chain, inst: perfFx.inputChainInstrument(ROUTE) };
}

/** A take record's chain as its lane plays it (normalised, the instrument written out), or null. */
export function takeChain(fxRec) {
  if (!fxRec?.chain) return null;
  return { ...normalizeChain(fxRec.chain, fxRec.inst), inst: chainInstrument(fxRec.chain, fxRec.inst) };
}

/**
 * The Performance track's lane plays the chain of the take that plays on it
 * (the take record of this browser): set whenever what the lane has differs
 * from what that take wants (another take chosen, its Effects switch, a mix
 * read again after a reload). A take this browser has no record of
 * (recorded elsewhere) leaves the lane as it is. Nothing changes while a
 * take records, the mix is switching or the lane has not loaded.
 */
export function syncTakeChain({ job, trackId, takeId, locked }) {
  if (!job || !trackId || !takeId || locked) return;
  if (getMixMode() !== "streamer" || isMixSwitching()) return;
  if (!mixerState[trackId] || trackIndex[trackId] === undefined) return;
  const info = takeInfo(takeId);
  if (!info) return;
  const want = takeChain(info.fx);
  const wanted = want ? normalizeChain(want, want.inst) : emptyChain();
  if (JSON.stringify(wanted) !== JSON.stringify(fx.getLaneChain(trackId))) fx.setLaneChain(trackId, want || emptyChain());
}

/**
 * The take lane's Effects switch: { show, on } for take `takeId` (shown
 * when its record has a chain).
 */
export function takeFxState(takeId) {
  const info = takeId ? takeInfo(takeId) : null;
  const chain = takeChain(info?.fx);
  return { show: !!chain && chainIsSet(chain) && chain.pedals.length > 0, on: !!chain && chain.on !== false };
}

/** The take lane's Effects switch pressed: the take's chain on or off. */
export function toggleTakeFx(takeId) {
  const info = takeId ? takeInfo(takeId) : null;
  if (!info?.fx) return false;
  const on = info.fx.chain.on !== false;
  return setTakeFx(takeId, { ...info.fx, chain: { ...info.fx.chain, on: !on } });
}

/**
 * What the MP4 export prints for take `takeId`: {lane, url, desc, inst, bpm}
 * for fx/fxPrint.ensurePrint (the take id is the print's lane, so the take
 * exported is the one printed, whichever plays), or null when its chain
 * changes nothing (switched off, empty) or this browser has no record.
 */
export function takePrintItem(jobId, takeId) {
  const info = takeId ? takeInfo(takeId) : null;
  const desc = takeChain(info?.fx);
  if (!desc || !chainIsActive(desc)) return null;
  return {
    lane: takeId,
    url: `/api/jobs/${encodeURIComponent(jobId)}/takes/${encodeURIComponent(takeId)}/audio.wav`,
    desc,
    inst: desc.inst,
    bpm: fx.chainTempo(),
  };
}

let inited = false;

/**
 * Once, from streamer.initStreamer: how to read the input and the stored
 * record. `channels()` the channels the take records, `kind()` the input's
 * kind ("voice" for a microphone), `read()` the stored {chain, low}, and
 * `write(rec)` stores it (null: nothing set). `trackId()` is the open song's
 * Performance track (its lane plays its take's chain).
 */
export function initStreamerFx({ channels, kind, read, write, trackId }) {
  host = { channels, kind, read, write };
  if (inited) return;
  inited = true;
  perfFx.init();
  fx.setPerformanceLane(trackId);
  perfFx.onInputFxChange(({ trackId: id }) => {
    if (id === ROUTE) paintSection();
    else paintLatency();
  });
  document.addEventListener(SETTINGS_EVENT, (ev) => { if (ev.detail?.kind === "calibration") paintLatency(); });
  navigator.mediaDevices?.addEventListener?.("devicechange", () => paintSpeakers());
  onLanguageChange(() => {
    strip?.refresh();
    paintSection();
    paintSpeakers();
  });
}
