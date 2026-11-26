// The 32 ms dip around a lane's rewire while it plays (static/js/laneDip.js):
// a key change (audioEngine.routeTrack, chunkedAudioEngine.routeStem) and an
// effects chain put in or taken out (fx/chainHost.js duck). A volume, mute
// or Sound change made inside the dip must survive it. It did not while the
// dip was scheduled on the lane gain itself: the ramp back to the level of
// the dip's start, already scheduled, came after the change and undid it.
//
// AudioParam automation is evaluated here on a fake clock, the way the Web
// Audio spec computes it, so no browser is needed.
//
// Run:  node tests/js/lane-dip.test.mjs

import { readFileSync } from "node:fs";
import { DIP_FADE, DIP_HOLD, createDip, scheduleDip } from "../../static/js/laneDip.js";
import { createChainHosts } from "../../static/js/fx/chainHost.js";
import { emptyChain } from "../../static/js/fx/chainModel.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const near = (a, b, tol = 1e-3) => Math.abs(a - b) <= tol;

/**
 * An AudioParam on a fake clock: setValueAtTime, linearRampToValueAtTime,
 * setTargetAtTime and cancelScheduledValues, evaluated by at(t) as the spec
 * does (a ramp runs from the previous event's time and value; a target
 * approaches its value from the value at its start). `.value` is the value
 * at the clock's now, as a browser reports it.
 */
class Param {
  constructor(clock, v) {
    this.clock = clock;
    this.base = v;
    this.events = [];
  }
  get value() { return this.at(this.clock.now); }
  set value(v) { this.base = v; this.events = []; }
  add(e) {
    this.events.push(e);
    this.events.sort((a, b) => a.t - b.t);
    return this;
  }
  setValueAtTime(v, t) { return this.add({ kind: "set", v, t }); }
  linearRampToValueAtTime(v, t) { return this.add({ kind: "linear", v, t }); }
  setTargetAtTime(v, t, tau) { return this.add({ kind: "target", v, t, tau }); }
  cancelScheduledValues(t) { this.events = this.events.filter((e) => e.t < t); return this; }
  at(T) {
    let v = this.base;
    let t0 = 0;
    let target = null;
    for (const e of this.events) {
      // What the param is just before this event.
      const before = target ? target.v + (v - target.v) * Math.exp(-(Math.min(T, e.t) - t0) / target.tau) : v;
      if (e.kind === "linear") {
        if (T < e.t) return before + (e.v - before) * ((T - t0) / (e.t - t0));
        v = e.v;
      } else {
        if (T < e.t) return before;
        v = e.kind === "set" ? e.v : before;
      }
      t0 = e.t;
      target = e.kind === "target" ? e : null;
    }
    return target ? target.v + (v - target.v) * Math.exp(-(T - t0) / target.tau) : v;
  }
}

function fakeCtx() {
  const clock = { now: 0 };
  const node = () => {
    const n = { gain: new Param(clock, 1), out: [] };
    n.connect = (to) => { n.out.push(to); return to; };
    n.disconnect = (to) => { n.out = to === undefined ? [] : n.out.filter((x) => x !== to); };
    return n;
  };
  return {
    clock,
    sampleRate: 48000,
    get currentTime() { return clock.now; },
    createGain: node,
    createDelay: () => ({ ...node(), delayTime: new Param(clock, 0) }),
  };
}

// What a lane plays at time t: its gain times its dip (lane gain -> dip ->
// EQ slot, fxChain.attachSource).
const heard = (lane, t) => lane.gain.gain.at(t) * (lane.dip ? lane.dip.gain.at(t) : 1);
// A user's change, as setGain, mute and setLaneSource make it.
const userSets = (lane, level, t) => lane.gain.gain.setTargetAtTime(level, t, 0.01);
const END = DIP_FADE + DIP_HOLD + DIP_FADE;

// ─── The shape of the dip ───

{
  const ctx = fakeCtx();
  const dip = createDip(ctx);
  check("a dip node rests at 1", dip.gain.value === 1);
  const mid = scheduleDip(dip.gain, 1);
  check("the rewire runs in the middle of the silence", near(mid, DIP_FADE + DIP_HOLD / 2, 1e-12));
  check("silent at the rewire", dip.gain.at(1 + mid) === 0);
  check("down within DIP_FADE", dip.gain.at(1 + DIP_FADE) === 0 && dip.gain.at(1 + DIP_FADE / 2) > 0);
  check("back to 1 after 32 ms", near(dip.gain.at(1 + END), 1, 1e-12) && near(END, 0.032, 1e-12));
  // A second dip inside the first starts from where the first is.
  ctx.clock.now = 1 + DIP_FADE / 2;
  scheduleDip(dip.gain, ctx.clock.now);
  check("a dip inside a dip: no jump up", near(dip.gain.at(ctx.clock.now), 0.5, 1e-9));
  check("and it still ends at 1", near(dip.gain.at(ctx.clock.now + END), 1, 1e-12));
}

