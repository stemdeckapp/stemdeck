// One effects slot in the Web Audio graph: a lane's EQ between its gain and
// its analyser, or the master slot between the master gain and the
// destination (03-eq.md section 5.1). Shared by audioEngine.js and
// chunkedAudioEngine.js so the two engines cannot drift apart.
//
//   input ─┬─ dry ─────────────────────────────┬─ out (output gain) = output
//          └─ biquad ─ biquad ─ ... ─ wet ──────┘
//
// Native BiquadFilterNodes only: no AudioWorklet, so the EQ works over plain
// HTTP on a LAN and on phones, where AudioWorklet does not exist.
//
// - Parameter changes ramp linearly over PARAM_RAMP, so a dragged band never
//   steps (no zipper, no click), and the ramp ends on the exact target.
// - A change of structure (a band added, removed, switched type or slope, a
//   band leaving or reaching 0 dB) builds a new filter branch and crossfades
//   to it over XFADE; the old branch is disconnected once it is silent.
// - Bypass crossfades to the dry path and holds the output gain at exactly 1,
//   so once the fade has run the slot's output is bit-identical to its input.
//   The filter branch stays connected and keeps running while bypassed, so
//   switching back is a crossfade between two settled signals.
// - Flat (no band changes anything, output gain 0 dB): no filter at all, the
//   dry path at gain 1, also bit-identical to the input.

import { eqSections, nodeQ } from "./eqMath.js";
import { createMasterEffect } from "./masterFx.js";

export const PARAM_RAMP = 0.02; // seconds
export const XFADE = 0.02; // seconds
// The old branch is let go a little after its fade has reached 0.
const RELEASE_MS = (XFADE + 0.05) * 1000;

function rampTo(param, value, ctx, immediate, time = PARAM_RAMP) {
  const now = ctx.currentTime;
  if (immediate) {
    param.cancelScheduledValues?.(0);
    param.value = value;
    return;
  }
  // Not cancelAndHoldAtTime: measured in Chromium, holding a ramp that is
  // still running and starting the next from it clicked on every knob step
  // (a 0.22 jump against 0.001 for the same sweep this way). The value the
  // param has now is where the next ramp starts.
  const current = param.value;
  param.cancelScheduledValues?.(now);
  param.setValueAtTime?.(current, now);
  if (typeof param.linearRampToValueAtTime === "function") {
    param.linearRampToValueAtTime(value, now + time);
  } else {
    param.value = value;
  }
}

const signature = (sections) => sections.map((s) => s.type).join(",");

/**
 * @param {BaseAudioContext} ctx
 * @returns {{input: GainNode, output: GainNode, setEq: Function,
 *   getNodeResponseDb: Function, sectionCount: Function, dispose: Function}}
 */
