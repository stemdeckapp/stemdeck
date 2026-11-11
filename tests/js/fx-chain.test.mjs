// Pure logic of the Advanced mode effects chain (static/js/fx/): the pedal
// specs and presets, the chain descriptor (normalising untrusted data,
// order, add, remove, move, presets, instrument), tempo sync, tails,
// structure keys (what forces a crossfaded rebuild), mix laws, and the
// export print's canonical key.
//
// Run:  node tests/js/fx-chain.test.mjs

import {
  PEDALS, INSTRUMENTS, SYNC_FACTORS, mixGains, pedalDefaults, presetsFor, presetById, typesFor,
  controlsFor, clampControl,
} from "../../static/js/fx/pedalSpecs.js";
import {
  CHAIN_ID, MAX_PEDALS, TAIL_CAP_SEC, normalizeChain, chainIsSet, chainIsActive, addPedal, removePedal,
  movePedal, setPedalOn, setPedalParam, applyPreset, setInstrument, bpmFromBeats, syncSeconds,
  delaySeconds, pedalStructureKey, chainStructureKey, pedalTailSec, chainTailSec, canonicalChain,
  laneInstrument, laneTakesChain, defaultIndex, newIid, emptyChain, pedalMix,
} from "../../static/js/fx/chainModel.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};
const near = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps;