// ─── The bug, on one AudioParam (how the dip used to be scheduled) ───

{
  const ctx = fakeCtx();
  const lane = { gain: ctx.createGain() };
  lane.gain.gain.value = 0.8;
  // The old dip: on the lane gain, back to the level of its start.
  const g = lane.gain.gain;
  g.cancelScheduledValues(0);
  g.setValueAtTime(g.value, 0);
  g.linearRampToValueAtTime(0, DIP_FADE);
  g.setValueAtTime(0, DIP_FADE + DIP_HOLD);
  g.linearRampToValueAtTime(0.8, END);
  userSets(lane, 0.2, 0.01);
  check("on one param the dip's ramp undoes a change made inside it (the old bug)", near(heard(lane, 0.2), 0.8));
}

// ─── The fix: the dip on its own node ───

for (const [what, level, t] of [
  ["a volume change", 0.2, 0.01],
  ["a mute", 0, 0.004],
  ["a Sound switched off (srcOn 0)", 0, 0.02],
  ["a volume change in the dip's last ramp", 1.5, 0.03],
]) {
  const ctx = fakeCtx();
  const lane = { gain: ctx.createGain(), dip: createDip(ctx) };
  lane.gain.gain.value = 0.8;
  scheduleDip(lane.dip.gain, 0);
  userSets(lane, level, t);
  check(`${what} ${Math.round(t * 1000)} ms into the dip survives it`, near(heard(lane, 0.2), level), `heard ${heard(lane, 0.2)}`);
  check(`${what}: the lane is still silent at the rewire`, heard(lane, DIP_FADE + DIP_HOLD / 2) === 0);
}
{
  // The other order: a dip starting while a volume change still glides.
  const ctx = fakeCtx();
  const lane = { gain: ctx.createGain(), dip: createDip(ctx) };
  lane.gain.gain.value = 0.8;
  userSets(lane, 0.3, 0);
  scheduleDip(lane.dip.gain, 0.005);
  check("a dip started during a volume change does not stop it", near(heard(lane, 0.2), 0.3));
}

// ─── The effects chain's duck (fx/chainHost.js) ───

{
  const ctx = fakeCtx();
  const lane = { controlName: "bass", gain: ctx.createGain(), dip: createDip(ctx), chainHost: null };
  lane.gain.gain.value = 0.8;
  const hosts = createChainHosts(ctx, { members: () => [lane], membersOf: (n) => (n === "bass" ? [lane] : []), liveNodes: () => [] });
  hosts.start(0, 0);
  ctx.clock.now = 1;
  hosts.setLane("bass", { desc: emptyChain(), inst: "bass", bpm: 120, loopExtra: 0 });
  check("putting a chain in while playing dips the lane", heard(lane, 1 + DIP_FADE + DIP_HOLD / 2) === 0);
  check("on the dip node, not the lane gain", lane.gain.gain.events.length === 0);
  userSets(lane, 0.25, 1.01);
  check("a volume change inside the chain's dip survives it", near(heard(lane, 1.2), 0.25), `heard ${heard(lane, 1.2)}`);
  await new Promise((r) => setTimeout(r, 60));
  check("and the chain is in the path after the dip", !!lane.chainHost);
  hosts.dispose();
}

// ─── Every dip goes through laneDip.js, none on a lane gain ───

const src = (p) => readFileSync(new URL(`../../static/js/${p}`, import.meta.url), "utf8").replace(/\r\n/g, "\n");
for (const [file, fn] of [["audioEngine.js", "routeTrack"], ["chunkedAudioEngine.js", "routeStem"], ["fx/chainHost.js", "duck"]]) {
  const s = src(file);
  const body = s.slice(s.indexOf(`function ${fn}(`), s.indexOf("\n  }\n", s.indexOf(`function ${fn}(`)));
  check(`${file} ${fn} dips the dip node`, /scheduleDip\(\w+\.dip\.gain,/.test(body));
  check(`${file} ${fn} schedules nothing on the lane gain`, !/\.gain\.gain/.test(body) && !/linearRampToValueAtTime/.test(body));
}
check("fxChain puts the dip after the gain, before the EQ slot", /gain\.connect\(dip\);\s*dip\.connect\(slot\.input\);/.test(src("fxChain.js")));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
