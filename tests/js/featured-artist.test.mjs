// The featured artist at the top of We Recommend (static/js/featuredArtist.js, #1092):
// which entry is current, who came before, what the grid leaves out, and the month.
//
// Run:  node tests/js/featured-artist.test.mjs

import {
  findMember,
  resolveFeatured,
  pickFeatured,
  groupsWithout,
  parseMonth,
  formatFeaturedMonth,
} from "../../static/js/featuredArtist.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";

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

const GROUPS = [
  { labelKey: "cat.artists", members: [{ name: "Ana", logo: "/a.webp" }, { name: "Bea" }, { name: "Cid" }] },
  { labelKey: "cat.gear", members: [{ name: "Dan" }] },
];
const entry = (name, month = "2026-11") => ({ id: name.toLowerCase(), month, name });

// ── findMember ──
check("a member is found by its exact name", findMember(GROUPS, "Dan")?.name === "Dan");
check("a name nobody has is null", findMember(GROUPS, "Zed") === null);
check("no name is null", findMember(GROUPS, "") === null && findMember(GROUPS, undefined) === null);
check("no groups is null", findMember(undefined, "Ana") === null);

// ── pickFeatured: current ──
{
  const { current, previous } = pickFeatured([entry("Ana")], GROUPS);
  check("one entry is the current one", current?.entry.name === "Ana");
  check("the current entry carries its member (photo, role)", current?.member.logo === "/a.webp");
  check("one entry has no one before it", same(previous, []));
}
{
  const { current } = pickFeatured([entry("Ana", "2026-11"), entry("Bea", "2026-12")], GROUPS);
  check("the last entry is the current one", current?.entry.name === "Bea");
}
{
  const { current, previous } = pickFeatured([], GROUPS);
  check("an empty list has no current entry, so no card", current === null);
  check("an empty list has no one before it", same(previous, []));
}
check("no list at all has no current entry", pickFeatured(undefined, GROUPS).current === null);

// ── pickFeatured: an entry that names nobody ──
{
  const { current, previous } = pickFeatured([entry("Ana"), entry("Zed")], GROUPS);
  check("a last entry that names nobody is skipped: the one before is current", current?.entry.name === "Ana");
  check("and it is not listed as before", same(previous, []));
}
{
  const { current, previous } = pickFeatured([entry("Zed"), entry("Ana")], GROUPS);
  check("an earlier entry that names nobody is skipped", current?.entry.name === "Ana" && previous.length === 0);
}
check("only entries that name nobody: no card", pickFeatured([entry("Zed")], GROUPS).current === null);
check("resolveFeatured keeps only entries that name a member",
  same(resolveFeatured([entry("Zed"), entry("Bea"), null], GROUPS).map((r) => r.entry.name), ["Bea"]));

// ── pickFeatured: previously featured ──
{
  const list = [entry("Ana", "2026-11"), entry("Bea", "2026-12"), entry("Cid", "2027-01")];
  const { previous } = pickFeatured(list, GROUPS);
  check("previously featured is most recent first", same(previous.map((p) => p.entry.name), ["Bea", "Ana"]));
}
{
  const list = [entry("Ana", "2026-11"), entry("Bea", "2026-12"), entry("Ana", "2027-01"), entry("Dan", "2027-02")];
  const { previous } = pickFeatured(list, GROUPS);
  check("someone featured twice is listed once", same(previous.map((p) => p.entry.name), ["Ana", "Bea"]));
}
{
  const list = [entry("Ana", "2026-11"), entry("Bea", "2026-12"), entry("Ana", "2027-01")];
  const { current, previous } = pickFeatured(list, GROUPS);
  check("the current artist is never listed as before, even if featured earlier too",
    current?.entry.name === "Ana" && same(previous.map((p) => p.entry.name), ["Bea"]));
}

// ── groupsWithout ──
{
  const shown = groupsWithout(GROUPS, "Bea");
  check("the current artist is left out of the grid", same(shown[0].members.map((m) => m.name), ["Ana", "Cid"]));
  check("other groups are untouched", same(shown[1].members.map((m) => m.name), ["Dan"]));
  check("FRIEND_GROUPS itself is not changed", GROUPS[0].members.length === 3);
}
check("a group left with no one is dropped", same(groupsWithout(GROUPS, "Dan").map((g) => g.labelKey), ["cat.artists"]));
check("no current artist leaves the grid whole", groupsWithout(GROUPS, undefined) === GROUPS);
check("a name nobody has leaves every member", groupsWithout(GROUPS, "Zed").flatMap((g) => g.members).length === 4);
{
  const list = [entry("Ana", "2026-11"), entry("Bea", "2026-12")];
  const { current } = pickFeatured(list, GROUPS);
  const names = groupsWithout(GROUPS, current.entry.name).flatMap((g) => g.members.map((m) => m.name));
  check("an earlier featured artist stays in the grid", names.includes("Ana") && !names.includes("Bea"));
}

// ── parseMonth / formatFeaturedMonth ──
check("a month parses", same(parseMonth("2026-11"), { year: 2026, month: 11 }));
check("month 13 is not a month", parseMonth("2026-13") === null);
check("month 00 is not a month", parseMonth("2026-00") === null);
check("a date is not a month", parseMonth("2026-11-01") === null);
check("nothing is not a month", parseMonth(undefined) === null);
check("English reads November 2026", formatFeaturedMonth("2026-11", "en") === "November 2026", formatFeaturedMonth("2026-11", "en"));
check("January stays January in UTC", formatFeaturedMonth("2027-01", "en") === "January 2027", formatFeaturedMonth("2027-01", "en"));
check("Spanish names the month in Spanish", /noviembre/.test(formatFeaturedMonth("2026-11", "es")), formatFeaturedMonth("2026-11", "es"));
check("an invalid month formats as nothing", formatFeaturedMonth("soon", "en") === "");

// ── The strings the card reads ──
const KEYS = [
  "friends.taglineAsk", "friends.featured.label", "friends.featured.labelMonth", "friends.featured.startHere",
  "friends.featured.kind.single", "friends.featured.release", "friends.featured.listen",
  "friends.featured.trackAria", "friends.featured.previous", "friends.featured.quote.adriannaClaro",
];
for (const [lang, table] of Object.entries(TRANSLATIONS)) {
  if (lang === "pt-PT") continue; // a variant: resolves through pt
  const missing = KEYS.filter((k) => !(k in table));
  check(`${lang} has every featured-artist string`, missing.length === 0, missing.join(", "));
  check(`${lang} label keeps its {month}`, table["friends.featured.labelMonth"]?.includes("{month}"));
  check(`${lang} track name keeps {title} and {artist}`,
    table["friends.featured.trackAria"]?.includes("{title}") && table["friends.featured.trackAria"]?.includes("{artist}"));
}
const allText = Object.values(TRANSLATIONS)
  .flatMap((table) => Object.entries(table))
  .filter(([k]) => k.startsWith("friends.") || k.startsWith("help.featured") || k === "help.recommend")
  .map(([, v]) => v)
  .join("\n");
check("no em dash in the We Recommend strings", !allText.includes(String.fromCharCode(0x2014)));

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