export function createFxSlot(ctx) {
  const input = ctx.createGain();
  const dry = ctx.createGain();
  const out = ctx.createGain();
  input.connect(dry);
  dry.connect(out);
  let branch = null; // { sig, nodes: BiquadFilterNode[], wet: GainNode }
  let disposed = false;
  // Old branches still fading out: timer id -> the function that drops them.
  const pending = new Map();

  function buildBranch(sections) {
    const nodes = sections.map((s) => {
      const node = ctx.createBiquadFilter();
      node.type = s.type;
      node.frequency.value = s.f;
      node.Q.value = nodeQ(s);
      node.gain.value = s.g;
      return node;
    });
    const wet = ctx.createGain();
    wet.gain.value = 0;
    let prev = input;
    for (const node of nodes) { prev.connect(node); prev = node; }
    prev.connect(wet);
    wet.connect(out);
    return { sig: signature(sections), nodes, wet };
  }

  function releaseBranch(old, immediate) {
    let timer = 0;
    const drop = () => {
      pending.delete(timer);
      try { input.disconnect(old.nodes[0] || old.wet); } catch { /* already gone */ }
      for (const node of old.nodes) { try { node.disconnect(); } catch { /* already gone */ } }
      try { old.wet.disconnect(); } catch { /* already gone */ }
    };
    if (immediate) { drop(); return; }
    timer = setTimeout(drop, RELEASE_MS);
    pending.set(timer, drop);
  }

  // Drop every branch that is still fading out, now.
  function flushPending() {
    for (const [t, drop] of [...pending]) { clearTimeout(t); drop(); }
  }

  /**
   * Apply a normalised EQ descriptor (eqMath.normalizeEq). `immediate` sets
   * every value at once, for a slot that is not sounding yet.
   */
  function setEq(eq, { immediate = false } = {}) {
    if (disposed) return;
    if (immediate) flushPending();
    // Sections ignoring bypass: a bypassed EQ keeps its filters warm.
    const sections = eq ? eqSections({ ...eq, bypass: false }) : [];
    const sig = signature(sections);
    if (branch && branch.sig === sig && sections.length) {
      sections.forEach((s, i) => {
        const node = branch.nodes[i];
        rampTo(node.frequency, s.f, ctx, immediate);
        rampTo(node.Q, nodeQ(s), ctx, immediate);
        rampTo(node.gain, s.g, ctx, immediate);
      });
    } else {
      const old = branch;
      branch = sections.length ? buildBranch(sections) : null;
      if (old) {
        rampTo(old.wet.gain, 0, ctx, immediate, XFADE);
        releaseBranch(old, immediate);
      }
    }
    const bypass = !eq || eq.bypass === true;
    const wetOn = !bypass && !!branch;
    rampTo(dry.gain, wetOn ? 0 : 1, ctx, immediate, XFADE);
    if (branch) rampTo(branch.wet.gain, wetOn ? 1 : 0, ctx, immediate, XFADE);
    const outDb = bypass ? 0 : (eq.params?.out || 0);
    rampTo(out.gain, outDb === 0 ? 1 : 10 ** (outDb / 20), ctx, immediate);
  }

  /**
   * The response of the filters that are actually in the graph now, read
   * from the nodes themselves (BiquadFilterNode.getFrequencyResponse), in dB,
   * output gain included. Used to check the maths against the browser.
   */
  function getNodeResponseDb(freqs) {
    const f32 = Float32Array.from(freqs);
    const db = new Float64Array(f32.length);
    const outGain = out.gain.value;
    for (let i = 0; i < db.length; i++) db[i] = 20 * Math.log10(outGain || 1e-12);
    if (!branch || dry.gain.value === 1) return db;
    const mag = new Float32Array(f32.length);
    const phase = new Float32Array(f32.length);
    for (const node of branch.nodes) {
      node.getFrequencyResponse(f32, mag, phase);
      for (let i = 0; i < db.length; i++) db[i] += 20 * Math.log10(mag[i]);
    }
    return db;
  }

  function dispose() {
    disposed = true;
    flushPending();
    if (branch) releaseBranch(branch, true);
    branch = null;
    for (const node of [input, dry, out]) { try { node.disconnect(); } catch { /* noop */ } }
  }

  return {
    input,
    output: out,
    setEq,
    getNodeResponseDb,
    sectionCount: () => branch?.nodes.length || 0,
    dispose,
  };
}

/**
 * Everything an engine needs for effects, in one place so both engines carry
 * the same code: a slot per playback source (inserted between its gain and
 * its VU analyser), the master slot (between the master gain and the
 * destination), the remembered EQ per lane (a source decoded after the EQ
 * was set still gets it), and the spectrum taps.
 *
 * `membersOf(name)` returns the engine's playback sources whose controlName is
 * `name`; each carries `.fx` (its slot) once attachSource() has run.
 */
