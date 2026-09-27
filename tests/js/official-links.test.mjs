// A band's or a show's official links, read from the Wikidata item the artist
// box already fetches (artistLookup.js officialLinks): site, Instagram,
// Spotify, Apple Music. Wikidata is edited by anyone, so most of this is what
// happens to a value that is not what its property promises.
//
// No network. lookupArtist and lookupWork take their fetch as an argument.
//
// Run:  node tests/js/official-links.test.mjs

import { lookupArtist, lookupWork, officialLinks, officialLinkUrl } from "../../static/js/artistLookup.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.error(`FAIL  ${name}${detail ? `\n      ${detail}` : ""}`);
  }
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const claim = (value, rank = "normal") => ({ rank, mainsnak: { snaktype: "value", datavalue: { value } } });

// ── All four, as Metallica (Q15920) has them ──
const METALLICA = {
  P856: [claim("https://www.metallica.com")],
  P2003: [claim("metallica")],
  P1902: [claim("2ye2Wgw4gimLv2eAKyk1NB")],
  P2850: [claim("3996865")],
};
check("all four, in order", same(officialLinks(METALLICA), [
  { kind: "website", url: "https://www.metallica.com/" },
  { kind: "instagram", url: "https://www.instagram.com/metallica/" },
  { kind: "spotify", url: "https://open.spotify.com/artist/2ye2Wgw4gimLv2eAKyk1NB" },
  { kind: "appleMusic", url: "https://music.apple.com/artist/3996865" },
]), JSON.stringify(officialLinks(METALLICA)));

// ── Some, and none ──
check("only the ones present", same(officialLinks({ P2003: [claim("kealasettle")], P1902: [claim("7HV2RI2qNug4EcQqLbCAKS")] }), [
  { kind: "instagram", url: "https://www.instagram.com/kealasettle/" },
  { kind: "spotify", url: "https://open.spotify.com/artist/7HV2RI2qNug4EcQqLbCAKS" },
]));
check("no claims, no links", same(officialLinks({}), []));
check("undefined claims, no links", same(officialLinks(undefined), []));
check("a claim list that is not a list", same(officialLinks({ P856: "https://x.example" }), []));
check("plain http site is kept", same(officialLinks({ P856: [claim("http://www.mcifa.com")] }), [
  { kind: "website", url: "http://www.mcifa.com/" },
]));

// ── Rank ──
check("preferred beats an earlier normal", officialLinks({
  P856: [claim("https://old.example/"), claim("https://new.example/", "preferred")],
})[0]?.url === "https://new.example/");
check("first normal when none is preferred", officialLinks({
  P856: [claim("https://first.example/"), claim("https://second.example/")],
})[0]?.url === "https://first.example/");
check("deprecated never counts", same(officialLinks({ P1902: [claim("2ye2Wgw4gimLv2eAKyk1NB", "deprecated")] }), []));
check("deprecated skipped for the next normal", officialLinks({
  P856: [claim("https://gone.example/", "deprecated"), claim("https://here.example/")],
})[0]?.url === "https://here.example/");
check("an invalid preferred falls back to a valid normal", officialLinks({
  P1902: [claim("not-an-id", "preferred"), claim("2ye2Wgw4gimLv2eAKyk1NB")],
})[0]?.url === "https://open.spotify.com/artist/2ye2Wgw4gimLv2eAKyk1NB");
check("no value / some value statements are skipped", same(officialLinks({
  P856: [{ rank: "normal", mainsnak: { snaktype: "novalue" } }, { rank: "normal", mainsnak: { snaktype: "somevalue" } }],
}), []));
check("a value that is not a string is skipped", same(officialLinks({
  P2850: [claim(3996865)], P856: [claim({ url: "https://x.example" })],
}), []));

// ── Hostile or broken websites ──
const hostileSites = [
  "javascript:alert(1)",
  "JavaScript:alert(1)",
  " javascript:alert(1)",
  "java\tscript:alert(1)",
  "data:text/html,<script>alert(1)</script>",
  "vbscript:msgbox(1)",
  "file:///etc/passwd",
  "ftp://example.com/",
  "//evil.example/",
  "evil.example",
  "https://user:pass@evil.example/",
  "https://real.example@evil.example/",
  "https:///nohost",
  "https://exa mple.com/",
  "https://example.com/\n<script>",
  `https://example.com/${"a".repeat(3000)}`,
  "",
];
for (const value of hostileSites) {
  check(`site rejected: ${JSON.stringify(value.slice(0, 50))}`, officialLinkUrl("website", value) === "");
}
check("site with a path and query is kept as parsed",
  officialLinkUrl("website", "https://example.com/band?lang=en#top") === "https://example.com/band?lang=en#top");