// A deterministic random source, so ids are reproducible.
function seeded(seed) {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

// ─── Specs and presets ───

check("three instruments", INSTRUMENTS.join() === "guitar,bass,voice");
for (const [type, spec] of Object.entries(PEDALS)) {
  check(`${type}: every control has a name and a Guide key`, spec.controls.every((c) => c.name?.startsWith("fx.") && c.help?.startsWith("help.")));
  for (const inst of INSTRUMENTS) {
    const presets = presetsFor(type, inst);
    check(`${type} has three presets for ${inst}`, presets.length === 3, String(presets.length));
    for (const p of presets) {
      for (const [k, v] of Object.entries(p.p)) {
        const c = spec.controls.find((x) => x.id === k);
        check(`preset ${p.id}: ${k} is a control in range`, !!c && clampControl(c, v, inst) === v, `${k}=${v}`);
      }
    }
  }
}
check("presetById finds a preset with its type", presetById("bass.delay.dub")?.type === "delay");
check("an unknown preset is null", presetById("guitar.delay.nope") === null);
check("ducking is a voice-only control",
  controlsFor("delay", "voice").some((c) => c.id === "duck") && !controlsFor("delay", "guitar").some((c) => c.id === "duck"));
check("delay is offered on every instrument", INSTRUMENTS.every((i) => typesFor(i).includes("delay")));
check("bass delay defaults cut the repeats' lows", pedalDefaults("delay", "bass").lowcut >= 150);

// ─── Mix laws ───

{
  const p0 = mixGains(0, "pedal");
  check("pedal law: mix 0 is exactly dry 1, wet 0", p0.dry === 1 && p0.wet === 0);
  const p1 = mixGains(1, "pedal");
  check("pedal law: mix 1 is exactly dry 0, wet 1", p1.dry === 0 && p1.wet === 1);
  const ph = mixGains(0.5, "pedal");
  check("pedal law: equal power at the middle", near(ph.dry ** 2 + ph.wet ** 2, 1, 1e-12) && near(ph.dry, ph.wet, 1e-12));
  const s = mixGains(0.25, "send");
  check("send law: dry stays at 1, wet adds", s.dry === 1 && s.wet === 0.25);
  check("voice delay uses the send law (D10)", pedalMix({ type: "delay", p: { mix: 0.3 } }, "voice").dry === 1);
  check("guitar delay uses the pedal law", pedalMix({ type: "delay", p: { mix: 0.3 } }, "guitar").dry < 1);
}

// ─── Tempo ───

{
  const beats = [];
  for (let i = 0; i < 40; i++) beats.push(i * 0.5 + (i % 3 === 0 ? 0.004 : 0));
  check("bpm from a 0.5 s grid is 120", near(bpmFromBeats(beats), 120, 0.01), String(bpmFromBeats(beats)));
  check("no grid: no tempo", bpmFromBeats([]) === null && bpmFromBeats(null) === null);
  check("a grid outside 20..400 bpm is refused", bpmFromBeats([0, 10]) === null);
  check("1/4 at 120 bpm is 0.5 s", near(syncSeconds("1/4", 120), 0.5));
  check("dotted 1/8 at 120 bpm is 0.375 s", near(syncSeconds("1/8d", 120), 0.375));
  check("1/8 triplet at 90 bpm is 2/9 s", near(syncSeconds("1/8t", 90), (60 / 90) / 3));
  check("1/16 at 100 bpm is 0.15 s", near(syncSeconds("1/16", 100), 0.15));
  check("sync without a tempo: none", syncSeconds("1/4", null) === null);
  check("every sync option has a factor", PEDALS.delay.controls.find((c) => c.id === "sync").options
    .every((o) => o.v === "off" || SYNC_FACTORS[o.v] > 0));
  const p = { ...pedalDefaults("delay", "guitar"), sync: "1/4", time: 0.3 };
  check("a synced delay plays the division", near(delaySeconds(p, 120), 0.5));
  check("a synced delay without a tempo plays its Time knob", near(delaySeconds(p, null), 0.3));
  check("a synced time beyond the knob is clamped", near(delaySeconds({ ...p, sync: "1/4" }, 20), 2));
}

// ─── Normalising untrusted data ───

{
  const n = normalizeChain(null);
  check("nothing stored: an empty chain", n.id === CHAIN_ID && n.pedals.length === 0 && n.on && n.trim === 0);
  const raw = {
    id: "chain", inst: "kazoo", on: "yes", trim: 99, extra: 1,
    pedals: [
      { iid: "aaaa", type: "delay", on: true, p: { time: 99, fb: 2, mix: -1, char: "warp", bogus: 3 } },
      { iid: "aaaa", type: "delay", p: { fb: 0.5 } },
      { iid: "bbbb", type: "fuzzbox", p: {} },
      "nonsense",
      ...Array.from({ length: 12 }, (_, i) => ({ iid: `c${String(i).padStart(3, "0")}`, type: "delay" })),
    ],
  };
  const c = normalizeChain(raw, "bass");
  check("unknown instrument becomes null (follow the lane)", c.inst === null);
  check("trim clamped to +24 dB", c.trim === 24);
  check("on: anything but false is on", c.on === true);
  check("unknown fields dropped", !("extra" in c));
  check(`at most ${MAX_PEDALS} pedals`, c.pedals.length === MAX_PEDALS, String(c.pedals.length));
  check("unknown pedal types dropped", c.pedals.every((p) => p.type === "delay"));
  const ids = c.pedals.map((p) => p.iid);
  check("instance ids are unique", new Set(ids).size === ids.length);
  const first = c.pedals[0].p;
  check("time clamped to 2 s", first.time === 2);
  check("feedback clamped to 98 %", first.fb === 0.98);
  check("mix clamped to 0", first.mix === 0);
  check("unknown character falls back to the lane default", first.char === pedalDefaults("delay", "bass").char);
  check("unknown control keys dropped", !("bogus" in first));
  check("a missing control gets its instrument default", c.pedals[1].p.lowcut === pedalDefaults("delay", "bass").lowcut);
  check("normalising twice changes nothing", JSON.stringify(normalizeChain(c, "bass")) === JSON.stringify(c));
  check("chainIsSet: empty chain is not set", !chainIsSet(emptyChain()));
  check("chainIsSet: a trim alone is set", chainIsSet({ ...emptyChain(), trim: -3 }));
  check("chainIsActive: off chain is not active", !chainIsActive({ ...c, on: false }));
  check("chainIsActive: all pedals off and no trim is not active",
    !chainIsActive({ ...emptyChain(), pedals: [{ ...c.pedals[0], on: false }] }));
}

// ─── Editing ───

{
  const rand = seeded(7);
  let c = emptyChain();
  c = addPedal(c, "delay", "guitar", { rand });
  c = addPedal(c, "delay", "guitar", { rand, presetId: "guitar.delay.oldTape" });
  check("two delays added", c.pedals.length === 2);
  check("a preset add carries the preset's values", c.pedals.some((p) => p.preset === "guitar.delay.oldTape" && p.p.char === "tape"));
  const [a, b] = c.pedals.map((p) => p.iid);
  const moved = movePedal(c, b, 0);
  check("move puts the pedal first", moved.pedals[0].iid === b && moved.pedals[1].iid === a);
  check("move does not change the original", c.pedals[0].iid === a);
  check("move past the end clamps", movePedal(c, a, 99).pedals[1].iid === a);
  check("moving an unknown id changes nothing", JSON.stringify(movePedal(c, "zzzz", 0)) === JSON.stringify(c));
  const off = setPedalOn(c, a, false);
  check("per-pedal off", off.pedals.find((p) => p.iid === a).on === false);
  const knob = setPedalParam(c, b, "fb", 5, "guitar");
  check("a knob turn clamps and leaves the preset", knob.pedals.find((p) => p.iid === b).p.fb === 0.98
    && knob.pedals.find((p) => p.iid === b).preset === null);
  check("an unknown control is ignored", JSON.stringify(setPedalParam(c, b, "nope", 1, "guitar")) === JSON.stringify(c));
  const pre = applyPreset(c, a, "guitar.delay.dotted", "guitar");
  check("applying a preset sets its values", pre.pedals.find((p) => p.iid === a).p.sync === "1/8d");
  check("a preset of another type is refused", JSON.stringify(applyPreset(c, a, "not.a.preset", "guitar")) === JSON.stringify(c));
  const rem = removePedal(c, a);
  check("remove", rem.pedals.length === 1 && rem.pedals[0].iid === b);
  let full = emptyChain();
  for (let i = 0; i < MAX_PEDALS; i++) full = addPedal(full, "delay", "voice", { rand });
  check(`a ${MAX_PEDALS + 1}th pedal is refused`, addPedal(full, "delay", "voice", { rand }) === null);
  check("ids from a seeded source are unique", new Set(full.pedals.map((p) => p.iid)).size === MAX_PEDALS);
  check("newIid avoids taken ids", !["aaaa"].includes(newIid(["aaaa"], () => 0.5)));
  // Instrument change: voice-only ducking goes back to its default, presets
  // of the old instrument are dropped.
  let v = addPedal(emptyChain(), "delay", "voice", { rand, presetId: "voice.delay.ducked" });
  check("the voice preset ducks", v.pedals[0].p.duck === 10);
  v = setInstrument(v, "guitar", "voice");
  check("leaving voice resets the ducking", v.pedals[0].p.duck === 0 && v.inst === "guitar");
  check("leaving voice drops the voice preset", v.pedals[0].preset === null);
  check("other values survive an instrument change", v.pedals[0].p.sync === "1/8d");
}

// ─── Default order ───

{
  const ped = (type) => ({ type });
  check("delay goes after a phaser on guitar", defaultIndex([ped("phaser")], "delay", "guitar") === 1);
  check("delay goes before a reverb on guitar", defaultIndex([ped("reverb")], "delay", "guitar") === 0);
  check("drive goes before a delay on bass", defaultIndex([ped("delay")], "drive", "bass") === 0);
}

// ─── Structure keys: what forces a crossfaded rebuild ───

{
  const d = { iid: "aaaa", type: "delay", on: true, p: pedalDefaults("delay", "guitar") };
  const k = (p) => pedalStructureKey({ ...d, p: { ...d.p, ...p } }, 120);
  check("analog time change glides (no rebuild)", k({ char: "analog", time: 0.3 }) === k({ char: "analog", time: 0.4 }));
  check("digital time change rebuilds", k({ char: "digital", time: 0.3 }) !== k({ char: "digital", time: 0.4 }));
  check("character change rebuilds", k({ char: "analog" }) !== k({ char: "tape" }));
  check("stereo change rebuilds", k({ stereo: "mono" }) !== k({ stereo: "pingpong" }));
  check("feedback is smooth", k({ fb: 0.1 }) === k({ fb: 0.9 }));
  check("mix is smooth", k({ mix: 0.1 }) === k({ mix: 0.9 }));
  check("modulation rate restarts the LFO", k({ mod: 1, rate: 0.5 }) !== k({ mod: 1, rate: 1 }));
  check("modulation rate without depth is smooth", k({ mod: 0, rate: 0.5 }) === k({ mod: 0, rate: 1 }));
  const c1 = { ...emptyChain(), pedals: [d, { ...d, iid: "bbbb" }] };
  const c2 = movePedal(c1, "bbbb", 0);
  check("reorder changes the chain's structure", chainStructureKey(c1, "guitar", 120) !== chainStructureKey(c2, "guitar", 120));
  check("instrument changes the chain's structure", chainStructureKey(c1, "guitar", 120) !== chainStructureKey(c1, "bass", 120));
}

// ─── Tails ───

{
  const d = (p) => ({ type: "delay", on: true, p: { ...pedalDefaults("delay", "guitar"), mod: 0, sync: "off", ...p } });
  check("no feedback: one repeat", near(pedalTailSec(d({ time: 0.3, fb: 0 }), null), 0.3));
  // 0.5^n <= 1e-3 at n = 10: eleven delay times.
  check("50 % feedback: to -60 dB", near(pedalTailSec(d({ time: 0.2, fb: 0.5 }), null), 0.2 * 11, 1e-9));
  check(`a chain's tail is capped at ${TAIL_CAP_SEC} s`, chainTailSec({ pedals: [d({ time: 1, fb: 0.98 })] }, null) === TAIL_CAP_SEC);
}

// ─── The export print key ───

{
  const base = addPedal(emptyChain(), "delay", "guitar", { rand: seeded(3) });
  const key = canonicalChain(base, "guitar", 120);
  check("the key is JSON", typeof JSON.parse(key) === "object");
  const other = { ...base, pedals: base.pedals.map((p) => ({ ...p, iid: "zzzz" })) };
  check("instance ids are not part of the key", canonicalChain(other, "guitar", 120) === key);
  check("the instrument is part of the key", canonicalChain(base, "bass", 120) !== key);
  check("a pedal that is off is not part of the key",
    canonicalChain(setPedalOn(base, base.pedals[0].iid, false), "guitar", 120) === canonicalChain(emptyChain(), "guitar", 120));
  check("tempo is part of the key only when a delay syncs", canonicalChain(base, "guitar", 90) === key);
  const synced = setPedalParam(base, base.pedals[0].iid, "sync", "1/4", "guitar");
  check("a synced delay's key follows the tempo", canonicalChain(synced, "guitar", 90) !== canonicalChain(synced, "guitar", 120));
}

// ─── Lanes ───

check("bass lane is bass", laneInstrument("bass") === "bass");
check("voice panel lanes are voice", laneInstrument("voice") === "voice");
check("guitar, other and drum panels are guitar", ["guitar", "graph", "drum", "keys"].every((k) => laneInstrument(k) === "guitar"));
check("original has no chain (D4)", !laneTakesChain("original"));
check("a stem and a track take a chain", laneTakesChain("guitar") && laneTakesChain("k0123abcd"));
check("the master id takes no chain", !laneTakesChain("__master"));

// ─── Reverb impulse responses (fx/irGen.js) ───

{
  const { generateIr } = await import("../../static/js/fx/irGen.js");
  // RT60 from a Schroeder backward integral, fitted between -5 and -35 dB.
  const rt60 = (d, rate) => {
    const e = new Float64Array(d.length);
    let acc = 0;
    for (let i = d.length - 1; i >= 0; i--) { acc += d[i] * d[i]; e[i] = acc; }
    const db = (i) => 10 * Math.log10(e[i] / e[0]);
    let a = 0;
    while (db(a) > -5) a++;
    let b = a;
    while (b < d.length - 1 && db(b) > -35) b++;
    return ((b - a) / rate) * 2;
  };
  for (const type of ["room", "hall", "plate", "spring"]) {
    for (const decay of [0.8, 2.5]) {
      const [l, r] = generateIr({ type, decay, drip: 0.6, rate: 48000 });
      let el = 0;
      for (const v of l) el += v * v;
      check(`IR ${type} ${decay} s: energy normalised`, Math.abs(el - 1) < 1e-4, String(el));
      const t = rt60(l, 48000);
      check(`IR ${type} ${decay} s: RT60 ${t.toFixed(3)} s within 10 %`, Math.abs(t - decay) / decay < 0.1);
      check(`IR ${type}: the two sides differ (width)`, l.some((v, i) => v !== r[i]));
    }
  }
  const a = generateIr({ type: "plate", decay: 1.2, drip: 0, rate: 44100 });
  check("the same setting gives the same IR (cached, seeded)", a === generateIr({ type: "plate", decay: 1.2, drip: 0, rate: 44100 }));
}

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
