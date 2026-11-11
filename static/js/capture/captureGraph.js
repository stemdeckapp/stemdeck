// The capture graph: one input device, opened in the same AudioContext as
// playback so a recorded frame and a played frame share one clock
// (05 section 5.2).
//
//   MediaStreamAudioSourceNode -> inputNode (GainNode, input trim, all channels)
//     ChannelSplitter -> one AnalyserNode per channel, made on first use
//        (the meters: the device's loudest channel, an armed track's own)
//     ChannelSplitter -> one route per armed track's channels (a mono
//        channel in the centre) -> [the track's live chain host] ->
//        monitorGain (0 by default) -> destination
//     inputNode -> ChannelSplitter -> ChannelMerger (1 or 2 channels) ->
//        recorder ("capture-recorder" worklet) -> 0-gain sink -> destination
//
// One stream per device, any number of recorders on it: each recording track
// takes one channel (mono) or a pair (stereo) of the same device, so tracks
// armed together share one clock by construction. Recorders are pulled
// through a silent sink so no engine can decide an unconnected worklet does
// not need to run. Monitoring goes straight to the destination, past the
// master, the lanes' effects and the tempo worklet, so it adds nothing to
// the round trip but its own chain's latency.
//
// A live chain (capture/inputFx.js decides when) is the lanes' chain host
// (fx/chainHost.js), reused: it sits in the monitor route only. The
// recorders tap the splitter, before any route, so a take is always the
// clean input: what is heard is wet, what is recorded is dry.
//
// Echo cancellation, noise suppression and auto gain are always off: the first
// removes the backing track from the take and pumps the instrument, the last
// rides the level of a dynamic performance.

import { requestMedia } from "./capturePermission.js";
import { createChainHost, laterIn } from "../fx/chainHost.js";
import { estimateRoundTripSec } from "./takeMath.js";
import { loadCalibration } from "./latencyCalibration.js";

const WORKLET_URL = "/js/capture/recorder-worklet.js";
const _loaded = new WeakMap(); // AudioContext -> Promise of addModule
// How long Stop waits for the worklet's last batch. A running one answers in
// a few milliseconds; a suspended context (a background tab) can take longer.
const STOP_ANSWER_MS = 3000;
// A monitor route's dip while its chain host goes in or out (as a lane's).
const ROUTE_FADE = 0.006;
const ROUTE_HOLD = 0.02;

function loadWorklet(ctx) {
  let p = _loaded.get(ctx);
  if (!p) {
    p = ctx.audioWorklet.addModule(WORKLET_URL);
    _loaded.set(ctx, p);
    p.catch(() => _loaded.delete(ctx));
  }
  return p;
}

// A meter in decibels, as a mixing desk draws one: -60 dBFS is empty, 0 dBFS
// full. A linear meter puts a -30 dBFS line signal (a mixer's USB send, a
// guitar on an interface) at 3% of its width, which reads as "nothing".
export const METER_FLOOR_DB = -60;

/** 0..1 width of a meter for a linear peak (0..1+). */
export function meterFraction(peak) {
  if (!(peak > 0)) return 0;
  const db = 20 * Math.log10(peak);
  return Math.max(0, Math.min(1, (db - METER_FLOOR_DB) / -METER_FLOOR_DB));
}

/** getUserMedia audio constraints. `channelCount` is what the tracks need. */
export function audioConstraints(deviceId, channelCount = 2) {
  return {
    ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
    channelCount: { ideal: Math.max(2, channelCount | 0) },
    latency: 0,
  };
}

/**
 * A capture with no input: what a video track's take records. A video track
 * is picture only, so no microphone opens and nothing is heard; the take
 * still records (silence) through the same recorder, because its first
 * frame is what places the camera in the song (the server's
 * camera_start_sec), exactly as an audio take is placed. Its round trip is
 * the output half only: there is no input to wait for.
 * Resolves { capture, reason: null } or { capture: null, reason }.
 */
