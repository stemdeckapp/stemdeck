// The recording panel's Live effects: the armed track's input heard through
// a chain of pedals while Hear myself is on (inputFx.js does the audio).
//
//   Live effects                         Effects of [Track 1]  (2+ armed)
//   Heard live: with these effects. Recorded: your clean input...
//   Effects (Track 1) [On]  Instrument [Auto (Guitar)]  Trim  meter  Clip
//   [ ] Low latency
//   You hear yourself about 14 ms late: 10 ms round trip (measured) + 4.0 ms
//   In > [Drive] > [Delay] > [+ Add effect] > Out
//   the selected pedal: preset, selects, knobs
//
// The strip is the lanes' strip (fx/chainStrip.js), with the input as its
// source. Also here: the speakers warning beside Hear myself, shown while
// monitoring plays to what is probably a speaker.

import { t, onLanguageChange } from "../i18n.js";
import { inputChainsAllowed } from "../fxEngine.js";
import { mountChainStrip } from "../fx/chainStrip.js";
import { trackInput, getTracks } from "./takeLanes.js";
import { SETTINGS_EVENT } from "./captureSettings.js";
import * as inp from "./inputFx.js";
import { liveInputSource, latencyText, speakersWarning } from "./liveFxShared.js";

const $ = (id) => document.getElementById(id);

let root = null;
let parts = null; // { side, body, head, ... } built once
let mounted = null; // { trackId, strip }
let chosen = null; // the track picked in "Effects of"
let onLayout = () => {};
let latencyToken = 0;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

const trackOf = (id) => getTracks().find((tr) => tr.id === id) || null;

function rawPeak(trackId) {
  const c = inp.inputCapture();
  const tr = trackOf(trackId);
  if (!c || !tr) return 0;
  return c.channelLevel(trackInput(tr).channels);
}

/** A track's input as a chain source for the shared strip. */
function inputSource(trackId) {
  return liveInputSource(inp.studioInputFx, trackId, {
    name: () => trackOf(trackId)?.name || trackId,
    rawPeak: () => rawPeak(trackId),
  });
}

function build() {
  root = $("recLiveFx");
  if (!root || parts) return;
  const head = el("div", "rec-live-head");
  const title = el("span", "rec-live-title");
  title.id = "recLiveFxTitle";
  const pickWrap = el("label", "rec-live-pick");
  const pickText = el("span", "rec-live-pick-label");
  const pick = el("select", "fx-select rec-live-track");
  pick.id = "recLiveFxTrack";
  pick.addEventListener("change", () => { chosen = pick.value; renderLiveFx(); });
  pickWrap.append(pickText, pick);
  head.append(title, pickWrap);
  const dry = el("p", "rec-note rec-live-dry");
  dry.id = "recLiveFxDry";
  const off = el("p", "rec-note rec-live-off");
  off.id = "recLiveFxOff";
  const empty = el("p", "rec-note rec-live-empty");
  empty.id = "recLiveFxEmpty";
  const side = el("div", "eq-side fx-side rec-live-side");
  const lowWrap = el("label", "rec-check rec-live-low");
  const low = el("input");
  low.type = "checkbox";
  low.id = "recLiveFxLow";
  const lowText = el("span");
  lowWrap.append(low, lowText);
  low.addEventListener("change", () => {
    if (mounted) inp.setInputLowLatency(mounted.trackId, low.checked);
  });
  const latency = el("p", "rec-note rec-live-latency");
  latency.id = "recLiveFxLatency";
  const body = el("div", "eq-panel-body fx-panel-body rec-live-body");
  body.setAttribute("role", "region");
  root.append(head, dry, off, empty, side, lowWrap, latency, body);
  root.setAttribute("aria-labelledby", "recLiveFxTitle");
  parts = { head, title, pickText, pick, dry, off, empty, side, lowWrap, low, lowText, latency, body };
}

function unmount() {
  if (!mounted) return;
  mounted.strip.destroy();
  parts.side.replaceChildren();
  parts.body.replaceChildren();
  mounted = null;
}

async function renderLatencyLine(trackId) {
  const mine = ++latencyToken;
  const line = parts.latency;
  const text = latencyText(await inp.heardLatency(trackId));
  if (mine !== latencyToken) return;
  line.textContent = text;
  line.hidden = !text;
}

