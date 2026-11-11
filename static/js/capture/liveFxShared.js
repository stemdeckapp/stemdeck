// What every live input effects panel shares: the recording panel's
// (capture/liveFxUi.js) and Performance's (streamer/streamerFx.js). The
// strip itself is fx/chainStrip.js; this is the input as its source, the
// latency sentence and the speakers check, so both panels say and measure
// the same thing in the same words.

import { t } from "../i18n.js";
import { listDevices } from "./captureDevices.js";
import { outputKind, defaultOutputLabel, inputHostWanted } from "../fx/inputChainModel.js";

const dbToGain = (db) => (db === 0 ? 1 : 10 ** (db / 20));

/**
 * A live input as a chain source for the shared strip (fx/chainStrip.js).
 * `ctl` is an inputFx controller (capture/inputFx.js createInputFx), `id`
 * its route. `name()` labels the strip; `rawPeak()` is the input's own peak
 * on its channels now (0..1, 0 with no input).
 */
export function liveInputSource(ctl, id, { key = `input:${id}`, name, rawPeak, color = "var(--accent)" }) {
  return {
    key,
    name,
    color,
    get: () => ctl.getInputFx(id).chain,
    set: (desc) => ctl.setInputChain(id, desc),
    inst: () => ctl.inputChainInstrument(id),
    defaultInst: () => ctl.defaultInst(id),
    // What goes into the pedals: the input after the trim. With the chain
    // off (or nothing in it) nothing is trimmed: the input as it comes.
    meterPeak() {
      if (!ctl.inputCapture()) return null;
      const d = ctl.getInputFx(id).chain;
      const raw = rawPeak();
      return d.on !== false && inputHostWanted(d) ? raw * dbToGain(d.trim) : raw;
    },
    // The input itself at full scale clips the recording: no trim undoes it.
    clipPeak: () => rawPeak(),
    onChange: (cb) => ctl.onInputFxChange(({ trackId }) => { if (trackId === id) cb(); }),
  };
}

/**
 * The latency sentence for monitorLatency's result, or "" when no round
 * trip is known (no input open): never a number that was not measured or
 * reported.
 */
export function latencyText(lat) {
  if (!lat || lat.totalMs === null) return "";
  return t("rec.liveFx.latency", {
    total: Math.round(lat.totalMs),
    rt: Math.round(lat.roundTripMs),
    how: t(lat.measured ? "rec.liveFx.measured" : "rec.liveFx.estimated"),
    fx: lat.fxMs.toFixed(1),
  });
}

/**
 * The speakers warning's text while monitoring plays to an output whose
 * name says speakers (fx/inputChainModel.js outputKind), else "". An
 * interface's or an unnamed output says nothing either way: the standing
 * headphones note covers those.
 */
export async function speakersWarning() {
  let label = "";
  try {
    label = defaultOutputLabel(await listDevices("audiooutput"));
  } catch (err) {
    console.warn("[rec] reading the output device failed:", err);
  }
  if (outputKind(label) !== "speakers") return "";
  return t("rec.speakersWarn", { name: label.replace(/^(default|communications)\s*-\s+/i, "").trim() });
}
