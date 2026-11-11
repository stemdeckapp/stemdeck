// A live input's effects chain, the pure part (static/js/fx/inputChainModel.js):
// the instrument default from the track's input, the stored shape, which
// routes get a chain host (gating), the latency readout and the output
// guess behind the speakers warning. Also the Low latency switch in the
// shared model (chainModel.js): latency and structure.
//
// Run:  node tests/js/input-fx.test.mjs

import {
  inputDefaultInstrument, normalizeInputFx, inputFxIsSet, parseInputFxStore, withInputFx, INPUT_FX_MAX_TRACKS,
  inputHostWanted, planInputRoutes, monitorLatency, inputChainFrames, outputKind, defaultOutputLabel,
} from "../../static/js/fx/inputChainModel.js";
import { normalizeChain, chainLatencyFrames, chainStructureKey, pedalLatencyFrames } from "../../static/js/fx/chainModel.js";
import { pedalDefaults } from "../../static/js/fx/pedalSpecs.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); } else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};

const pedal = (iid, type, inst = "guitar", p = {}) => ({ iid, type, on: true, preset: null, p: { ...pedalDefaults(type, inst), ...p } });
const chainOf = (pedals, extra = {}) => normalizeChain({ pedals, ...extra });
const FACTS = { mono: 128, shaper4x: 192 };

// ── Instrument default ──
check("a microphone input is a voice", inputDefaultInstrument("voice") === "voice");
check("an instrument input is a guitar", inputDefaultInstrument("instrument") === "guitar");
check("an unknown input is a guitar", inputDefaultInstrument(null) === "guitar");

// ── Stored shape ──
{
  const rec = normalizeInputFx(null, "voice");
  check("nothing stored: an empty chain, Low latency off", rec.chain.pedals.length === 0 && rec.low === false && rec.chain.inst === null);
  check("an empty record is not kept", !inputFxIsSet(rec));
  const bad = normalizeInputFx({ chain: { trim: 99, pedals: [{ type: "nope" }, pedal("aaaa", "delay")] }, low: "yes" }, "guitar");
  check("the stored chain is normalised (trim clamped, unknown pedal dropped)", bad.chain.trim === 24 && bad.chain.pedals.length === 1);
  check("Low latency is true only when stored as true", bad.low === false && normalizeInputFx({ low: true }).low === true);
  check("Low latency alone is kept", inputFxIsSet({ chain: normalizeChain(null), low: true }));
  const store = parseInputFxStore(JSON.stringify({ k0000000a: { chain: {}, low: true }, notatrack: {}, k1: {} }));
  check("the store keeps track ids only", Object.keys(store).join() === "k0000000a");
  check("a broken store reads as empty", Object.keys(parseInputFxStore("{not json")).length === 0 && Object.keys(parseInputFxStore("[1]")).length === 0);
  const s1 = withInputFx({}, "k00000001", { chain: chainOf([pedal("aaaa", "delay")]), low: false }, 5);
  check("a set record is stored with its time", s1.k00000001.at === 5 && s1.k00000001.chain.pedals.length === 1);
  const s2 = withInputFx(s1, "k00000001", { chain: normalizeChain(null), low: false }, 6);
  check("an emptied record is removed", !("k00000001" in s2));
  let big = {};
  for (let i = 0; i < INPUT_FX_MAX_TRACKS + 3; i++) big = withInputFx(big, `k${String(i).padStart(8, "0")}`, { chain: normalizeChain(null), low: true }, i);
  check("the store is capped, the oldest go first", Object.keys(big).length === INPUT_FX_MAX_TRACKS && !("k00000000" in big) && `k${String(INPUT_FX_MAX_TRACKS + 2).padStart(8, "0")}` in big);
}

// ── Gating: which routes get a host ──
{
  const delay = chainOf([pedal("aaaa", "delay")]);
  const empty = normalizeChain(null);
  const tracks = [
    { id: "k00000001", channels: [0], desc: delay },
    { id: "k00000002", channels: [1], desc: empty },
  ];
  const on = planInputRoutes({ allowed: true, monitorOn: true, tracks });
  check("monitoring on, allowed: a host for the track with a chain only", on.length === 2 && on[0].host === true && on[1].host === false);
  check("monitoring off: no host at all", planInputRoutes({ allowed: true, monitorOn: false, tracks }).every((r) => !r.host));
  check("a mode that does not allow it: no host at all", planInputRoutes({ allowed: false, monitorOn: true, tracks }).every((r) => !r.host));
  check("nothing armed: no route", planInputRoutes({ allowed: true, monitorOn: true, tracks: [] }).length === 0);
  const same = planInputRoutes({ allowed: true, monitorOn: true, tracks: [tracks[0], { ...tracks[1], channels: [0] }] });
  check("two tracks on one channel: one route (not 6 dB too loud), the first track's chain", same.length === 1 && same[0].id === "k00000001" && same[0].host);
  check("a chain switched off keeps its host (so it crossfades)", inputHostWanted({ ...delay, on: false }));
  check("a trim alone wants a host", inputHostWanted(normalizeChain({ trim: 6 })));
  check("an empty chain wants none", !inputHostWanted(empty));
}

