// The effects chain in the live graph: one host per playback source of a
// lane that has a chain (Advanced mode only), between the source and the
// lane's gain, so the chain sits before the fader (report D1) and before the
// lane EQ (D2). Shared by audioEngine.js and chunkedAudioEngine.js through
// fxChain.createEngineFx.
//
//   source ─ input ─┬─ dry ──────────────────────────┬─ sum ─ [comp delay] ─ output ─ lane gain
//                   └─ trim ─┬─ feed ─ [pedals] ─ out ┤
//                            └─ feed ─ [pedals] ─ out ┘   (an older order, ringing out)
//
// A lane with no chain gets no host at all: the source connects to the lane
// gain exactly as before the chain existed. Practice, Karaoke and Performance
// never get one (fxEngine.js decides), so their graph is the graph of 0.21.0.
//
// Click-free changes (report 3.7):
// - A knob turn ramps the AudioParam over 20 ms (ramp.js).
// - A change of structure (order, a pedal added or removed, an instrument, a
//   digital delay's time) builds the new chain beside the old one and
//   crossfades the INPUT over 20 ms: the old chain's echoes keep ringing
//   through their own pedals, as pedals left on during a change would, and it
//   is let go once its longest tail has passed (capped at 4 s). connect() and
//   disconnect() take no time argument, so a live rewire would land on an
//   unknown render quantum with the signal mid-flight: a click.
// - At most two older chains ring at once; a third change fades the oldest
//   out over 20 ms.
// - Whole chain off: the dry path crossfades in and the trim to 0, so the
//   tails ring out; once they have, the output is the input bit for bit.
// - A seek flushes: every chain fades out over 20 ms and a fresh one takes
//   over, so an echo of the old place never plays over the new one (D7).
// - A play start rebuilds a chain that has an LFO, so its phase follows song
//   time again after a pause, as the export print's does.
// - Inserting or removing a host while the song plays ducks the lane over
//   32 ms, the way a key change does (audioEngine.js routeTrack).

import { rampTo, XFADE } from "./ramp.js";
import { createSeries } from "./pedals.js";
import { chainStructureKey, chainTailSec, chainLatencyFrames } from "./chainModel.js";

const DROP_MARGIN = 0.05;
const MAX_FADING = 2;
const ROUTE_FADE = 0.006;
const ROUTE_HOLD = 0.02;

const isOffline = (ctx) => typeof OfflineAudioContext !== "undefined" && ctx instanceof OfflineAudioContext;

// Offline: the render pauses at the quantum boundary at or after `t` and the
// callbacks due then run, so a scheduled drop happens at a render time and
// not at whatever wall-clock moment a timer fires (tests render offline).
const offlineDue = new WeakMap();
/**
 * Run `fn` `sec` seconds from now on this context's clock. Returns a cancel
 * function. A live context uses a timer; an offline one suspends its render.
 */
export function laterIn(ctx, sec, fn) {
  if (!isOffline(ctx)) {
    const id = setTimeout(fn, Math.max(0, sec) * 1000);
    return () => clearTimeout(id);
  }
  const q = 128 / ctx.sampleRate;
  const t = Math.ceil((ctx.currentTime + Math.max(sec, q)) / q - 1e-9) * q;
  let due = offlineDue.get(ctx);
  if (!due) { due = new Map(); offlineDue.set(ctx, due); }
  const key = t.toFixed(9);
  let list = due.get(key);
  const entry = { fn, live: true };
  if (!list) {
    list = [];
    due.set(key, list);
    ctx.suspend(t).then(() => {
      due.delete(key);
      for (const e of list) if (e.live) { try { e.fn(); } catch (err) { console.warn("[fx] a scheduled change failed:", err); } }
      return ctx.resume();
    }).catch(() => { due.delete(key); /* past the end of the render */ });
  }
  list.push(entry);
  return () => { entry.live = false; };
}

const dbToGain = (db) => (db === 0 ? 1 : 10 ** (db / 20));