export async function openSilentCapture(engine) {
  const ctx = engine?.audioContext;
  if (!ctx) return { capture: null, reason: "unknown" };
  if (!ctx.audioWorklet) {
    return { capture: null, reason: window.isSecureContext === false ? "insecure-context" : "unsupported" };
  }
  try {
    await loadWorklet(ctx);
  } catch (e) {
    console.warn("[capture] recorder worklet failed to load:", e);
    return { capture: null, reason: "unsupported" };
  }
  const silence = ctx.createConstantSource();
  silence.offset.value = 0;
  silence.start();
  const sink = ctx.createGain();
  sink.gain.value = 0;
  sink.connect(ctx.destination);
  const nodes = [];
  const fin = (v) => (Number.isFinite(v) && v >= 0 ? v : 0);
  const roundTrip = fin(ctx.outputLatency) + fin(ctx.baseLatency);
  let closed = false;
  const capture = {
    ctx,
    engine,
    stream: null,
    track: null,
    sampleRate: ctx.sampleRate,
    channels: 1,
    deviceChannels: 1,
    requestedDeviceId: "",
    fellBack: false,
    deviceId: "",
    deviceLabel: "",
    silent: true,
    estimate: {
      outputLatencyMs: Number.isFinite(ctx.outputLatency) ? ctx.outputLatency * 1000 : null,
      baseLatencyMs: Number.isFinite(ctx.baseLatency) ? ctx.baseLatency * 1000 : null,
      inputLatencyMs: null,
    },
    estimatedSec: roundTrip,
    roundTripSeconds: roundTrip,
    calibrated: false,
    calibration: null,
    createRecorder() {
      const node = new AudioWorkletNode(ctx, "capture-recorder", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: "explicit",
        channelInterpretation: "discrete",
        processorOptions: { channels: 1 },
      });
      silence.connect(node);
      node.connect(sink);
      nodes.push(node);
      let onBatch = null;
      let waitStarted = null;
      let waitStopped = null;
      node.port.onmessage = (e) => {
        const m = e.data || {};
        if (m.type === "batch") onBatch?.(m);
        else if (m.type === "started") { waitStarted?.(m); waitStarted = null; }
        else if (m.type === "stopped") { waitStopped?.(m); waitStopped = null; }
      };
      return {
        channels: 1,
        sourceChannels: [0],
        start(batchCb) {
          onBatch = batchCb;
          return new Promise((resolve) => {
            waitStarted = resolve;
            node.port.postMessage({ type: "start" });
          });
        },
        stop() {
          return new Promise((resolve) => {
            let timer = 0;
            const done = (m) => {
              clearTimeout(timer);
              waitStopped = null;
              onBatch = null;
              resolve(m);
            };
            if (ctx.state === "closed") { done({ type: "stopped", dropped: 0, contextClosed: true }); return; }
            waitStopped = done;
            node.port.postMessage({ type: "stop" });
            timer = setTimeout(() => done({ type: "stopped", dropped: 0, timedOut: true }), STOP_ANSWER_MS);
          });
        },
        dispose() {
          try { node.port.postMessage({ type: "dispose" }); } catch (e) { console.warn(e); }
          try { node.disconnect(); } catch { /* not connected */ }
        },
      };
    },
    applyCalibration() {},
    level() { return 0; },
    channelLevel() { return 0; },
    setMonitorChannels() {},
    setMonitorRoutes() {},
    setMonitor() {},
    monitorNode: null,
    chainHostCount: () => 0,
    routeLatencyFrames: () => 0,
    routeChainCount: () => 0,
    setTrim() {},
    onDeviceLost() { return () => {}; },
    get closed() { return closed; },
    close() {
      if (closed) return;
      closed = true;
      for (const n of [silence, sink, ...nodes]) {
        try { n.disconnect(); } catch { /* not connected */ }
      }
      try { silence.stop(); } catch { /* already stopped */ }
    },
  };
  return { capture, reason: null };
}

/**
 * Open an input on `engine`'s AudioContext.
 * Resolves { capture, reason: null } or { capture: null, reason }.
 */
