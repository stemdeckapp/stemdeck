// Loading the worklet, and how late its output runs, for both audio engines.
//
// Both engines drive the same processor and used to carry their own copy of
// its latency. Signalsmith Stretch (#729) made that a formula that depends on
// the tempo and on which stage loaded, and a third copy of it would drift.

const CORE_URL = "/vendor/signalsmith-stretch.js";
const PROCESSOR_URL = "/vendor/soundtouch-processor.js";

/**
 * Add the Signalsmith core, then the processor.
 *
 * The core is optional. A browser without WebAssembly in worklets, or a CSP
 * without 'wasm-unsafe-eval', fails here, and the processor then keeps WSOLA
 * as its tempo stage. Only the processor failing fails the whole thing.
 */
export async function loadStretchWorklet(audioWorklet) {
  try {
    await audioWorklet.addModule(CORE_URL);
  } catch (err) {
    console.warn("[tempoStage] Signalsmith core did not load, keeping WSOLA:", err);
  }
  await audioWorklet.addModule(PROCESSOR_URL);
}

// WSOLA's tempo stage, as the engines have always counted it.
function wsolaLatencySeconds(sampleRate) {
  const needed = Math.round(0.012 * sampleRate)
    + Math.round(0.028 * sampleRate)
    + Math.round(0.082 * sampleRate);
  return Math.floor(needed / 128) * 128 / sampleRate;
}

/**
 * Seconds between a source sample entering the worklet and it being heard.
 *
 * @param {object} p
 * @param {number} p.workletFrames  the processor's `latency` message: the
 *   priming every bus goes through once the worklet is doing anything
 * @param {object|null} p.stage     the processor's latest `tempoStage` message
 * @param {number} p.rate           playback rate
 * @param {boolean} p.transposed    whether any lane is off its own key
 * @param {number} p.sampleRate
 */
export function pipelineLatencySeconds({ workletFrames, stage, rate, transposed, sampleRate }) {
  const tempoActive = Math.abs(rate - 1) >= 1e-3;
  // The pitch buses only buffer once something is actually transposed. Until
  // then the worklet hands its input straight back, with no delay to correct.
  const pitchSeconds = transposed ? workletFrames / sampleRate : 0;
  if (!tempoActive) return pitchSeconds;
  if (stage?.stage === "signalsmith") {
    // The priming and the core's input side are counted in input samples, so
    // they stretch with the tempo; its output side does not. Measured against
    // the processor end to end, this lands within a millisecond.
    return ((workletFrames + stage.inputFrames) / rate + stage.outputFrames) / sampleRate;
  }
  return pitchSeconds + wsolaLatencySeconds(sampleRate);
}

// How long a teardown waits for the processor to confirm it stopped. It
// answers within a render quantum (about 3 ms) while the context runs; a
// suspended context never calls it, and is closed after this anyway.
const DISPOSE_WAIT_MS = 300;

/**
 * Tear down the stretch node and, when `closeCtx` is true, the context it
 * runs in.
 *
 * A closed context is not freed while an AudioWorkletNode in it still has an
 * active processor: Chromium keeps the node alive as a pending activity, and
 * the node holds the context and, through its port's handler, the engine
 * that made it. Measured: one closed context kept per song load, for good
 * (with the probe context audioContext.js used to open, two). So the
 * processor is told to stop (soundtouch-processor.js returns false from
 * process()), and the context is closed once it has, or after
 * DISPOSE_WAIT_MS. Without a node the context is closed at once. Never
 * throws.
 */
export function disposeStretchNode(node, ctx, closeCtx) {
  const close = () => {
    if (closeCtx) Promise.resolve().then(() => ctx.close()).catch(() => {});
  };
  if (!node) { close(); return; }
  // Disconnected first, and that matters: a node whose output feeds nodes
  // that no longer reach the destination is never pulled, so its process()
  // would never run to see the message. One with no output connected is
  // rendered on its own (Chromium's automatic pull), so it answers.
  try { node.disconnect(); } catch { /* already disconnected */ }
  let timer = 0;
  const finish = () => {
    clearTimeout(timer);
    node.port.onmessage = null;
    try { node.port.close(); } catch { /* already closed */ }
    close();
  };
  node.port.onmessage = (event) => { if (event?.data?.type === "disposed") finish(); };
  timer = setTimeout(finish, DISPOSE_WAIT_MS);
  try { node.port.postMessage({ type: "dispose" }); } catch { finish(); }
}
