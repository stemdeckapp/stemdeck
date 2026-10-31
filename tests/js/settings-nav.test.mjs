// The Settings dialog's search and arrow-key navigation (static/js/settingsNav.js).
//
// Run:  node tests/js/settings-nav.test.mjs

import {
  normalizeText,
  queryTerms,
  findRanges,
  highlightParts,
  searchSettings,
  countBySection,
  nextTabIndex,
} from "../../static/js/settingsNav.js";

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
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ── normalizeText ──
{
  const { norm, map } = normalizeText("Löschen");
  check("accents fold and case lowers", norm === "loschen", norm);
  check("the map points back at the original letters", map[1] === 1 && map[norm.length] === 7, JSON.stringify(map));
  check("null is an empty string", normalizeText(null).norm === "");
  check("Japanese passes through", normalizeText("言語").norm === "言語");
}

// ── queryTerms ──
check("terms split on white space", same(queryTerms("  Sample   RATE "), ["sample", "rate"]));
check("an empty query has no terms", same(queryTerms("   "), []));

// ── findRanges ──
check("one match", same(findRanges("Sample rate", ["rate"]), [[7, 11]]));
check("every occurrence", same(findRanges("a log, a log", ["log"]), [[2, 5], [9, 12]]));
check("overlapping terms merge", same(findRanges("export", ["exp", "port"]), [[0, 6]]));
check("an accented match highlights the original", same(findRanges("Gefahrenzone löschen", ["loschen"]), [[13, 20]]));
check("no match, no ranges", same(findRanges("Port", ["xyz"]), []));

// ── highlightParts ──
check(
  "parts alternate plain and matched",
  same(highlightParts("Sample rate", [[7, 11]]), [
    { text: "Sample ", match: false },
    { text: "rate", match: true },
  ]),
);
check("no ranges is one plain part", same(highlightParts("Port", []), [{ text: "Port", match: false }]));
check(
  "a match at the start",
  same(highlightParts("Port", [[0, 2]]), [
    { text: "Po", match: true },
    { text: "rt", match: false },
  ]),
);

// ── searchSettings ──
const entries = [
  { pane: "general", title: "Language", desc: "Display language for this app.", group: "Interface", section: "General" },
  { pane: "general", title: "Compute device", desc: "Device used for stem separation.", group: "Separation", section: "General" },
  { pane: "library", title: "Reset app data", desc: "Permanently deletes every track.", group: "Danger zone", section: "Library" },
  { pane: "export", title: "Sample rate", desc: "Sample rate for exported mixes.", group: "Audio", section: "Export" },
  { pane: "network", title: "Port", desc: "Port StemDeck runs on.", group: "", section: "Network" },
  { pane: "library", title: "Delete after", desc: "Days a finished track is kept.", group: "Automatic cleanup", section: "Library" },
];
check("an empty query finds nothing", searchSettings(entries, "").length === 0);
{
  const r = searchSettings(entries, "language");
  check("a title match is found", r.length === 1 && r[0].entry.title === "Language");
  check("its title is highlighted", same(r[0].titleRanges, [[0, 8]]));
  check("its description is highlighted too", same(r[0].descRanges, [[8, 16]]));
}
{
  const r = searchSettings(entries, "danger");
  check("a group heading finds its settings", r.length === 1 && r[0].entry.title === "Reset app data");
  check("the group is not highlighted in the title", r[0].titleRanges.length === 0);
}
{
  const r = searchSettings(entries, "network");
  check("a section name finds its settings", r.length === 1 && r[0].entry.title === "Port");
}
{
  const r = searchSettings(entries, "track");
  check(
    "title matches rank above description matches, otherwise dialog order",
    same(r.map((x) => x.entry.title), ["Reset app data", "Delete after"]),
    JSON.stringify(r.map((x) => x.entry.title)),
  );
}
{
  const r = searchSettings(entries, "rate sample");
  check("every word must match, in any order", r.length === 1 && r[0].entry.title === "Sample rate");
  check("both words are highlighted", same(r[0].titleRanges, [[0, 6], [7, 11]]));
}
{
  const r = searchSettings(entries, "delete");
  check(
    "a full title match outranks a description match",
    same(r.map((x) => x.entry.title), ["Delete after", "Reset app data"]),
    JSON.stringify(r.map((x) => x.entry.title)),
  );
}
check("no match is an empty list", searchSettings(entries, "zzzz").length === 0);
check("case and accents do not matter", searchSettings(entries, "SÁMPLE").length === 1);
check(
  "results count per section",
  same(countBySection(searchSettings(entries, "e")), { general: 2, library: 2, export: 1, network: 1 }),
  JSON.stringify(countBySection(searchSettings(entries, "e"))),
);

// ── nextTabIndex ──
check("down moves on", nextTabIndex(0, "ArrowDown", 8) === 1);
check("right moves on too", nextTabIndex(3, "ArrowRight", 8) === 4);
check("down wraps to the top", nextTabIndex(7, "ArrowDown", 8) === 0);
check("up wraps to the bottom", nextTabIndex(0, "ArrowUp", 8) === 7);
check("left moves back", nextTabIndex(5, "ArrowLeft", 8) === 4);
check("Home is the first", nextTabIndex(5, "Home", 8) === 0);
check("End is the last", nextTabIndex(2, "End", 8) === 7);
check("another key does not move", nextTabIndex(2, "Enter", 8) === null);
check("an empty list does not move", nextTabIndex(0, "ArrowDown", 0) === null);

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
