// static/js/constants.js -- the lane vocabulary the player and mixer build on.
//
// Nothing imported this module before. effectiveStemOrder decides both what
// gets rendered and in what sequence (waveform stacking, mixer rows), and it is
// the only place that knows the on-demand vocal split (#275) replaces the
// vocals lane rather than adding to it. Get that wrong and a job that has been
// split either shows vocals three times or loses the split entirely -- neither
// of which any backend test can see.
//
// syncStemNamesFromAPI is the other half: the fallback list here is a copy of
// STEM_NAMES in app/core/config.py, and the API is meant to be canonical. The
// tests below pin that it actually defers, and that a failed or malformed
// response leaves the working fallback in place instead of emptying the player.
//
// Run:  node tests/js/stem-order.test.mjs

import {
  EXTRA_STEM_NAMES,
  STEM_COLORS,
  STEM_NAMES,
  TRACK_NAMES,
  effectiveStemOrder,
  syncStemNamesFromAPI,
} from "../../static/js/constants.js";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const same = (name, actual, expected) =>
  check(
    name,
    JSON.stringify(actual) === JSON.stringify(expected),
    `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`,
  );

// ── the fallback vocabulary ────────────────────────────────────────────────
{
  same("the fallback stem list matches the backend's", STEM_NAMES, [
    "vocals",
    "drums",
    "bass",
    "guitar",
    "piano",
    "other",
  ]);
  same("the split stems are named", EXTRA_STEM_NAMES, ["lead_vocals", "backing_vocals"]);
  check("original leads the track list", TRACK_NAMES[0] === "original");
  check(
    "every stem has a colour, including the split ones",
    [...STEM_NAMES, ...EXTRA_STEM_NAMES, "original"].every((n) => typeof STEM_COLORS[n] === "string"),
  );
}

// ── effectiveStemOrder: an ordinary job ────────────────────────────────────
{
  const present = new Set(STEM_NAMES);
  same("an unsplit job renders exactly the base stems", effectiveStemOrder(present), STEM_NAMES);

  // A job that produced fewer stems still gets the full order back: this
  // function decides the lane sequence, and the caller filters by presence.
  same(
    "a job missing stems keeps the canonical order",
    effectiveStemOrder(new Set(["vocals", "drums"])),
    STEM_NAMES,
  );
  same("a job with no stems at all does not throw", effectiveStemOrder(new Set()), STEM_NAMES);
}

// ── effectiveStemOrder: a split job ────────────────────────────────────────
{
  const split = new Set([...STEM_NAMES, "lead_vocals", "backing_vocals"]);
  const order = effectiveStemOrder(split);

  same("the vocals lane is replaced by the two split lanes", order, [
    "lead_vocals",
    "backing_vocals",
    "drums",
    "bass",
    "guitar",
    "piano",
    "other",
  ]);

  // The replacement, not an addition: rendering both would play the vocal
  // twice, at double amplitude.
  check("vocals is gone rather than duplicated", !order.includes("vocals"));
  check("no lane is repeated", new Set(order).size === order.length);
  check("the split lanes sit where vocals was", order[0] === "lead_vocals");
  check("the other lanes keep their order", order.slice(2).join() === "drums,bass,guitar,piano,other");
}

// ── a half-finished split ──────────────────────────────────────────────────
{
  // Both files are required. One of them present means the split failed
  // partway, and swapping the lane then would leave a silent lane on screen.
  same(
    "only the lead vocal present is not a split",
    effectiveStemOrder(new Set([...STEM_NAMES, "lead_vocals"])),
    STEM_NAMES,
  );
  same(
    "only the backing vocal present is not a split",
    effectiveStemOrder(new Set([...STEM_NAMES, "backing_vocals"])),
    STEM_NAMES,
  );
}

// ── syncStemNamesFromAPI ───────────────────────────────────────────────────
{
  const originalFetch = globalThis.fetch;
  const baseline = [...STEM_NAMES];

  const withFetch = async (impl) => {
    globalThis.fetch = impl;
    await syncStemNamesFromAPI();
  };

  // A server that is unreachable, erroring, or returning nonsense must leave
  // the working fallback alone. Emptying the list would render a player with
  // no lanes at all.
  await withFetch(async () => {
    throw new Error("network down");
  });
  same("a network failure leaves the fallback intact", STEM_NAMES, baseline);

  await withFetch(async () => ({ ok: false, status: 500, json: async () => ({}) }));
  same("a 500 leaves the fallback intact", STEM_NAMES, baseline);

  await withFetch(async () => ({ ok: true, json: async () => ({ stem_names: [] }) }));
  same("an empty list leaves the fallback intact", STEM_NAMES, baseline);

  await withFetch(async () => ({ ok: true, json: async () => ({ stem_names: "vocals" }) }));
  same("a non-list leaves the fallback intact", STEM_NAMES, baseline);

  await withFetch(async () => ({ ok: true, json: async () => ({}) }));
  same("a response with no stem_names leaves the fallback intact", STEM_NAMES, baseline);

  globalThis.fetch = originalFetch;
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