// ── Low latency in the shared model ──
{
  const drive = chainOf([pedal("aaaa", "drive"), pedal("bbbb", "delay"), pedal("cccc", "reverb")]);
  check("a drive adds the measured 4x shaper latency", chainLatencyFrames(drive, FACTS) === 192);
  check("Low latency: the drive adds nothing", chainLatencyFrames(drive, FACTS, { lowLatency: true }) === 0);
  check("delay, chorus, phaser, reverb add nothing either way", ["delay", "chorus", "phaser", "reverb"].every((t) => pedalLatencyFrames(pedal("aaaa", t), FACTS) === 0));
  check("Low latency is part of the structure (a switch crossfades)", chainStructureKey(drive, "guitar", 120, { lowLatency: true }) !== chainStructureKey(drive, "guitar", 120));
  check("without the switch the key is the lanes' key unchanged", chainStructureKey(drive, "guitar", 120) === chainStructureKey(drive, "guitar", 120, {}));
  check("a live input's frames follow its switch", inputChainFrames({ chain: drive, low: false }, FACTS) === 192 && inputChainFrames({ chain: drive, low: true }, FACTS) === 0);
  check("no chain, no frames", inputChainFrames({ chain: normalizeChain(null), low: false }, FACTS) === 0);
  check("a chain switched off adds nothing", chainLatencyFrames({ ...drive, on: false }, FACTS) === 0);
}

// ── Latency readout ──
{
  const a = monitorLatency({ roundTripSec: 0.0105, calibrated: true, chainFrames: 192, rate: 48000 });
  check("round trip plus the chain's frames, in ms", Math.abs(a.roundTripMs - 10.5) < 1e-9 && Math.abs(a.fxMs - 4) < 1e-9 && Math.abs(a.totalMs - 14.5) < 1e-9 && a.measured);
  const b = monitorLatency({ roundTripSec: 0.02, calibrated: false, chainFrames: 0, rate: 44100 });
  check("no chain latency: the round trip alone, an estimate", b.fxMs === 0 && Math.abs(b.totalMs - 20) < 1e-9 && !b.measured);
  check("no round trip: no total", monitorLatency({ roundTripSec: NaN, chainFrames: 192, rate: 48000 }).totalMs === null);
  check("192 frames at 44.1 kHz is 4.35 ms", Math.abs(monitorLatency({ roundTripSec: 0, chainFrames: 192, rate: 44100 }).fxMs - 4.3537) < 1e-3);
}

// ── Output guess (the speakers warning) ──
{
  const cases = [
    ["Default - Speakers (Realtek(R) Audio)", "speakers"],
    ["Speakers (Focusrite USB Audio)", "interface"],
    ["Speakers (2- MG-XU)", "interface"],
    ["Monitor (Universal Audio Apollo Twin)", "interface"],
    ["Headphones (Realtek(R) Audio)", "headphones"],
    ["Headset Earphone (HyperX Cloud)", "headphones"],
    ["AirPods Pro", "headphones"],
    ["LG TV (NVIDIA High Definition Audio)", "speakers"],
    ["MacBook Pro Speakers", "speakers"],
    ["Kopfhörer (USB Audio)", "headphones"],
    ["", "unknown"],
    ["Digital Output (S/PDIF)", "unknown"],
  ];
  for (const [label, want] of cases) check(`output "${label}" is ${want}`, outputKind(label) === want, outputKind(label));
  check("the default output is the one the page plays to", defaultOutputLabel([
    { kind: "audiooutput", deviceId: "abc", label: "Headphones (X)" },
    { kind: "audiooutput", deviceId: "default", label: "Default - Speakers (Y)" },
  ]) === "Default - Speakers (Y)");
  check("no outputs listed: no name", defaultOutputLabel([]) === "");
}

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
