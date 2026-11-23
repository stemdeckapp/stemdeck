// The featured artist at the top of We Recommend (static/js/featuredArtist.js, #1092):
// which entry is current, who came before, what the grid leaves out, the month,
// how an image is drawn, and the "Previously featured" row.
//
// The row only draws with two entries or more, and FEATURED_ARTISTS has one, so
// no browser test can reach it without changing the data. What the row draws
// (previousFaces) and which grid card an avatar goes to (findFriendCard) are
// therefore tested here; featured-artist.spec.mjs checks the row is absent.
//
// Run:  node tests/js/featured-artist.test.mjs

import {
  findMember,
  resolveFeatured,
  pickFeatured,
  groupsWithout,
  parseMonth,
  formatFeaturedMonth,
  imageShape,
  initialOf,
  previousFaces,
  findFriendCard,
  FEATURED_ARTISTS,
} from "../../static/js/featuredArtist.js";
import { TRANSLATIONS, hasKey } from "../../static/js/i18n.js";

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

// ── imageShape: a photo is cropped, a wordmark is shown whole ──
check("a profile photo is a photo", imageShape({ logo: "/a.jpg", avatar: true }) === "photo");
check("an image without avatar is a wordmark", imageShape({ logo: "/w.png" }) === "logo");
check("no image is none", imageShape({ name: "X" }) === "none");
check("no member is none", imageShape(undefined) === "none");

// ── initialOf ──
check("the initial is the first letter, upper case", initialOf("  dead roses") === "D");
check("no name is a question mark", initialOf("") === "?" && initialOf(undefined) === "?");

// ── previousFaces: what the "Previously featured" row draws ──
{
  const groups = [{ members: [
    { name: "Ana", logo: "/a.jpg", avatar: true },
    { name: "Bea", logo: "/b.png" },
    { name: "Cid" },
    { name: "Dan", logo: "/d.jpg", avatar: true },
  ] }];
  const list = [entry("Ana", "2026-09"), entry("Bea", "2026-10"), entry("Cid", "2026-11"), entry("Dan", "2026-12")];
  const faces = previousFaces(pickFeatured(list, groups).previous);
  check("one face per earlier artist, most recent first", same(faces.map((f) => f.name), ["Cid", "Bea", "Ana"]));
  check("a photo face is cropped", same(faces[2], { name: "Ana", image: "/a.jpg", shape: "photo", initial: "A" }));
  check("a wordmark face is shown whole", faces[1].shape === "logo" && faces[1].image === "/b.png");
  check("a face with no image draws its initial", faces[0].shape === "none" && faces[0].image === "" && faces[0].initial === "C");
  check("no earlier artist draws no face", same(previousFaces(pickFeatured([entry("Ana")], groups).previous), []));
  check("nothing draws nothing", same(previousFaces(undefined), []));
}

// ── findFriendCard: where an avatar goes ──
{
  const card = (name) => ({ dataset: { friend: name } });
  const cards = [card("Ana"), card("Bea"), {}, card("Cid")];
  check("the card named by data-friend is found", findFriendCard(cards, "Bea") === cards[1]);
  check("a name with no card is null", findFriendCard(cards, "Zed") === null);
  check("no cards is null", findFriendCard(undefined, "Ana") === null);
}