/**
 * One host. `spec`: {desc (normalised chain), inst, bpm, loopExtra,
 * lowLatency} or null for a host that only carries latency compensation.
 * `lowLatency` is set only by a live input's chain (capture/inputFx.js).
 * Also the live input's monitor chain (captureGraph.js), unchanged.
 */
export function createChainHost(ctx) {
  const mk = (v) => { const n = ctx.createGain(); n.gain.value = v; return n; };
  const input = mk(1);
  const dry = mk(1);
  const trim = mk(0);
  const sum = mk(1);
  const output = mk(1);
  input.connect(dry);
  dry.connect(sum);
  input.connect(trim);
  sum.connect(output);
  let comp = null;
  let compFrames = 0;
  let active = null;
  const fading = [];
  let spec = null;
  let disposed = false;

  function build(sp, { songTime, when }, feedNow) {
    const env = { inst: sp.inst, bpm: sp.bpm, loopExtra: sp.loopExtra, lowLatency: !!sp.lowLatency, songTime, when };
    const series = createSeries(ctx, sp.desc, env);
    const feed = mk(feedNow);
    const out = mk(1);
    trim.connect(feed);
    feed.connect(series.input);
    series.output.connect(out);
    out.connect(sum);
    return {
      key: chainStructureKey(sp.desc, sp.inst, sp.bpm, { lowLatency: !!sp.lowLatency }),
      series,
      feed,
      out,
      tail: chainTailSec(sp.desc, sp.bpm),
      usesSongTime: [...series.pedals.values()].some((n) => n.usesSongTime),
      cancel: null,
    };
  }

  function drop(inst) {
    inst.cancel?.();
    inst.cancel = null;
    const i = fading.indexOf(inst);
    if (i >= 0) fading.splice(i, 1);
    try { trim.disconnect(inst.feed); } catch { /* gone */ }
    inst.series.dispose();
    for (const n of [inst.feed, inst.out]) { try { n.disconnect(); } catch { /* gone */ } }
  }

  // The input fades out; the tail rings until it has passed, then it goes.
  function retire(inst, immediate) {
    if (immediate) { drop(inst); return; }
    rampTo(inst.feed.gain, 0, ctx, false, XFADE);
    fading.push(inst);
    while (fading.length > MAX_FADING) fadeOut(fading[0], false);
    inst.cancel = laterIn(ctx, inst.tail + XFADE + DROP_MARGIN, () => drop(inst));
  }

  // The output fades out over 20 ms, then it goes (a flush, or one too many).
  function fadeOut(inst, immediate) {
    const i = fading.indexOf(inst);
    if (i >= 0) fading.splice(i, 1);
    inst.cancel?.();
    if (immediate) { drop(inst); return; }
    rampTo(inst.out.gain, 0, ctx, false, XFADE);
    inst.cancel = laterIn(ctx, XFADE + DROP_MARGIN, () => drop(inst));
  }

  function setLevels(immediate) {
    const on = !!spec && spec.desc.on !== false;
    rampTo(dry.gain, on ? 0 : 1, ctx, immediate, XFADE);
    rampTo(trim.gain, on ? dbToGain(spec.desc.trim) : 0, ctx, immediate, XFADE);
  }

  return {
    input,
    output,
    /** What the chain meter reads: the signal after the trim. */
    meterNode: trim,
    /**
     * Apply a chain. `timing`: {songTime, when} for the LFO phase of a chain
     * built now. `immediate`: the host is not sounding yet, so nothing fades.
     */
    apply(sp, timing, { immediate = false, rebuild = false } = {}) {
      if (disposed) return;
      spec = sp;
      setLevels(immediate);
      if (!sp) {
        if (active) retire(active, immediate);
        active = null;
        return;
      }
      const key = chainStructureKey(sp.desc, sp.inst, sp.bpm, { lowLatency: !!sp.lowLatency });
      if (active && active.key === key && !rebuild) {
        active.series.update(sp.desc, { inst: sp.inst, bpm: sp.bpm, loopExtra: sp.loopExtra, lowLatency: !!sp.lowLatency }, immediate);
        active.tail = chainTailSec(sp.desc, sp.bpm);
        return;
      }
      const next = build(sp, timing, immediate ? 1 : 0);
      if (!immediate) rampTo(next.feed.gain, 1, ctx, false, XFADE);
      if (active) retire(active, immediate);
      active = next;
    },
    /** A play start: rebuild a chain whose LFO follows song time. */
    resync(timing) {
      if (!spec || !active?.usesSongTime) return;
      this.apply(spec, timing, { rebuild: true });
    },
    /** A seek: every chain out over 20 ms, a fresh one in at once. */
    flush(timing) {
      if (disposed || !spec) return;
      for (const inst of [...fading]) fadeOut(inst, false);
      if (active) fadeOut(active, false);
      active = build(spec, timing, 1);
    },
    /** The latency compensation in front of the output, in frames. */
    setCompensation(frames) {
      const n = Math.max(0, Math.round(frames));
      if (n === compFrames) return;
      compFrames = n;
      if (n > 0 && !comp) {
        comp = ctx.createDelay(1);
        // Explicit, as every delay line of the pedals (delayPedal.js says why).
        comp.channelCount = 2;
        comp.channelCountMode = "explicit";
        try { sum.disconnect(output); } catch { /* gone */ }
        sum.connect(comp);
        comp.connect(output);
      } else if (n === 0 && comp) {
        try { sum.disconnect(comp); } catch { /* gone */ }
        try { comp.disconnect(); } catch { /* gone */ }
        comp = null;
        sum.connect(output);
      }
      if (comp) comp.delayTime.value = n / ctx.sampleRate;
    },
    compensationFrames: () => compFrames,
    /** The chain's own latency in frames (what compensation lines up). */
    latencyFrames: () => (spec && spec.desc.on !== false ? chainLatencyFrames(spec.desc, spec.loopExtra, { lowLatency: !!spec.lowLatency }) : 0),
    /** Chains alive now: the active one plus those ringing out. */
    chainCount: () => (active ? 1 : 0) + fading.length,
    hasSpec: () => !!spec,
    dispose() {
      disposed = true;
      for (const inst of [...fading]) drop(inst);
      if (active) drop(active);
      active = null;
      for (const n of [input, dry, trim, sum, output, comp]) { if (n) { try { n.disconnect(); } catch { /* gone */ } } }
    },
  };
}

