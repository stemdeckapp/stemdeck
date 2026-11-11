// Performance's live input effects, the pure parts: the chain kept in the
// recording settings (streamer/outputSettings.js inputFx), a take's copy of
// the chain (streamer/recordModel.js takeFxOf), the one monitor route
// Performance plans (fx/inputChainModel.js planInputRoutes), and where the
// strip's add menu goes (fx/menuPlace.js placeMenu).
import {
  defaultRecordingSettings, parseRecordingSettings, serializeRecordingSettings, applySettings, inputFxOf,
} from "../../static/js/streamer/outputSettings.js";
import { takeFxOf } from "../../static/js/streamer/recordModel.js";
import { planInputRoutes, normalizeInputFx, inputFxIsSet } from "../../static/js/fx/inputChainModel.js";
import { normalizeChain } from "../../static/js/fx/chainModel.js";
import { pedalDefaults } from "../../static/js/fx/pedalSpecs.js";
import { placeMenu, MENU_EDGE, MENU_GAP, MENU_MIN_H } from "../../static/js/fx/menuPlace.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const delay = { iid: "dddd", type: "delay", on: true, preset: null, p: pedalDefaults("delay", "voice") };
const chain = { inst: null, on: true, trim: 3, pedals: [delay] };

// ── The chain in the recording settings ──
{
  const d = defaultRecordingSettings();
  check("no inputFx by default (the stored shape is unchanged)", !("inputFx" in d) && !("inputFx" in parseRecordingSettings(null)));
  const set = applySettings(d, { inputFx: { chain, low: true } });
  check("a chain and Low latency are kept", eq(set.inputFx, { chain, low: true }));
  const back = parseRecordingSettings(JSON.stringify(serializeRecordingSettings(set)));
  check("round trip through the store", eq(back.inputFx, { chain, low: true }) && back.fps === d.fps);
  check("null removes it", !("inputFx" in applySettings(set, { inputFx: null })));
  check("another setting keeps it", eq(applySettings(set, { fps: 60 }).inputFx, { chain, low: true }));
  check("Low latency alone is kept", eq(inputFxOf({ low: true }), { chain: null, low: true }));
  check("nothing set is nothing", inputFxOf({ chain: null, low: false }) === null && inputFxOf({}) === null);
  for (const bad of [7, "x", [], [chain], { chain: [1, 2] }, { chain: "x" }, { low: "yes" }]) {
    check(`garbage ${JSON.stringify(bad).slice(0, 24)} reads as none`, inputFxOf(bad) === null);
  }
  const huge = { chain: { pedals: Array.from({ length: 2000 }, () => ({ type: "delay", p: { note: "x".repeat(10) } })) }, low: false };
  check("a record far too large reads as none", inputFxOf(huge) === null);
  check("a stored record is a copy, not the caller's object", inputFxOf({ chain, low: false }).chain !== chain);
  // Hear myself is never part of the settings.
  check("Hear myself is not a setting", !("monitor" in parseRecordingSettings({ monitor: true })) && !("hear" in parseRecordingSettings({ hear: true })));
}

// ── The take's copy of the chain ──
{
  check("a take with a chain keeps it and its instrument", eq(takeFxOf({ chain, inst: "voice" }), { chain, inst: "voice" }));
  check("an unknown instrument is a guitar (the general voicings)", takeFxOf({ chain, inst: "kazoo" }).inst === "guitar");
  for (const bad of [null, 1, {}, { chain: null }, { chain: [] }, { chain: "x", inst: "voice" }]) {
    check(`take record ${JSON.stringify(bad)} has no chain`, takeFxOf(bad) === null);
  }
  check("a take record far too large has no chain", takeFxOf({ chain: { pad: "x".repeat(20000) }, inst: "voice" }) === null);
  check("the take keeps a copy (a later edit of the live chain does not change it)", (() => {
    const live = { ...chain, pedals: [{ ...delay }] };
    const kept = takeFxOf({ chain: live, inst: "voice" });
    live.pedals[0].on = false;
    return kept.chain.pedals[0].on === true;
  })());
}

// ── The monitor route Performance plans ──
{
  const desc = normalizeChain(chain, "voice");
  const one = (allowed, monitorOn, d = desc, channels = [0]) => planInputRoutes({ allowed, monitorOn, tracks: [{ id: "performance", channels, desc: d }] });
  check("one route on the input's channels", eq(one(true, true).map((p) => [p.key, p.channels.join()]), [["performance", "0"]]));
  check("a host with monitoring on, allowed, and a chain", one(true, true)[0].host === true);
  check("no host while Hear myself is off", one(true, false)[0].host === false);
  check("no host where it is not allowed (another mode)", one(false, true)[0].host === false);
  check("no host for an empty chain", one(true, true, normalizeChain(null, "voice"))[0].host === false);
  check("a chain switched off keeps its host (switching on again crossfades)", one(true, true, { ...desc, on: false })[0].host === true);
  check("a pair of channels is one stereo route", eq(one(true, true, desc, [0, 1])[0].channels, [0, 1]));
  check("no channels, no route", planInputRoutes({ allowed: true, monitorOn: true, tracks: [{ id: "performance", channels: [], desc }] }).length === 0);
  const rec = normalizeInputFx({ chain, low: true }, "voice");
  check("the stored record normalises with the input's instrument", rec.low === true && rec.chain.pedals[0].type === "delay" && inputFxIsSet(rec));
}

// ── Where the add menu goes ──
{
  const btn = (top, h = 32, left = 100) => ({ left, top, bottom: top + h });
  const below = placeMenu(btn(100), 140, 180, 1280, 720);
  check("below the button when it fits", below.top === 132 + MENU_GAP && below.maxHeight === null && below.left === 100);
  const above = placeMenu(btn(600), 140, 180, 1280, 720);
  check("above the button when only that fits", above.top === 600 - MENU_GAP - 180 && above.maxHeight === null);
  check("above, it ends over the button and starts inside the window", above.top + 180 <= 600 && above.top >= MENU_EDGE);
  const tiny = placeMenu(btn(150), 140, 400, 1280, 360);
  check("a short window: the roomier side, with a maximum height", tiny.maxHeight !== null && tiny.top + tiny.maxHeight <= 360 - MENU_EDGE + 0.5);
  const top = placeMenu(btn(250), 140, 400, 1280, 330);
  check("the roomier side above: it starts at least at the edge", top.top >= MENU_EDGE && top.top + top.maxHeight <= 250);
  check("never below the least height", placeMenu(btn(5, 10), 140, 400, 1280, 60).maxHeight >= MENU_MIN_H);
  const right = placeMenu(btn(100, 32, 1250), 140, 100, 1280, 720);
  check("kept inside the window on the right", right.left + 140 <= 1280 - MENU_EDGE);
  check("kept inside on the left", placeMenu(btn(100, 32, -40), 140, 100, 1280, 720).left === MENU_EDGE);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exitCode = 1;
