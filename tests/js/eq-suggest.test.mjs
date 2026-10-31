// Suggest EQ, the browser's part (static/js/eq/eqSuggestMath.js): the request
// URLs, the server's answer turned into an EQ descriptor or a panel's knob
// values, merge, the loudness-matched "current" for an A/B, and the reasons
// as text.
//
// The parity checks use real answers of the server (tests/fixtures/
// suggest_answers.json, captured from GET suggest/eq and suggest/master on a
// copy of "Shining Star"): whatever the panel turns the answer into must have
// exactly the response of the bands the server fitted (eq.bands, out).
//
// Run:  node tests/js/eq-suggest.test.mjs

import { readFileSync } from "node:fs";
import {
  MASTER_LANE, canSuggest, canMerge, suggestUrl, descriptorFromAnswer, pedalControls, keysControls,
  matchedCurrent, formatReason, reasonLines, targetOptions, masterParamsFromAnswer, presetLine,
} from "../../static/js/eq/eqSuggestMath.js";
import { eqResponseDb, normalizeEq, encodeFxParam } from "../../static/js/eqMath.js";
import { controlsFromEq } from "../../static/js/eq/eqPedalMath.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`); }
};

const answers = JSON.parse(readFileSync(new URL("../fixtures/suggest_answers.json", import.meta.url), "utf8"));
const FREQS = Array.from({ length: 200 }, (_, i) => 20 * 1000 ** (i / 199));
const maxDiff = (a, b) => Math.max(...a.map((v, i) => Math.abs(v - b[i])));
const serverEq = (ans) => normalizeEq({ id: "eq", v: 1, params: { out: ans.eq.out, bands: ans.eq.bands.map((b) => ({ on: true, ...b })) } });
// A fake t(): the key with its variables, so the formatting is visible.
const t = (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key);

// ─── Which panels ───
{
  for (const k of ["graph", "drum", "bass", "guitar", "keys"]) check(`${k} offers Suggest`, canSuggest(k));
  check("the voice placeholder does not", !canSuggest("voice"));
  check("merge: graphical panels", canMerge("graph", "other") && canMerge("drum", "drums"));
  check("merge: not the fixed panels", !canMerge("bass", "bass") && !canMerge("keys", "piano"));
  check("merge: the master", canMerge("graph", MASTER_LANE));
}

// ─── URLs ───
{
  const laneEq = normalizeEq({ id: "eq", v: 1, params: { out: 0, bands: [{ type: "bell", f: 300, g: -3, q: 1 }] } });
  const u = new URL(suggestUrl({ jobId: "abc123def456", lane: "bass", kind: "bass", target: "bass.warm", mode: "replace", laneEq }), "http://x");
  check("lane url: route", u.pathname === "/api/jobs/abc123def456/suggest/eq");
  check("lane url: lane, layout, target, mode", u.searchParams.get("lane") === "bass" && u.searchParams.get("layout") === "bass"
    && u.searchParams.get("target") === "bass.warm" && u.searchParams.get("mode") === "replace");
  check("lane url: the lane's EQ as fx, only that lane", u.searchParams.get("fx") === encodeFxParam({ bass: laneEq }));
  const flat = new URL(suggestUrl({ jobId: "abc123def456", lane: "other", kind: "graph", laneEq: normalizeEq(null) }), "http://x");
  check("lane url: no fx for a flat EQ, no target for the default", !flat.searchParams.has("fx") && !flat.searchParams.has("target"));
  const m = new URL(suggestUrl({ jobId: "abc123def456", lane: MASTER_LANE, target: "mix.warm", mode: "merge", mixQuery: "stems=bass,drums&gains=1.000,0.500&fx=AAA" }), "http://x");
  check("master url: route", m.pathname === "/api/jobs/abc123def456/suggest/master");
  check("master url: keeps the export's own query", m.searchParams.get("stems") === "bass,drums" && m.searchParams.get("gains") === "1.000,0.500" && m.searchParams.get("fx") === "AAA");
  check("master url: target and mode", m.searchParams.get("target") === "mix.warm" && m.searchParams.get("mode") === "merge");
  check("job id is encoded", suggestUrl({ jobId: "../x", lane: "bass", kind: "bass" }).startsWith("/api/jobs/..%2Fx/"));
}

// ─── Parity: what the panel applies sounds like what the server fitted ───
for (const [name, kind] of [["bass", "bass"], ["guitar", "guitar"], ["keys", "keys"], ["drum", "drum"], ["master", "graph"]]) {
  const ans = answers[name];
  const desc = descriptorFromAnswer(ans, kind, normalizeEq(null));
  const d = maxDiff(eqResponseDb(desc, FREQS, 44100), eqResponseDb(serverEq(ans), FREQS, 44100));
  check(`${name}: the panel's EQ has the server's response (max ${d.toFixed(4)} dB)`, d < 0.01, d.toFixed(4));
  check(`${name}: applying turns the EQ on`, desc.bypass === false);
}

// ─── Knobs ───
{
  const ans = answers.bass;
  const c = pedalControls("bass", ans);
  const low = ans.bands.find((b) => b.slot === "low");
  check("bass: the low band lands on the low knobs", c.lf === low.f && c.lg === low.g && c.lq === low.q);
  check("bass: the output gain is the level knob", c.lvl === ans.out);
  const desc = descriptorFromAnswer(ans, "bass", normalizeEq(null));
  const back = controlsFromEq("bass", desc);
  check("bass: the panel reads its knobs back from the applied EQ", JSON.stringify(back) === JSON.stringify(c));
  const keys = keysControls(answers.keys);
  const pres = answers.keys.bands.find((b) => b.slot === "presence");
  check("keys: presence gain and focus", !pres || (keys.presence === pres.g && keys.presF === pres.f));
  check("keys: level clamped to the panel's +-12", keysControls({ bands: [], out: 30 }).lvl === 12);
  check("pedal: an empty answer is the flat starting point", JSON.stringify(pedalControls("guitar", { bands: [], out: 0 })) === JSON.stringify(controlsFromEq("guitar", normalizeEq(null))));
}