/** Draw the section for what is armed now (cheap: the strip is kept). */
export function renderLiveFx() {
  build();
  if (!root || !parts) return;
  const allowed = inputChainsAllowed("studio");
  root.hidden = !allowed;
  if (!allowed) {
    unmount();
    $("recPanel")?.classList.remove("has-live-fx");
    renderSpeakers();
    return;
  }
  // A closed panel draws nothing (hideLiveFx); opening it renders again.
  if ($("recPanel")?.hidden) {
    unmount();
    renderSpeakers();
    return;
  }
  const armed = inp.audioArmedTracks();
  parts.title.textContent = t("rec.liveFx.title");
  parts.pickText.textContent = t("rec.liveFx.track");
  parts.dry.textContent = t("rec.liveFx.dry");
  parts.off.textContent = t("rec.liveFx.monitorOff");
  parts.empty.textContent = t("rec.liveFx.empty");
  parts.lowText.textContent = t("rec.liveFx.low");
  parts.pick.setAttribute("aria-label", t("rec.liveFx.trackAria"));
  const has = armed.length > 0;
  $("recPanel")?.classList.toggle("has-live-fx", has);
  parts.empty.hidden = has;
  for (const n of [parts.head, parts.dry, parts.side, parts.lowWrap, parts.body]) n.hidden = !has;
  parts.pick.closest(".rec-live-pick").hidden = armed.length < 2;
  parts.off.hidden = !has || inp.isMonitoring();
  if (!has) {
    unmount();
    parts.latency.hidden = true;
    renderSpeakers();
    onLayout();
    return;
  }
  if (!armed.some((tr) => tr.id === chosen)) chosen = armed[0].id;
  parts.pick.replaceChildren(...armed.map((tr) => new Option(tr.name || tr.id, tr.id)));
  parts.pick.value = chosen;
  if (mounted?.trackId !== chosen) {
    unmount();
    mounted = { trackId: chosen, strip: mountChainStrip(inputSource(chosen), parts.side, parts.body, parts.body) };
  } else {
    mounted.strip.refresh();
  }
  parts.low.checked = inp.getInputFx(chosen).low;
  renderLatencyLine(chosen);
  renderSpeakers();
  onLayout();
}

/**
 * The warning beside Hear myself: shown while monitoring plays to an output
 * whose name says speakers (fx/inputChainModel.js outputKind). An
 * interface's or an unnamed output says nothing either way: the panel's
 * standing headphones note covers those.
 */
async function renderSpeakers() {
  const warn = $("recSpeakersWarn");
  if (!warn) return;
  if (!inp.isMonitoring()) { warn.hidden = true; return; }
  const text = await speakersWarning();
  const speakers = inp.isMonitoring() && !!text;
  warn.hidden = !speakers;
  if (speakers) warn.textContent = text;
}

/**
 * The panel closed: the strip goes (its meter stops reading the input every
 * frame). The chain itself is untouched and keeps sounding while monitored;
 * opening the panel draws the strip again (renderLiveFx).
 */
export function hideLiveFx() {
  if (!parts) return;
  unmount();
  latencyToken++;
}

/** Once, from recordUi.initRecordUi. `onLayout` re-places the panel. */
export function initLiveFxUi(opts = {}) {
  onLayout = opts.onLayout || (() => {});
  build();
  // The routes changed (a capture opened, a track armed, Low latency): the
  // latency line follows; a chain's own change re-renders its strip itself.
  inp.onInputFxChange(({ trackId }) => {
    if (!mounted) return;
    if (trackId === null || trackId === mounted.trackId) {
      parts.low.checked = inp.getInputFx(mounted.trackId).low;
      renderLatencyLine(mounted.trackId);
    }
  });
  document.addEventListener(SETTINGS_EVENT, (ev) => { if (ev.detail?.kind === "calibration") renderLiveFx(); });
  document.addEventListener("stemdeck:mix-switched", () => renderLiveFx());
  navigator.mediaDevices?.addEventListener?.("devicechange", () => renderSpeakers());
  onLanguageChange(() => renderLiveFx());
}