check("site with markup characters comes back escaped, not as markup",
  !/[<>"]/.test(officialLinkUrl("website", "https://example.com/\"><img src=x onerror=alert(1)>")));

// ── Hostile or broken ids ──
const badInstagram = ["", ".", "..", "../../evil", "a/b", "name?x=1", "name#x", "a b", "<script>", "x".repeat(31), "javascript:alert(1)", "%2e%2e", "user\u0000"];
for (const value of badInstagram) {
  check(`instagram rejected: ${JSON.stringify(value)}`, officialLinkUrl("instagram", value) === "");
}
check("instagram with dots and underscores", officialLinkUrl("instagram", "wicked_musical") === "https://www.instagram.com/wicked_musical/");
check("instagram mixed case kept", officialLinkUrl("instagram", "Some.Band_1") === "https://www.instagram.com/Some.Band_1/");

const badSpotify = ["", "2ye2Wgw4gimLv2eAKyk1N", "2ye2Wgw4gimLv2eAKyk1NBX", "2ye2Wgw4gimLv2eAKyk1N/", "../../../../evil/xxxxxx", "2ye2Wgw4gimLv2eAKyk1N-", "spotify:artist:2ye2Wgw4g"];
for (const value of badSpotify) {
  check(`spotify rejected: ${JSON.stringify(value)}`, officialLinkUrl("spotify", value) === "");
}

const badApple = ["", "0", "0123", "12a", "-1", "1.5", "1e9", "1234567890123456", "123/../evil", " 123"];
for (const value of badApple) {
  check(`apple music rejected: ${JSON.stringify(value)}`, officialLinkUrl("appleMusic", value) === "");
}
check("apple music id", officialLinkUrl("appleMusic", "159260351") === "https://music.apple.com/artist/159260351");
check("unknown kind", officialLinkUrl("myspace", "abc") === "");
check("non-string value", officialLinkUrl("spotify", null) === "");

// Every URL that can come out is https on one of the four hosts, or an
// http(s) site: nothing else, whatever went in.
const everything = [...hostileSites, ...badInstagram, ...badSpotify, ...badApple, "metallica", "3996865", "https://ok.example"];
for (const kind of ["website", "instagram", "spotify", "appleMusic"]) {
  for (const value of everything) {
    const url = officialLinkUrl(kind, value);
    if (url) check(`${kind} ${JSON.stringify(value.slice(0, 30))} is http(s)`, /^https?:\/\/[^/@\s]+\//.test(url), url);
  }
}

// ── Through lookupArtist and lookupWork ──
function fakeFetch(entities) {
  return async (url) => {
    const u = new URL(url);
    if (u.host === "query.wikidata.org") return { results: { bindings: [] } };
    if (u.host.endsWith("wikipedia.org")) return { query: { pages: [{ extract: "" }] } };
    if (u.searchParams.get("props") === "labels") return { entities: {} };
    return { entities };
  };
}
const band = await lookupArtist("", "en", {
  id: "Q15920",
  fetchJson: fakeFetch({ Q15920: { id: "Q15920", labels: { en: { value: "Metallica" } }, claims: { P434: [{}], ...METALLICA } } }),
});
check("lookupArtist carries the links", band?.links?.length === 4 && band.links[0].kind === "website", JSON.stringify(band?.links));
const quiet = await lookupArtist("", "en", {
  id: "Q5484158",
  fetchJson: fakeFetch({ Q5484158: { id: "Q5484158", labels: { en: { value: "Norbert Leo Butz" } }, claims: { P434: [{}] } } }),
});
check("an artist with none carries an empty list", same(quiet?.links, []));
const work = await lookupWork("Q616439", "en", {
  fetchJson: fakeFetch({ Q616439: { id: "Q616439", labels: { en: { value: "Wicked" } }, claims: { P856: [claim("https://wickedthemusical.com")] } } }),
});
check("lookupWork carries the site", same(work?.links, [{ kind: "website", url: "https://wickedthemusical.com/" }]), JSON.stringify(work?.links));

console.log(`${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
