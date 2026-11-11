// Performance's Timeline and Effects tabs (static/js/streamer/streamerTabs.js):
// the stored choice, the keys and the timeline's kept centre. buildTabs
// needs a DOM and is covered by tests/e2e/performance-tabs.spec.mjs.
//
// Run:  node tests/js/streamer-tabs.test.mjs

import { TAB_KEY, TABS, DEFAULT_TAB, tabOf, readTab, writeTab, tabForKey, viewCentre } from "../../static/js/streamer/streamerTabs.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}

// The warnings a blocked store logs are expected here.
const warn = console.warn;
console.warn = () => {};

const memory = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), m };
};
const throwing = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };

check("two tabs, Timeline first and the default", JSON.stringify(TABS) === '["timeline","effects"]' && DEFAULT_TAB === "timeline");
check("the key", TAB_KEY === "stemdeck.streamer.tab");
check("a known tab is itself", tabOf("effects") === "effects" && tabOf("timeline") === "timeline");
check("anything else is Timeline", [null, undefined, "", "Effects", "fx", 1, {}].every((v) => tabOf(v) === "timeline"));

const s = memory();
check("nothing stored: Timeline", readTab(s) === "timeline");
check("stored and read back", writeTab(s, "effects") === true && readTab(s) === "effects" && s.m.get(TAB_KEY) === "effects");
s.m.set(TAB_KEY, "garbage");
check("a stored value that is no tab: Timeline", readTab(s) === "timeline");
check("an unknown tab is stored as Timeline", writeTab(s, "nope") && s.m.get(TAB_KEY) === "timeline");
check("a store that throws reads Timeline", readTab(throwing) === "timeline");
check("a store that throws: writing says so, without throwing", writeTab(throwing, "effects") === false);
check("no store at all", readTab(null) === "timeline" && writeTab(null, "effects") === true);

check("Right moves on", tabForKey("timeline", "ArrowRight") === "effects");
check("Right wraps from the last", tabForKey("effects", "ArrowRight") === "timeline");
check("Left moves back and wraps", tabForKey("effects", "ArrowLeft") === "timeline" && tabForKey("timeline", "ArrowLeft") === "effects");
check("Home and End", tabForKey("effects", "Home") === "timeline" && tabForKey("timeline", "End") === "effects");
check("other keys are not the tabs'", ["ArrowUp", "ArrowDown", "Enter", " ", "Tab", "a"].every((k) => tabForKey("timeline", k) === null));
check("an unknown current tab counts as the first", tabForKey("nope", "ArrowRight") === "effects");

check("the view's centre in seconds", viewCentre(240, 480, 120) === 4);
check("unscrolled: half the view", viewCentre(0, 600, 100) === 3);
check("a negative scroll counts as none", viewCentre(-5, 600, 100) === 3);
check("no scale or no view: nothing to keep", viewCentre(100, 400, 0) === null && viewCentre(100, 1, 120) === null && viewCentre(100, 400, NaN) === null);

console.warn = warn;
console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
