// The studio mode read back from storage (static/js/mode.js). The Mode menu
// once listed a "Streaming" placeholder (value "streaming"); it is gone, so a
// browser that still holds that value, or any other unknown one, must open
// in Practice without an error. Also: the menu lists exactly Practice,
// Advanced, Karaoke and Performance (id "streamer").

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const classes = new Set();
globalThis.document = {
  documentElement: {
    dataset: {},
    classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) },
  },
};

let n = 0;
async function load(stored) {
  store.clear();
  classes.clear();
  if (stored !== undefined) store.set("stemdeck.mode", stored);
  const warn = console.warn;
  const warnings = [];
  console.warn = (...a) => warnings.push(a.join(" "));
  try {
    n += 1;
    const mod = await import(`../../static/js/mode.js?case=${n}`);
    return { mod, warnings };
  } finally {
    console.warn = warn;
  }
}

for (const [label, stored, want] of [
  ["nothing stored opens Practice", undefined, "practice"],
  ["a stored Practice opens Practice", "practice", "practice"],
  ["a stored Advanced opens Advanced", "daw", "daw"],
  ["the removed Streaming placeholder opens Practice", "streaming", "practice"],
  ["an unknown value opens Practice", "kazoo", "practice"],
]) {
  let res;
  try {
    res = await load(stored);
  } catch (err) {
    check(label, false, `threw ${err}`);
    continue;
  }
  check(label, res.mod.getMode() === want, `got ${res.mod.getMode()}`);
  check(`${label}: no warning`, res.warnings.length === 0, res.warnings.join(" | "));
  check(`${label}: <html> class matches`, classes.has(`mode-${want}`));
}

const { mod } = await load(undefined);
check("the menu lists Practice, Advanced, Karaoke and Performance only",
  JSON.stringify(mod.MODES) === JSON.stringify(["practice", "daw", "karaoke", "streamer"]), JSON.stringify(mod.MODES));
check("Streaming cannot be set", mod.setMode("streaming") === "practice" && store.get("stemdeck.mode") !== "streaming");

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