// ── FEATURED_ARTISTS itself: read from the data, so a new featured artist
//    is a change to the list alone ──
{
  const ids = FEATURED_ARTISTS.map((e) => e.id);
  check("every entry has a unique id", new Set(ids).size === ids.length && ids.every(Boolean), ids.join(", "));
  for (const e of FEATURED_ARTISTS) {
    check(`${e.id}: has a name`, typeof e.name === "string" && e.name.trim().length > 0);
    check(`${e.id}: its month is a month`, parseMonth(e.month) !== null, e.month);
    check(`${e.id}: its month formats`, formatFeaturedMonth(e.month, "en").length > 0);
    check(`${e.id}: every link is https with a label`,
      (e.links || []).every((l) => l.label && /^https:\/\//.test(l.url)), JSON.stringify(e.links));
    if (e.track) {
      check(`${e.id}: its track has a title and an https link`, Boolean(e.track.title) && /^https:\/\//.test(e.track.url));
      check(`${e.id}: its track kind is a key every full table has`, Object.entries(TRANSLATIONS)
        .filter(([lang]) => lang !== "pt-PT").every(([, table]) => e.track.kindKey in table));
    }
    if (e.quoteKey) {
      const missing = Object.entries(TRANSLATIONS)
        .filter(([lang, table]) => lang !== "pt-PT" && !(e.quoteKey in table)).map(([lang]) => lang);
      check(`${e.id}: its quote is in every full table`, missing.length === 0, missing.join(", "));
    }
  }
  // A placeholder quote can never ship. Bracketed capitals ("[SOMETHING
  // PENDING]") or a value starting with PLACEHOLDER, in any table, pt-PT too.
  const latest = FEATURED_ARTISTS.at(-1);
  if (latest?.quoteKey) {
    const placeholder = (v) => /\[[A-Z][A-Z ]*\]/.test(v) || /^\s*PLACEHOLDER/.test(v);
    const held = Object.entries(TRANSLATIONS)
      .filter(([, table]) => typeof table[latest.quoteKey] === "string" && placeholder(table[latest.quoteKey]))
      .map(([lang]) => lang);
    check(`${latest.id}: no table holds a placeholder quote`, held.length === 0, held.join(", "));
    check("the placeholder test catches a pending quote", placeholder("[" + "QUOTE " + "PENDING]") && placeholder("PLACEHOLDER text"));
    check("and lets a real quote through", !placeholder(TRANSLATIONS.en[latest.quoteKey] || ""));
  }
  // A quote key no entry names is a leftover from an artist taken off the list.
  const named = new Set(FEATURED_ARTISTS.map((e) => e.quoteKey).filter(Boolean));
  const orphans = Object.keys(TRANSLATIONS.en).filter((k) => k.startsWith("friends.featured.quote.") && !named.has(k));
  check("no quote is left for an artist no longer on the list", orphans.length === 0, orphans.join(", "));
  // The list as the card sees it: groups made from the entries' own names, so
  // this checks the list (order, duplicates), not who is in FRIEND_GROUPS.
  const groups = [{ members: FEATURED_ARTISTS.map((e) => ({ name: e.name })) }];
  const { current, previous } = pickFeatured(FEATURED_ARTISTS, groups);
  check("the last entry is the current one", current?.entry === FEATURED_ARTISTS.at(-1));
  check("the current artist is never also listed as before", !previous.some((p) => p.entry.name === current?.entry.name));
}

// ── hasKey: how the card decides to draw a quote at all ──
check("a key a table has is there", hasKey("friends.featured.startHere"));
check("a key no table has is not, so the card draws no quote", !hasKey("friends.featured.quote.nobodyAtAll"));

// ── The strings the card reads ──
const KEYS = [
  "friends.taglineAsk", "friends.featured.label", "friends.featured.labelMonth", "friends.featured.startHere",
  "friends.featured.kind.single", "friends.featured.release", "friends.featured.listen",
  "friends.featured.trackAria", "friends.featured.previous", "friends.featured.previousGo",
];
for (const [lang, table] of Object.entries(TRANSLATIONS)) {
  if (lang === "pt-PT") continue; // a variant: resolves through pt
  const missing = KEYS.filter((k) => !(k in table));
  check(`${lang} has every featured-artist string`, missing.length === 0, missing.join(", "));
  check(`${lang} label keeps its {month}`, table["friends.featured.labelMonth"]?.includes("{month}"));
  check(`${lang} track name keeps {title} and {artist}`,
    table["friends.featured.trackAria"]?.includes("{title}") && table["friends.featured.trackAria"]?.includes("{artist}"));
  check(`${lang} avatar name keeps its {name}`, table["friends.featured.previousGo"]?.includes("{name}"));
}
// The design has no monthly cadence: the card stays until the next entry.
const monthly = /del mes|of the month|du mois|do mês|des Monats/i;
const promises = Object.values(TRANSLATIONS).flatMap((table) =>
  ["friends.featured.label", "friends.featured.labelMonth", "friends.featured.previous", "help.featuredPrevious", "help.recommend"]
    .filter((k) => monthly.test(table[k] || "")).map((k) => `${k}: ${table[k]}`));
check("no label promises a featured artist every month", promises.length === 0, promises.join(" | "));
const allText = Object.values(TRANSLATIONS)
  .flatMap((table) => Object.entries(table))
  .filter(([k]) => k.startsWith("friends.") || k.startsWith("help.featured") || k.startsWith("help.friend") || k === "help.recommend")
  .map(([, v]) => v)
  .join("\n");
check("no em dash in the We Recommend strings", !allText.includes(String.fromCharCode(0x2014)));

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