export async function openCapture(engine, { deviceId = "", channelCount = 2 } = {}) {
  const ctx = engine?.audioContext;
  if (!ctx) return { capture: null, reason: "unknown" };
  // AudioWorklet is a secure-context API: absent on plain http to a LAN
  // address, which is also where getUserMedia is absent.
  if (!ctx.audioWorklet) {
    return { capture: null, reason: window.isSecureContext === false ? "insecure-context" : "unsupported" };
  }
  const { stream, reason, fellBack } = await requestMedia("audio", audioConstraints(deviceId, channelCount));
  if (!stream) return { capture: null, reason };
  const track = stream.getAudioTracks()[0];
  if (!track) {
    stream.getTracks().forEach((tr) => tr.stop());
    return { capture: null, reason: "no-device" };
  }
  try {
    await loadWorklet(ctx);
  } catch (e) {
    console.warn("[capture] recorder worklet failed to load:", e);
    stream.getTracks().forEach((tr) => tr.stop());
    return { capture: null, reason: "unsupported" };
  }
  const settings = track.getSettings?.() || {};
  // Every channel the device delivered; recorders pick from them.
  const deviceChannels = Math.max(1, settings.channelCount | 0 || 1);
  let source;
  try {
    source = ctx.createMediaStreamSource(stream);
  } catch (e) {
    // Firefox refuses a mic at a different rate from the context (Mozilla
    // bug 1664223). Chromium resamples, so this is the Firefox path only.
    console.warn("[capture] could not connect the input:", e);
    stream.getTracks().forEach((tr) => tr.stop());
    return { capture: null, reason: "rate-mismatch" };
  }
  // Discrete and explicit, so a 4-channel interface stays 4 channels instead
  // of being mixed down to stereo before the splitter sees it.
  const inputNode = ctx.createGain();
  inputNode.channelCount = deviceChannels;
  inputNode.channelCountMode = "explicit";
  inputNode.channelInterpretation = "discrete";
  source.connect(inputNode);

  const splitter = ctx.createChannelSplitter(deviceChannels);
  inputNode.connect(splitter);
  const sink = ctx.createGain();
  sink.gain.value = 0;
  sink.connect(ctx.destination);

  // Two channels, "speakers": a mono channel fed in is heard in both ears.
  const monitorGain = ctx.createGain();
  monitorGain.gain.value = 0;
  monitorGain.channelCount = 2;
  monitorGain.channelCountMode = "explicit";
  monitorGain.channelInterpretation = "speakers";
  monitorGain.connect(ctx.destination);
  // key -> { chans, merger, node, host }: one per armed track's channels.
  const routes = new Map();
  let wholeDevice = false;
  let monitorOn = false;

  // Per-channel analysers for the armed tracks' meters, made on first use.
  const chanMeters = new Map();
  function chanMeter(c) {
    let m = chanMeters.get(c);
    if (!m && c < deviceChannels) {
      const a = ctx.createAnalyser();
      a.fftSize = 1024;
      splitter.connect(a, c);
      m = { a, buf: new Float32Array(a.fftSize) };
      chanMeters.set(c, m);
    }
    return m || null;
  }

  // A dip on a route while its node is rewired at the bottom: connect() and
  // disconnect() take no time, so a rewire under signal clicks (the lanes'
  // chain hosts do the same, fx/chainHost.js duck). Silent monitoring needs
  // none.
  function duckRoute(r, fn) {
    if (!monitorOn) { fn(); return; }
    const g = r.node.gain;
    const at = ctx.currentTime;
    g.cancelScheduledValues(at);
    g.setValueAtTime(g.value, at);
    g.linearRampToValueAtTime(0, at + ROUTE_FADE);
    g.setValueAtTime(0, at + ROUTE_FADE + ROUTE_HOLD);
    g.linearRampToValueAtTime(1, at + ROUTE_FADE + ROUTE_HOLD + ROUTE_FADE);
    laterIn(ctx, ROUTE_FADE + ROUTE_HOLD / 2, fn);
  }

  function makeRoute(chans) {
    const merger = ctx.createChannelMerger(chans.length);
    chans.forEach((c, i) => splitter.connect(merger, c, i));
    // Always two channels: a mono input is put in the centre (left = right)
    // here, before any chain, so every pedal gets the stereo signal its
    // explicit two-channel delay lines are built for. Without a chain this
    // is what monitorGain ("speakers") did with the mono channel: the same
    // samples on both sides.
    const node = ctx.createGain();
    node.channelCount = 2;
    node.channelCountMode = "explicit";
    node.channelInterpretation = "speakers";
    merger.connect(node);
    node.connect(monitorGain);
    return { chans, merger, node, host: null };
  }

  function dropRoute(r) {
    for (const n of [r.merger, r.node]) {
      try { n.disconnect(); } catch { /* not connected */ }
    }
    r.host?.dispose();
    r.host = null;
  }

  const hostTiming = () => ({ songTime: ctx.currentTime, when: ctx.currentTime });

  // A route's chain: a spec for fx/chainHost.js, or null for none (the route
  // goes straight to monitorGain, as before chains existed).
  function setRouteSpec(r, spec) {
    if (spec && !r.host) {
      const host = createChainHost(ctx);
      host.apply(spec, hostTiming(), { immediate: true });
      host.output.connect(monitorGain);
      r.host = host;
      duckRoute(r, () => {
        if (r.host !== host) return;
        try { r.node.disconnect(monitorGain); } catch { /* not connected */ }
        r.node.connect(host.input);
      });
    } else if (spec && r.host) {
      r.host.apply(spec, hostTiming());
    } else if (!spec && r.host) {
      const host = r.host;
      r.host = null;
      duckRoute(r, () => {
        try { r.node.disconnect(host.input); } catch { /* not connected */ }
        if (!r.host) r.node.connect(monitorGain);
        host.dispose();
      });
    }
  }

  /**
   * What monitoring plays: a list of routes, each {key, channels, spec}
   * (an armed track's [c] or [a, b], and its chain spec or null), or null
   * for every channel of the device as it comes. A mono channel is heard in
   * the centre, a pair as left and right. A route kept with the same
   * channels keeps its nodes (and its chain's echoes); only what changed is
   * rebuilt.
   */
  function routeMonitor(list) {
    if (!list) {
      for (const r of routes.values()) dropRoute(r);
      routes.clear();
      if (!wholeDevice) inputNode.connect(monitorGain);
      wholeDevice = true;
      return;
    }
    if (wholeDevice) {
      try { inputNode.disconnect(monitorGain); } catch { /* not connected */ }
      wholeDevice = false;
    }
    const wanted = new Map();
    for (const item of list) {
      const chans = (item?.channels || []).filter((c) => c < deviceChannels).slice(0, 2);
      if (!chans.length || wanted.has(item.key)) continue;
      wanted.set(item.key, { chans, spec: item.spec || null });
    }
    for (const [key, r] of [...routes]) {
      const w = wanted.get(key);
      if (!w || w.chans.join(",") !== r.chans.join(",")) {
        dropRoute(r);
        routes.delete(key);
      }
    }
    for (const [key, w] of wanted) {
      let r = routes.get(key);
      if (!r) {
        r = makeRoute(w.chans);
        routes.set(key, r);
      }
      setRouteSpec(r, w.spec);
    }
  }
  routeMonitor(null);

  const recorders = new Set();

  /**
   * A recorder on some channels of this device: [c] records mono, [a, b] a
   * stereo pair. A channel the device does not have records silence.
   */
  function createRecorder(channels) {
    const chans = (channels?.length ? channels : [0]).slice(0, 2);
    const node = new AudioWorkletNode(ctx, "capture-recorder", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      channelCount: chans.length,
      channelCountMode: "explicit",
      channelInterpretation: "discrete",
      processorOptions: { channels: chans.length },
    });
    const merger = ctx.createChannelMerger(chans.length);
    chans.forEach((c, i) => {
      if (c < deviceChannels) splitter.connect(merger, c, i);
    });
    merger.connect(node);
    node.connect(sink);

    let onBatch = null;
    let waitStarted = null;
    let waitStopped = null;
    node.port.onmessage = (e) => {
      const m = e.data || {};
      if (m.type === "batch") onBatch?.(m);
      else if (m.type === "started") { waitStarted?.(m); waitStarted = null; }
      else if (m.type === "stopped") { waitStopped?.(m); waitStopped = null; }
    };
    const rec = {
      channels: chans.length,
      sourceChannels: chans,
      /** Start copying input; resolves with the context frame of frame 0. */
      start(batchCb) {
        onBatch = batchCb;
        return new Promise((resolve) => {
          waitStarted = resolve;
          node.port.postMessage({ type: "start" });
        });
      },
      /**
       * Stop; resolves once every batch has been delivered. A closed context
       * runs no worklet and so never answers: what it delivered before it
       * closed is all there is, and Stop must not wait for more. It used to
       * wait forever, leaving the take on "Saving" with its audio unsaved.
       */
      stop() {
        return new Promise((resolve) => {
          let timer = 0;
          const done = (m) => {
            clearTimeout(timer);
            waitStopped = null;
            onBatch = null;
            resolve(m);
          };
          if (ctx.state === "closed") { done({ type: "stopped", dropped: 0, contextClosed: true }); return; }
          waitStopped = done;
          node.port.postMessage({ type: "stop" });
          // A running worklet answers within one render quantum.
          timer = setTimeout(() => {
            console.warn("[capture] the recorder did not answer Stop; keeping what it delivered");
            done({ type: "stopped", dropped: 0, timedOut: true });
          }, STOP_ANSWER_MS);
        });
      },
      dispose() {
        recorders.delete(rec);
        try { node.port.postMessage({ type: "dispose" }); } catch (e) { console.warn(e); }
        for (const n of [merger, node]) {
          try { n.disconnect(); } catch { /* not connected */ }
        }
      },
    };
    recorders.add(rec);
    return rec;
  }

  const deviceIdUsed = settings.deviceId || deviceId || "";
  const estimate = {
    outputLatencyMs: Number.isFinite(ctx.outputLatency) ? ctx.outputLatency * 1000 : null,
    baseLatencyMs: Number.isFinite(ctx.baseLatency) ? ctx.baseLatency * 1000 : null,
    inputLatencyMs: Number.isFinite(settings.latency) ? settings.latency * 1000 : null,
  };
  const estimatedSec = estimateRoundTripSec({
    outputLatency: ctx.outputLatency, baseLatency: ctx.baseLatency, trackLatency: settings.latency,
  });

  const lostCbs = new Set();
  const onEnded = () => { for (const cb of lostCbs) cb(); };
  track.addEventListener("ended", onEnded);

  let closed = false;
  const capture = {
    ctx,
    engine,
    stream,
    track,
    inputNode,
    sampleRate: ctx.sampleRate,
    // Channels the default recorder takes (the latency test).
    channels: Math.min(2, deviceChannels),
    deviceChannels,
    // The device asked for; deviceId is the one that opened. They differ only
    // when the asked-for device was gone and the default opened instead.
    requestedDeviceId: deviceId || "",
    fellBack: !!fellBack,
    deviceId: deviceIdUsed,
    deviceLabel: track.label || "",
    estimate,
    estimatedSec,
    roundTripSeconds: estimatedSec,
    calibrated: false,
    calibration: null,
    createRecorder,

    /** Use a stored or fresh calibration for this device and rate. */
    applyCalibration(cal) {
      if (cal && Number.isFinite(cal.roundTripMs)) {
        capture.calibration = cal;
        capture.calibrated = true;
        capture.roundTripSeconds = cal.roundTripMs / 1000;
      } else {
        capture.calibration = null;
        capture.calibrated = false;
        capture.roundTripSeconds = estimatedSec;
      }
    },

    /**
     * Peak level of the latest block, 0..1 (for the meter): the loudest
     * channel. A downmix would halve a signal on one side only (a mixer
     * channel panned hard left), and a meter at half reads as "too quiet".
     */
    level() {
      return capture.channelLevel(Array.from({ length: deviceChannels }, (_, i) => i));
    },
    /** Peak of some channels of the latest block, 0..1 (an armed track's meter). */
    channelLevel(channels) {
      let peak = 0;
      for (const c of channels || [0]) {
        const m = chanMeter(c);
        if (!m) continue;
        m.a.getFloatTimeDomainData(m.buf);
        for (let i = 0; i < m.buf.length; i++) {
          const v = Math.abs(m.buf[i]);
          if (v > peak) peak = v;
        }
      }
      return peak;
    },
    /** Monitor these channel sets, no chain (null: the whole device). */
    setMonitorChannels(sets) {
      routeMonitor(sets ? sets.map((ch) => ({ key: (ch || []).join(","), channels: ch, spec: null })) : null);
    },
    /**
     * Monitor these routes: [{key, channels, spec}], each with its own chain
     * (capture/inputFx.js decides), or null for the whole device.
     */
    setMonitorRoutes(list) {
      routeMonitor(list);
    },
    setMonitor(on, gain = 1) {
      monitorOn = !!on;
      monitorGain.gain.setTargetAtTime(on ? Math.max(0, gain) : 0, ctx.currentTime, 0.01);
    },
    /** What monitoring sends to the output (a tap for a meter or a test). */
    monitorNode: monitorGain,
    /** Chain hosts in the monitor path now (0 with monitoring off or no chain). */
    chainHostCount: () => [...routes.values()].filter((r) => r.host).length,
    /** A route's chain latency in frames, as built (0 without a host). */
    routeLatencyFrames: (key) => routes.get(key)?.host?.latencyFrames() || 0,
    /** Chains alive in a route's host: the active one plus those ringing out. */
    routeChainCount: (key) => routes.get(key)?.host?.chainCount() || 0,
    setTrim(gain) {
      inputNode.gain.setTargetAtTime(Math.max(0, gain), ctx.currentTime, 0.01);
    },
    onDeviceLost(cb) {
      lostCbs.add(cb);
      return () => lostCbs.delete(cb);
    },
    get closed() { return closed; },
    close() {
      if (closed) return;
      closed = true;
      track.removeEventListener("ended", onEnded);
      lostCbs.clear();
      for (const r of [...recorders]) r.dispose();
      for (const r of routes.values()) dropRoute(r);
      routes.clear();
      for (const n of [source, inputNode, splitter, sink, monitorGain, ...[...chanMeters.values()].map((m) => m.a)]) {
        try { n.disconnect(); } catch { /* not connected */ }
      }
      stream.getTracks().forEach((tr) => tr.stop());
    },
  };
  // The default recorder: the first channel, or the first pair. The latency
  // test uses it.
  capture.recorder = createRecorder(capture.channels === 2 ? [0, 1] : [0]);
  capture.applyCalibration(loadCalibration(deviceIdUsed, ctx.sampleRate));
  return { capture, reason: null };
}