export function createEngineFx(ctx, membersOf) {
  const laneEq = new Map();
  const taps = createTapRegistry(ctx);
  const master = createFxSlot(ctx);
  let masterEq = null;
  // After the master EQ: the master effect (masterFx.js), created the first
  // time mastering is on and dropped once it is off again, crossfaded with a
  // straight path around it. Off, that path is at exactly 1 and nothing else
  // is connected, so the output is bit-identical to the master EQ's. `tail`
  // is the end of the chain, the stable tap for recorders and streams.
  const tail = ctx.createGain();
  const around = ctx.createGain();
  master.output.connect(around);
  around.connect(tail);
  tail.connect(ctx.destination);
  let mfx = null; // { effect, wet, active }
  let mfxDrop = 0;
  // A master effect made the moment it is switched on starts out compressing
  // hard: measured in Chromium, the level dipped 4 dB and took about 200 ms
  // to come back, and the compressors reported up to -22 dB of reduction
  // that was not there. So while the Master drawer is open the effect is kept
  // running, silent (wet at 0), and switching it on only crossfades to a
  // signal that has settled. The straight path stays at exactly 1 meanwhile,
  // so the output is still bit-identical to the master EQ's.
  let warmWanted = false;

  function makeMfx(params) {
    const effect = createMasterEffect(ctx);
    const wet = ctx.createGain();
    wet.gain.value = 0;
    master.output.connect(effect.input);
    effect.output.connect(wet);
    wet.connect(tail);
    effect.set(params, { immediate: true });
    return { effect, wet, active: false };
  }

  function dropMfx(old) {
    if (mfx !== old) return;
    try { master.output.disconnect(old.effect.input); } catch { /* gone */ }
    old.effect.dispose();
    try { old.wet.disconnect(); } catch { /* gone */ }
    mfx = null;
  }

  function scheduleDrop(immediate) {
    if (!mfx || mfx.active || warmWanted) return;
    const old = mfx;
    if (mfxDrop) clearTimeout(mfxDrop);
    const drop = () => { mfxDrop = 0; if (!old.active && !warmWanted) dropMfx(old); };
    if (immediate) drop();
    else mfxDrop = setTimeout(drop, RELEASE_MS);
  }

  return {
    /** Wire gain -> slot -> analyser for one source and give it its EQ. */
    attachSource(source, gain, analyser) {
      const slot = createFxSlot(ctx);
      gain.connect(slot.input);
      slot.output.connect(analyser);
      source.fx = slot;
      const eq = laneEq.get(source.controlName);
      if (eq) slot.setEq(eq, { immediate: true });
      return slot;
    },
    /** The node the engine's master gain connects to. */
    masterInput: master.input,
    /** The end of the master chain: what reaches the speakers. */
    masterOutput: tail,
    setLaneEq(name, eq, opts = {}) {
      laneEq.set(name, eq);
      let found = false;
      for (const s of membersOf(name)) {
        if (!s.fx) continue;
        s.fx.setEq(eq, opts);
        found = true;
      }
      return found;
    },
    setMasterEq(eq, opts = {}) {
      masterEq = eq;
      master.setEq(eq, opts);
      return true;
    },
    getMasterEq: () => masterEq,
    /**
     * The live master: `params` for masterFx.set(), or null to take it out
     * of the graph (20 ms crossfade both ways).
     */
    setMasterFx(params, { immediate = false } = {}) {
      if (params) {
        if (mfxDrop) { clearTimeout(mfxDrop); mfxDrop = 0; }
        if (!mfx) mfx = makeMfx(params);
        else mfx.effect.set(params, { immediate });
        mfx.active = true;
        rampTo(around.gain, 0, ctx, immediate, XFADE);
        rampTo(mfx.wet.gain, 1, ctx, immediate, XFADE);
        return true;
      }
      if (!mfx) return false;
      mfx.active = false;
      rampTo(around.gain, 1, ctx, immediate, XFADE);
      rampTo(mfx.wet.gain, 0, ctx, immediate, XFADE);
      scheduleDrop(immediate);
      return true;
    },
    /**
     * Keep the master effect running, silent, ready to be switched on (true),
     * or let it go once it is off (false). `params` are what it should
     * settle on: the master the drawer would switch on.
     */
    warmMasterFx(on, params = null) {
      warmWanted = !!on;
      if (warmWanted) {
        if (mfxDrop) { clearTimeout(mfxDrop); mfxDrop = 0; }
        if (!mfx) mfx = makeMfx(params || {});
        else if (params && !mfx.active) mfx.effect.set(params);
        return true;
      }
      scheduleDrop(false);
      return true;
    },
    /** The live master's In and Out nodes (export level), or null when off. */
    getMasterFxNodes() {
      return mfx?.active ? { input: mfx.effect.inTap, output: mfx.effect.outTap } : null;
    },
    masterFxReduction: () => (mfx?.active ? mfx.effect.reduction() : null),
    masterFxLatency: () => (mfx?.active ? mfx.effect.latencySec : 0),
    getLaneTap(name, which, opts) {
      const nodes = membersOf(name)
        .filter((s) => s.fx)
        .map((s) => (which === "pre" ? s.fx.input : s.fx.output));
      if (!nodes.length) return null;
      return taps.get(`${name}|${which}`, nodes, opts);
    },
    releaseLaneTap(name, which) { taps.release(`${name}|${which}`); },
    getMasterTap(which, opts) {
      return taps.get(`\u0000master|${which}`, [which === "pre" ? master.input : master.output], opts);
    },
    releaseMasterTap(which) { taps.release(`\u0000master|${which}`); },
    getLaneNodeResponseDb(name, freqs) {
      const s = membersOf(name).find((m) => m.fx);
      return s ? s.fx.getNodeResponseDb(freqs) : null;
    },
    getMasterNodeResponseDb: (freqs) => master.getNodeResponseDb(freqs),
    laneSectionCount(name) {
      return membersOf(name).reduce((n, s) => n + (s.fx?.sectionCount() || 0), 0);
    },
    dispose() {
      taps.releaseAll();
      if (mfxDrop) clearTimeout(mfxDrop);
      mfx?.effect.dispose();
      master.dispose();
      for (const n of [around, tail]) { try { n.disconnect(); } catch { /* noop */ } }
    },
  };
}

