// The artist box's Discogs part (artistDiscogs.js): checking the server's
// answer, a band known only to Discogs, and filling the gaps a Wikidata band
// leaves, only when it is the same band. Plus the Discogs id a Wikidata item
// names (artistLookup.js discogsArtistId).
//
// Pure functions, no network. The answer is shaped as GET
// /api/jobs/{id}/artist-extra gives it (tests/test_artist_extra_api.py).
//
// Run:  node tests/js/artist-discogs.test.mjs

import { discogsBand, discogsExtraFromJson, needsDiscogs, sameArtist, withDiscogs } from "../../static/js/artistDiscogs.js";
import { discogsArtistId } from "../../static/js/artistLookup.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.error(`FAIL ${name}`, detail);
  }
};

const NIHIL = {
  id: 555501,
  name: "Nihil",
  real_name: "",
  profile: ["Portuguese sludge band from Porto.", "Their debut came out in 2021."],
  members: { current: ["Rui Barros", "Ana Lima"], former: ["Pedro Sá"] },
  groups: [],
  links: [
    { kind: "website", url: "http://www.nihilband.pt/" },
    { kind: "bandcamp", url: "https://nihil.bandcamp.com/" },
    { kind: "instagram", url: "https://www.instagram.com/nihil.porto/" },
    { kind: "youtube", url: "javascript:alert(1)" },
    { kind: "myspace", url: "https://myspace.com/nihil" },
  ],
  releases: [
    { year: "2019", title: "Barro" },
    { year: "", title: "Untitled" },
    { year: "20199", title: "" },
  ],
  url: "https://www.discogs.com/artist/555501-Nihil-5",
};

// ── the answer's shape ──

const extra = discogsExtraFromJson(NIHIL);
check("an answer is read", extra?.id === 555501 && extra.name === "Nihil");
check("only http(s) links of a kind the box draws", JSON.stringify(extra.links.map((l) => l.kind)) === '["website","bandcamp","instagram"]', extra.links);
check("a release with no title is dropped", extra.releases.length === 2);
check("the page link is kept when it is Discogs'", extra.url === NIHIL.url);
check("a page link elsewhere is rebuilt from the id", discogsExtraFromJson({ ...NIHIL, url: "https://evil.example/" }).url === "https://www.discogs.com/artist/555501");
check("no id is no answer", discogsExtraFromJson({ ...NIHIL, id: "555501" }) === null);
check("no name is no answer", discogsExtraFromJson({ ...NIHIL, name: "" }) === null);
check("nothing is no answer", discogsExtraFromJson(null) === null);

// ── a band only Discogs knows ──

const band = discogsBand(extra);
check("its history is the profile", band.history.length === 2 && band.history[0].startsWith("Portuguese"));
check("members are the box's shape", band.members.current[0].name === "Rui Barros" && band.members.former[0].name === "Pedro Sá");
check("its releases are titled as releases", band.albumsStudioOnly === null && band.albums[0].title === "Barro");
check("it has no Wikidata id to be saved by", band.id === "");
check("it is credited to Discogs alone", band.discogs.only === true && band.discogs.url === NIHIL.url);
check("no Wikidata band means the Discogs one", withDiscogs(null, extra).name === "Nihil");

// ── a Wikidata band beside it ──

const wikipedia = {
  id: "Q1",
  name: "Nihil",
  englishName: "Nihil",
  history: ["From Wikipedia."],
  historyLang: "en",
  members: { current: [{ name: "Someone", native: "" }], former: [] },
  albums: [{ year: "2020", title: "Wiki Album" }],
  albumsStudioOnly: true,
  links: [{ kind: "website", url: "https://official.example/" }],
  discogsId: "",
};

check("a complete band is not asked about", needsDiscogs(wikipedia) === false);
check("no band is asked about", needsDiscogs(null) === true);
check("no history is asked about", needsDiscogs({ ...wikipedia, history: [] }) === true);
check("no albums is asked about", needsDiscogs({ ...wikipedia, albums: [] }) === true);

const full = withDiscogs(wikipedia, extra);
check("Wikipedia keeps its history", full.history[0] === "From Wikipedia.");
check("Wikipedia keeps its members and albums", full.members.current[0].name === "Someone" && full.albums[0].title === "Wiki Album");
check("links of a kind it lacks are added, marked", full.links.length === 3 && full.links.slice(1).every((l) => l.fromDiscogs));
check("its own website is not replaced", full.links[0].url === "https://official.example/" && !full.links[0].fromDiscogs);
check("only the links were filled", JSON.stringify(full.discogs.filled) === '["links"]');
check("credited to both", full.discogs.only === false);
check("the band given in is not changed", wikipedia.links.length === 1 && !wikipedia.discogs);

const stub = withDiscogs({ ...wikipedia, history: [], members: { current: [], former: [] }, albums: [] }, extra);
check("gaps are filled from Discogs", stub.history[0].startsWith("Portuguese") && stub.members.current.length === 2 && stub.albums.length === 2);
check("each filled section is named", ["history", "members", "albums"].every((k) => stub.discogs.filled.includes(k)));
check("filled albums are titled as releases", stub.albumsStudioOnly === null);

const nothingNew = withDiscogs({ ...wikipedia, links: extra.links }, { ...extra, profile: [], members: { current: [], former: [] }, releases: [] });
check("nothing to fill leaves the band as it was", !nothingNew.discogs);

// ── the same band? ──

check("the same name is the same band", sameArtist(wikipedia, extra));
check("another name is not", !sameArtist({ ...wikipedia, name: "Nihilist", englishName: "" }, extra));
check("another band is left alone", withDiscogs({ ...wikipedia, name: "Nihilist", englishName: "", history: [] }, extra).history.length === 0);
check("the Discogs id Wikidata gives decides", sameArtist({ ...wikipedia, discogsId: "555501" }, extra));
check("a different Discogs id is another band, whatever the name", !sameArtist({ ...wikipedia, discogsId: "222202" }, extra));

// ── the Discogs id on a Wikidata item ──

const claim = (value, rank = "normal") => ({ rank, mainsnak: { datavalue: { value } } });
check("P1953 is read", discogsArtistId({ P1953: [claim("555501")] }) === "555501");
check("none is empty", discogsArtistId({}) === "");
check("two different ones are no answer", discogsArtistId({ P1953: [claim("1"), claim("2")] }) === "");
check("a deprecated one does not count", discogsArtistId({ P1953: [claim("1", "deprecated"), claim("2")] }) === "2");
check("a value that is not an id does not count", discogsArtistId({ P1953: [claim("abc")] }) === "");

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