/**
 * Every host of one engine. `members()` lists the engine's playback sources
 * (each with .gain, .level, .controlName), `membersOf(name)` those of one
 * lane, `liveNodes(member)` the source nodes playing into it now.
 */
export function createChainHosts(ctx, { members, membersOf, liveNodes, onLatencyChange = null }) {
  const lanes = new Map();
  let anchor = { offset: 0, when: 0, playing: false };
  let compFrames = 0;
  // Called when a lane's hosts change, so a meter tap can re-attach.
  let onHostsChanged = null;

  const songTimeAt = (t) => (anchor.playing ? anchor.offset + (t - anchor.when) : anchor.offset);
  const timing = () => ({ songTime: songTimeAt(ctx.currentTime), when: ctx.currentTime });

  // A short dip on the lane gain with the rewire at its bottom.
  function duck(member, fn, immediate) {
    if (immediate || !anchor.playing) { fn(); return; }
    const g = member.gain.gain;
    const at = ctx.currentTime;
    g.cancelScheduledValues(at);
    g.setValueAtTime(g.value, at);
    g.linearRampToValueAtTime(0, at + ROUTE_FADE);
    g.setValueAtTime(0, at + ROUTE_FADE + ROUTE_HOLD);
    g.linearRampToValueAtTime(member.level ?? 1, at + ROUTE_FADE + ROUTE_HOLD + ROUTE_FADE);
    laterIn(ctx, ROUTE_FADE + ROUTE_HOLD / 2, fn);
  }

  function rewire(member, from, to) {
    for (const n of liveNodes(member)) {
      try { n.disconnect(from); } catch { /* not connected there */ }
      try { n.connect(to); } catch (err) { console.warn("[fx] reconnecting a source failed:", err); }
    }
  }

  function addHost(member, immediate) {
    const host = createChainHost(ctx);
    host.output.connect(member.gain);
    member.chainHost = host;
    duck(member, () => rewire(member, member.gain, host.input), immediate);
    return host;
  }

  function removeHost(member, immediate) {
    const host = member.chainHost;
    member.chainHost = null;
    duck(member, () => {
      rewire(member, host.input, member.gain);
      host.dispose();
    }, immediate);
  }

  function syncMember(member, immediate) {
    const sp = lanes.get(member.controlName) || null;
    if (!sp && compFrames === 0) {
      if (member.chainHost) removeHost(member, immediate);
      return;
    }
    let host = member.chainHost;
    const fresh = !host;
    if (fresh) host = addHost(member, immediate);
    host.apply(sp, timing(), { immediate: immediate || fresh });
    // A fresh host is not in the signal path until the duck's bottom, so its
    // delay can be set now; an existing one changes inside a duck
    // (syncCompensation).
    if (fresh) host.setCompensation(compFrames - host.latencyFrames());
  }

  // D8: every lane is delayed to the slowest chain, so a pedal with latency
  // does not put its lane late against the others.
  function syncCompensation(immediate) {
    let max = 0;
    for (const m of members()) max = Math.max(max, m.chainHost?.latencyFrames() || 0);
    const changed = max !== compFrames;
    compFrames = max;
    // The click is scheduled against the lanes (the engine adds this latency
    // to its clock mapping), so clicks already queued are now off by the old
    // amount: the engine starts a new scheduling epoch.
    if (changed) onLatencyChange?.();
    for (const m of members()) {
      if (!m.chainHost) {
        if (compFrames > 0) syncMember(m, immediate);
        continue;
      }
      if (!m.chainHost.hasSpec() && compFrames === 0) { removeHost(m, immediate); continue; }
      const target = compFrames - m.chainHost.latencyFrames();
      if (target === m.chainHost.compensationFrames()) continue;
      duck(m, () => m.chainHost?.setCompensation(target), immediate);
    }
  }

  return {
    /** A new playback source of the engine: give it its lane's chain. */
    attach(member) {
      if (lanes.has(member.controlName) || compFrames > 0) syncMember(member, true);
    },
    /** What a source connects to: its host's input, or the lane gain. */
    inputOf(member) {
      return member.chainHost ? member.chainHost.input : member.gain;
    },
    /** A lane's chain spec, or null to take the lane's chain out of the graph. */
    setLane(name, sp, { immediate = false } = {}) {
      if (sp) lanes.set(name, sp);
      else lanes.delete(name);
      for (const m of membersOf(name)) syncMember(m, immediate);
      syncCompensation(immediate);
      onHostsChanged?.(name);
      return membersOf(name).length > 0;
    },
    start(offset, when) {
      anchor = { offset, when, playing: true };
      for (const m of members()) m.chainHost?.resync(timing());
    },
    stop(songTime) {
      anchor = { offset: songTime, when: ctx.currentTime, playing: false };
    },
    seek(songTime) {
      anchor = { offset: songTime, when: ctx.currentTime, playing: false };
      for (const m of members()) m.chainHost?.flush(timing());
    },
    latencySec: () => compFrames / ctx.sampleRate,
    /** How many sources have a host now (0 outside Advanced mode). */
    hostCount: () => members().filter((m) => m.chainHost).length,
    meterNodes: (name) => membersOf(name).filter((m) => m.chainHost).map((m) => m.chainHost.meterNode),
    hostOf: (member) => member.chainHost || null,
    onHostsChanged(cb) { onHostsChanged = cb; },
    dispose() {
      for (const m of members()) {
        if (!m.chainHost) continue;
        m.chainHost.dispose();
        m.chainHost = null;
      }
      lanes.clear();
    },
  };
}