// ─── Merge and replace ───
{
  const ans = answers.graphMerge;
  const current = normalizeEq({
    id: "eq", v: 1,
    params: { out: 1.5, bands: [{ type: "bell", f: 300, g: -3, q: 1 }, { on: false, type: "highshelf", f: 9000, g: 4 }], ui: { ab: { cur: "B", other: { out: 0, bands: [] } }, junk: 1 } },
  });
  const merged = descriptorFromAnswer(ans, "graph", current, "merge");
  check("merge keeps every current band, on or off, first", merged.params.bands[0].f === 300 && merged.params.bands[1].on === false);
  check("merge adds the new bands", merged.params.bands.length === 2 + ans.bands.length);
  check("merge takes the server's output gain", merged.params.out === ans.out);
  check("the A/B slot is kept, other panel state is not", merged.params.ui?.ab?.cur === "B" && merged.params.ui.junk === undefined);
  const replaced = descriptorFromAnswer(ans, "graph", current, "replace");
  check("replace drops the current bands", replaced.params.bands.length === ans.bands.length);
  const many = { ...ans, bands: Array.from({ length: 12 }, (_, i) => ({ type: "bell", f: 100 * (i + 1), g: 1, q: 1, slope: 12 })) };
  check("never more than the engine's 12 bands", descriptorFromAnswer(many, "graph", current, "merge").params.bands.length === 12);
}

// ─── Loudness-matched current ───
{
  const ans = { loudness: { current_change: 2.5, suggested_change: 0.1 } };
  const cur = normalizeEq({ id: "eq", v: 1, params: { out: 1, bands: [{ type: "bell", f: 1000, g: 6, q: 1 }] } });
  const m = matchedCurrent(cur, ans);
  check("matched current: output moved by the estimated difference", Math.abs(m.params.out - (1 + 0.1 - 2.5)) < 0.011);
  check("matched current: same bands", m.params.bands.length === 1 && m.params.bands[0].g === 6);
  const byp = matchedCurrent({ ...cur, bypass: true }, { loudness: { current_change: 0, suggested_change: -0.2 } });
  check("matched current of a bypassed EQ: flat with the trim", byp.bypass === false && byp.params.bands.length === 0 && byp.params.out === -0.2);
  const real = answers.drum;
  const sug = descriptorFromAnswer(real, "drum", normalizeEq(null));
  check("the drum suggestion is loudness-neutral against the raw lane (estimate)", Math.abs(real.loudness.suggested_change) < 0.15);
  check("its matched flat current gets the same tiny trim", Math.abs(matchedCurrent(normalizeEq(null), real).params.out - real.loudness.suggested_change) < 0.011 && sug.params.out === real.out);
}

// ─── Reasons ───
{
  const line = formatReason({ key: "suggest.why.bellCut", vars: { f: 250, g: -3.5, dev: 4.26, region: "lowmid" } }, t);
  check("a reason formats the frequency", line.includes('"f":"250 Hz"'), line);
  check("a reason formats the gain with its unit and a real minus", line.includes('"g":"−3.5 dB"'), line);
  check("a reason shows the deviation without its sign, one decimal", line.includes('"dev":"4.3"'), line);
  check("a reason translates the region", line.includes('"region":"suggest.region.lowmid {}"') || line.includes('"region":"suggest.region.lowmid"'), line);
  const below = formatReason({ key: "suggest.why.bellBoost", vars: { f: 1250, g: 2, dev: -3.04, region: "mid" } }, t);
  check("below the target: the sign is in the words, not the number", below.includes('"dev":"3.0"') && below.includes('"f":"1.25 kHz"'), below);
  const lines = reasonLines(answers.master, t);
  check("master: one line per reason plus the preset's", lines.length === answers.master.why.length + 1);
  check("master: the preset's reason is last", lines[lines.length - 1].startsWith(answers.master.master.why.key));
  const dashes = new RegExp(`[${String.fromCharCode(0x2013, 0x2014)}]`);
  check("no em or en dash in any formatted line", lines.every((l) => !dashes.test(l)));
  const opts = targetOptions(answers.master, (k) => `T(${k})`);
  check("master: four targets, translated labels", opts.length === 4 && opts[0].label.startsWith("T(suggest.target."));
}

// ─── The master preset ───
{
  const p = masterParamsFromAnswer(answers.master);
  check("master preset: masterMath's shape", p && ["gentle", "streaming", "louder", "custom"].includes(p.preset) && "target_lufs" in p && "ceiling_dbtp" in p && "comp" in p);
  check("no preset on a lane answer", masterParamsFromAnswer(answers.bass) === null);
  const line = presetLine(answers.master, t);
  check("the preset line names the preset and its target", line.startsWith("suggest.presetLine") && line.includes(`master.preset.${answers.master.master.preset}`), line);
  check("a gentle preset says no change", presetLine({ master: { preset: "gentle", target_lufs: null } }, t).startsWith("suggest.presetLineGentle"));
  check("no preset line for a lane", presetLine(answers.bass, t) === "");
}

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