/**
 * Lane analyser taps for the EQ panel's spectrum. One AnalyserNode per lane
 * and position, created on first request and fed by every source of the lane
 * (the "original" lane can be several stems), disconnected on release, so a
 * closed panel costs nothing.
 */
export function createTapRegistry(ctx) {
  const taps = new Map(); // key -> { analyser, sources: AudioNode[] }
  return {
    get(key, sourceNodes, { fftSize = 8192, smoothing = 0.8 } = {}) {
      let tap = taps.get(key);
      if (!tap) {
        const analyser = ctx.createAnalyser();
        analyser.fftSize = fftSize;
        analyser.smoothingTimeConstant = smoothing;
        tap = { analyser, sources: [] };
        taps.set(key, tap);
      }
      for (const node of sourceNodes) {
        if (tap.sources.includes(node)) continue;
        node.connect(tap.analyser);
        tap.sources.push(node);
      }
      return tap.analyser;
    },
    release(key) {
      const tap = taps.get(key);
      if (!tap) return;
      for (const node of tap.sources) {
        try { node.disconnect(tap.analyser); } catch { /* already gone */ }
      }
      taps.delete(key);
    },
    releaseAll() {
      for (const key of [...taps.keys()]) this.release(key);
    },
  };
}

/**
 * The effects methods both engines expose (spread into the engine object), so
 * eqEngine.js talks to either engine through one surface.
 */
export function engineFxApi(fx) {
  return {
    supportsEq: true,
    setLaneEq: (name, eq, opts) => fx.setLaneEq(name, eq, opts),
    setMasterEq: (eq, opts) => fx.setMasterEq(eq, opts),
    getLaneTap: (name, which, opts) => fx.getLaneTap(name, which, opts),
    releaseLaneTap: (name, which) => fx.releaseLaneTap(name, which),
    getMasterTap: (which, opts) => fx.getMasterTap(which, opts),
    releaseMasterTap: (which) => fx.releaseMasterTap(which),
    getLaneNodeResponseDb: (name, freqs) => fx.getLaneNodeResponseDb(name, freqs),
    getMasterNodeResponseDb: (freqs) => fx.getMasterNodeResponseDb(freqs),
    laneSectionCount: (name) => fx.laneSectionCount(name),
    // After the master chain, before the speakers: the tap point for anything
    // that records or streams the mix (03-eq.md section 14).
    getMasterOutputNode: () => fx.masterOutput,
    supportsMaster: true,
    setMasterFx: (params, opts) => fx.setMasterFx(params, opts),
    warmMasterFx: (on, params) => fx.warmMasterFx(on, params),
    getMasterFxNodes: () => fx.getMasterFxNodes(),
    masterFxReduction: () => fx.masterFxReduction(),
    masterFxLatency: () => fx.masterFxLatency(),
  };
}
